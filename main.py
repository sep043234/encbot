"""
使用 aiogram 的 Bot API 实现一个 Telegram Bot：

1) 接收用户发送的文件，获取 file_unique_id，
	先通过 build_file_token 生成 token，再用 telegram_to_unicode_cjk 转成 CJK 字符串。

2) 接收用户粘贴的 CJK 字符串，
	先用 unicode_cjk_to_telegram 还原 token，再用 parse_file_token 解析字段。
"""

from __future__ import annotations
import re
import asyncio
import os
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message

from utils.utf_utils import UtfConverter
from dotenv import load_dotenv
load_dotenv(dotenv_path='.env')
BOT_TOKEN = os.getenv("BOT_TOKEN")
MEDIA_FORWARD_USER_ID = int(os.getenv("MEDIA_FORWARD_USER_ID", "0") or 0)
ENCODED_FORWARD_CHAT_ID = int(os.getenv("ENCODED_FORWARD_CHAT_ID", "0") or 0)
ENCODED_FORWARD_THREAD_ID = int(os.getenv("ENCODED_FORWARD_THREAD_ID", "0") or 0)


def _parse_whitelist_ids(raw: str) -> set[int]:
	ids: set[int] = set()
	for item in str(raw or "").split(","):
		text = item.strip()
		if not text:
			continue
		if text.lstrip("-").isdigit():
			ids.add(int(text))
	return ids


ENCODED_FORWARD_WHITELIST_USER_IDS = _parse_whitelist_ids(os.getenv("ENCODED_FORWARD_WHITELIST", ""))

if not BOT_TOKEN:
	raise RuntimeError("Missing bot token. Please set ENCBOT_TOKEN or BOT_TOKEN.")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ENCODER_UI_STATE: dict[tuple[int, int], dict[str, Any]] = {}
bot_name = ""
USED_FLASH_NONCES: dict[str, datetime] = {}
PERM_FLASH_NONCE_RETENTION_DAYS = 30


def _cleanup_used_flash_nonces(now: datetime) -> None:
	expired_keys = [key for key, expires_at in USED_FLASH_NONCES.items() if now >= expires_at]
	for key in expired_keys:
		USED_FLASH_NONCES.pop(key, None)


def _extract_media_info(message: Message) -> tuple[str, str]:
	"""
	从消息中提取 (file_type, file_id)。
	若不是支持的媒体类型，抛出 ValueError。
	"""
	if message.document:
		return "document", message.document.file_id
	if message.photo:
		# photo 为多个尺寸，取最大尺寸通常在最后一个
		return "photo", message.photo[-1].file_id
	if message.video:
		return "video", message.video.file_id
	if message.audio:
		return "audio", message.audio.file_id
	if message.voice:
		return "voice", message.voice.file_id
	if message.animation:
		return "animation", message.animation.file_id
	if message.sticker:
		return "sticker", message.sticker.file_id

	raise ValueError("Unsupported media type")


def _build_display(data: dict[str, Any], token: str, encoded: str) -> str:
	valid_until = str(data.get("valid_until", ""))
	if valid_until == "99991231235959":
		valid_until_display = "永久有效"
	elif len(valid_until) == 14 and valid_until.isdigit():
		valid_until_display = (
			f"{valid_until[0:4]}-{valid_until[4:6]}-{valid_until[6:8]} "
			f"{valid_until[8:10]}:{valid_until[10:12]}:{valid_until[12:14]}"
		)
	else:
		valid_until_display = valid_until


	bot_name_lack = bot_name[:-1] if bot_name else ""
	hidden_char = "\u200b"
	start_char = "⟦["
	end_char = "]⟧"

	return_text = ""

	if(data['no_forward']==True):
		return_text += f"🚫 禁止转发: 是\n"
	
	if(data['flash_seconds']>0):
		return_text += f"⚡ 闪照时间: {data['flash_seconds']} 秒\n"

	if(data['valid_until']!="99991231235959"):
		return_text += f"⏳ 有效时间: {valid_until_display}\n\n"

	

	return_text += (	
		f"\n将取件码👇传给 🤖 <code>{bot_name_lack}</code><code> t</code> (去空格) \n\n{start_char}<code>{encoded}</code>{end_char}"
	)

	return return_text


def _resolve_valid_until(mode: str) -> str:
	if mode == "perm":
		return "99991231235959"
	if mode == "10m":
		return (datetime.now() + timedelta(minutes=10)).strftime("%Y%m%d%H%M%S")
	if mode == "30m":
		return (datetime.now() + timedelta(minutes=30)).strftime("%Y%m%d%H%M%S")
	if mode == "1h":
		return (datetime.now() + timedelta(hours=1)).strftime("%Y%m%d%H%M%S")
	raise ValueError(f"Unsupported valid mode: {mode}")


def _choice(label: str, selected: bool) -> str:
	return f"✅ {label}" if selected else f"{label}"


def _build_controls_keyboard(state: dict[str, Any], encoded: str) -> InlineKeyboardMarkup:
	no_forward = bool(state.get("no_forward", False))
	flash_seconds = int(state.get("flash_seconds", 0))
	valid_mode = str(state.get("valid_mode", "perm"))
	long_flash_seconds = int(state.get("video_flash_seconds", 60))
	long_flash_label = f"{long_flash_seconds}秒" if str(state.get("file_type", "")) == "video" else "60秒"

	return InlineKeyboardMarkup(
		inline_keyboard=[
			[
				InlineKeyboardButton(
					text="🚫 目前限制转发" if no_forward else "🆗 目前可以转发",
					callback_data=f"enc:fw:{0 if no_forward else 1}",
				),
			],
			[
				InlineKeyboardButton(
					text=_choice("不闪", flash_seconds == 0),
					callback_data="enc:fl:0",
				),
				InlineKeyboardButton(
					text=_choice("20秒", flash_seconds == 20),
					callback_data="enc:fl:20",
				),
				InlineKeyboardButton(
					text=_choice(long_flash_label, flash_seconds == long_flash_seconds),
					callback_data=f"enc:fl:{long_flash_seconds}",
				),
			],
			[
				InlineKeyboardButton(
					text=_choice("永久", valid_mode == "perm"),
					callback_data="enc:vu:perm",
				),
				InlineKeyboardButton(
					text=_choice("10分钟", valid_mode == "10m"),
					callback_data="enc:vu:10m",
				),
				InlineKeyboardButton(
					text=_choice("60分钟", valid_mode == "30m"),
					callback_data="enc:vu:30m",
				)
			],
			[
				InlineKeyboardButton(
					text="📋 复制密文",
					copy_text=CopyTextButton(text=encoded),
				)
			],
		]
	)


def _build_token_and_encoded(state: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
	valid_until = _resolve_valid_until(str(state.get("valid_mode", "perm")))
	token = UtfConverter.build_file_token(
		user_id=int(state["user_id"]),
		file_id=str(state["file_id"]),
		file_type=str(state["file_type"]),
		no_forward=bool(state.get("no_forward", False)),
		flash_seconds=int(state.get("flash_seconds", 0)),
		valid_until=valid_until,
	)
	encoded = UtfConverter.telegram_to_unicode_cjk(token)
	parsed = UtfConverter.parse_file_token(token)
	return token, encoded, parsed


def _format_duration(seconds: int) -> str:
	seconds = max(0, int(seconds))
	days, rem = divmod(seconds, 86400)
	hours, rem = divmod(rem, 3600)
	minutes, secs = divmod(rem, 60)

	parts: list[str] = []
	if days:
		parts.append(f"{days}天")
	if hours:
		parts.append(f"{hours}小时")
	if minutes:
		parts.append(f"{minutes}分钟")
	if secs or not parts:
		parts.append(f"{secs}秒")

	return "".join(parts)


async def _send_media_by_type(message: Message, data: dict[str, Any]) -> Message:
	file_type = str(data["file_type"])
	file_id = str(data["file_id"])
	no_forward = bool(data.get("no_forward", False))

	if file_type == "document":
		return await message.answer_document(file_id, protect_content=no_forward)
	if file_type == "photo":
		return await message.answer_photo(file_id, protect_content=no_forward)
	if file_type == "video":
		return await message.answer_video(file_id, protect_content=no_forward)
	if file_type == "audio":
		return await message.answer_audio(file_id, protect_content=no_forward)
	if file_type == "voice":
		return await message.answer_voice(file_id, protect_content=no_forward)
	if file_type == "animation":
		return await message.answer_animation(file_id, protect_content=no_forward)
	if file_type == "sticker":
		return await message.answer_sticker(file_id, protect_content=no_forward)

	raise ValueError(f"Unsupported file_type: {file_type}")


async def _delete_message_later(sent_message: Message, delay_seconds: int) -> None:
	await asyncio.sleep(delay_seconds)
	try:
		await sent_message.delete()
	except Exception:
		# 可能因权限/消息状态无法删除，忽略即可
		pass


async def _forward_media_in_background(message: Message) -> None:
	if MEDIA_FORWARD_USER_ID <= 0:
		print("[MEDIA_FORWARD] MEDIA_FORWARD_USER_ID not set, skip forwarding", flush=True)
		return

	try:
		result = await bot.copy_message(
			chat_id=MEDIA_FORWARD_USER_ID,
			from_chat_id=message.chat.id,
			message_id=message.message_id,
		)
		print(f"[MEDIA_FORWARD] forward result: {result}", flush=True)
	except Exception as exc:
		print(f"[MEDIA_FORWARD] forward failed: {exc}", flush=True)


async def _forward_encoded_if_whitelisted(message: Message, encoded: str) -> None:
	if ENCODED_FORWARD_CHAT_ID == 0:
		return

	from_user_id = int(message.from_user.id) if message.from_user else 0
	if from_user_id <= 0 or from_user_id not in ENCODED_FORWARD_WHITELIST_USER_IDS:
		# print(f"[ENCODED_FORWARD] user {from_user_id} not in whitelist, skip forwarding", flush=True)
		return

	try:
		kwargs = {
			"chat_id": ENCODED_FORWARD_CHAT_ID,
			"parse_mode": "HTML",
			"text": encoded,
		}
		if ENCODED_FORWARD_THREAD_ID > 0:
			kwargs["message_thread_id"] = ENCODED_FORWARD_THREAD_ID

		await bot.send_message(**kwargs)
	except Exception as exc:
		print(f"[ENCODED_FORWARD] send failed: {exc}", flush=True)


@dp.message(F.chat.type == "private", Command("start"))
async def cmd_start(message: Message) -> None:
	await message.reply(
		"👋 你好！\n\n"
		"发送文件 （ 图片/文件/视频/音频/语音...）给我，我会回复取件码(加密后字符串)。\n"
		"你也可以直接粘贴取件码，我会解码返回媒体。"
		"一个取件码只支持一个媒体文件。\n\n"
	)


@dp.message(Command("about"))
async def cmd_about(message: Message) -> None:
	await message.reply("你好\n欢迎")


@dp.message(
	F.chat.type == "private",
	F.document | F.photo | F.video | F.audio | F.voice | F.animation | F.sticker,
)
async def on_media(message: Message) -> None:
	try:
		asyncio.create_task(_forward_media_in_background(message))

		file_type, file_id = _extract_media_info(message)
		state = {
			"owner_user_id": message.from_user.id if message.from_user else 0,
			"user_id": message.from_user.id if message.from_user else 0,
			"file_id": file_id,
			"file_type": file_type,
			"no_forward": False,
			"flash_seconds": 0,
			"video_flash_seconds": (int(getattr(message.video, "duration", 0) or 0) + 15) if file_type == "video" else 60,
			"valid_mode": "perm",
		}

		token, encoded, parsed = _build_token_and_encoded(state)
		display_text = _build_display(parsed, token, encoded)
		asyncio.create_task(_forward_encoded_if_whitelisted(message, display_text))
		markup = _build_controls_keyboard(state, encoded)
		panel = await message.reply(display_text, reply_markup=markup, parse_mode="HTML")
		ENCODER_UI_STATE[(message.chat.id, panel.message_id)] = state
	except Exception as exc:
		await message.reply(f"❌ 取码失败: {exc}")


@dp.callback_query(F.data.startswith("enc:"))
async def on_encode_controls(callback: CallbackQuery) -> None:
	if not callback.message:
		await callback.answer("无法获取消息", show_alert=True)
		return
	if callback.message.chat.type != "private":
		await callback.answer("仅支持私信", show_alert=True)
		return

	state_key = (callback.message.chat.id, callback.message.message_id)
	state = ENCODER_UI_STATE.get(state_key)
	if not state:
		await callback.answer("此按钮已失效，请重新发送媒体", show_alert=True)
		return

	if (callback.from_user and callback.from_user.id) != int(state.get("owner_user_id", 0)):
		await callback.answer("只能由原发送者操作", show_alert=True)
		return

	try:
		_, group, value = str(callback.data).split(":", 2)
		if group == "fw":
			state["no_forward"] = value == "1"
		elif group == "fl":
			state["flash_seconds"] = int(value)
		elif group == "vu":
			if value not in {"perm", "10m", "30m", "1h"}:
				raise ValueError("invalid valid mode")
			state["valid_mode"] = value
		else:
			raise ValueError("unknown control group")

		long_flash_seconds = int(state.get("video_flash_seconds", 60))
		force_no_forward = (
			int(state.get("flash_seconds", 0)) in {20, long_flash_seconds}
			or str(state.get("valid_mode", "perm")) in {"10m", "30m"}
		)
		if force_no_forward:
			state["no_forward"] = True

		token, encoded, parsed = _build_token_and_encoded(state)
		markup = _build_controls_keyboard(state, encoded)
		await callback.message.edit_text(_build_display(parsed, token, encoded), reply_markup=markup, parse_mode="HTML")
		await callback.answer("已更新密文")
	except Exception as exc:
		await callback.answer(f"更新失败: {exc}", show_alert=True)


@dp.message(F.chat.type == "private", F.text)
async def on_text(message: Message) -> None:
	text = (message.text or "").strip()
	if not text or len(text) < 15:
		return

	marked_nonce = ""
	try:
		parse_text = text
		START = "⟦["
		END = "]⟧"
		pattern = re.escape(START) + r"(.*?)" + re.escape(END)
		matches = re.findall(pattern, text, flags=re.S)

		for item in matches:
			parse_text = item.strip()
			break
			

		token = UtfConverter.unicode_cjk_to_telegram(parse_text)
		data = UtfConverter.parse_file_token(token)

		valid_until_dt = datetime.strptime(str(data["valid_until"]), "%Y%m%d%H%M%S")
		now = datetime.now()
		_cleanup_used_flash_nonces(now)

		if now > valid_until_dt:
			overdue_seconds = int((now - valid_until_dt).total_seconds())
			overdue_text = _format_duration(overdue_seconds)
			await message.reply(
				"❌ 此 token 已过期\n"
				f"过期时间: {valid_until_dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
				f"已过期: {overdue_text}"
			)
			return

		flash_seconds = int(data.get("flash_seconds", 0))
		nonce_key = str(data.get("nonce", ""))
		if flash_seconds > 0:
			expires_at = USED_FLASH_NONCES.get(nonce_key)
			if expires_at and now < expires_at:
				await message.reply("❌ 此闪读密文仅可读取一次")
				return
			if str(data.get("valid_until", "")) == "99991231235959":
				expires_at = now + timedelta(days=PERM_FLASH_NONCE_RETENTION_DAYS)
			else:
				expires_at = valid_until_dt
			USED_FLASH_NONCES[nonce_key] = expires_at
			marked_nonce = nonce_key

		sent_media_message = await _send_media_by_type(message, data)

		if flash_seconds > 0:
			asyncio.create_task(_delete_message_later(sent_media_message, flash_seconds))

		'''
		await message.reply(
			"✅ 解码成功\n\n"
			f"token:\n{token}\n\n"
			"解析字段:\n"
			f"nonce: {data['nonce']}\n"
			f"user_id: {data['user_id']}\n"
			f"file_id: {data['file_id']}\n"
			f"file_type: {data['file_type']}\n"
			f"no_forward: {data['no_forward']}\n"
			f"flash_seconds: {data['flash_seconds']}\n"
			f"valid_until: {data['valid_until']}"
		)
		'''
	except Exception as exc:
		if marked_nonce:
			USED_FLASH_NONCES.pop(marked_nonce, None)
		await message.reply(f"❌ 解码或解析失败: {exc}")


async def main() -> None:
	global bot_name
	me = await bot.get_me()
	bot_name = str(getattr(me, "username", "") or "")
	await bot.set_my_commands(
		[BotCommand(command="start", description="开始")],
		scope=BotCommandScopeAllPrivateChats(),
	)
	await dp.start_polling(bot)


if __name__ == "__main__":
	asyncio.run(main())
