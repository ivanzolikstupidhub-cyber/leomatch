from __future__ import annotations

import asyncio
import configparser
import logging
import re
import sys
import json
from pathlib import Path
from typing import Optional, Dict
from datetime import datetime

from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import Message
import openai

ROOT = Path.cwd()
CFG_PATH = ROOT / "config.ini"

if not CFG_PATH.exists():
    sys.exit("Файл config.ini не найден.")

cfg = configparser.ConfigParser()
cfg.read(CFG_PATH, encoding="utf-8")

def cfg_get(section: str, option: str, cast=str, *, required: bool = True, fallback=None):
    if not cfg.has_option(section, option):
        if required:
            sys.exit(f"Отсутствует параметр в config: [{section}] {option}")
        return fallback
    try:
        return cast(cfg.get(section, option))
    except Exception as e:
        sys.exit(f"Неверное значение config [{section}] {option}: {e}")

API_ID = cfg_get("telegram", "api_id", int)
API_HASH = cfg_get("telegram", "api_hash", str)
SESSION_NAME = cfg_get("telegram", "session", str, required=False, fallback="leo.session")
DAVING_BOT = cfg_get("telegram", "daving_bot", str).lstrip("@")
OPENAI_API_KEY = cfg_get("openai", "api_key", str)
DEFAULT_MESSAGE = cfg_get("bot", "default_message", str, required=False, fallback="Привет, как дела?")
AI_ROLE = cfg_get("bot", "ai_role", str, required=False, fallback="Ты дружелюбный и общительный парень, который ищет девушку. Ведешь себя естественно, проявляешь интерес к общению и заинтересован в знакомстве.")
AI_PROMPT = cfg_get("bot", "ai_prompt", str, required=False, fallback="Веди диалог естественно, как обычный парень в реальной жизни. Задавай вопросы, проявляй интерес к собеседнице, будь дружелюбным и открытым. Не будь слишком навязчивым, но проявляй заинтересованность.")

TRIGGER_PHRASES = [
    "взаимная симпатия",
    "ваша анкета понравилась",
    "вам понравилась",
    "симпатия",
    "понравилась кому то"
]

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "leo_bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ],
)

openai_client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

active_conversations: Dict[int, Dict] = {}
conversations_history: Dict[int, list] = {}

def make_client(session_name: str) -> Client:
    return Client(
        session_name,
        api_id=API_ID,
        api_hash=API_HASH,
    )

def is_trigger_message(text: str) -> bool:
    text_lower = text.lower()
    return any(phrase in text_lower for phrase in TRIGGER_PHRASES)

def extract_user_id_from_message(text: str) -> Optional[int]:
    patterns = [
        r'id[:\s]+(\d+)',
        r'(\d{9,})',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return int(match.group(1))
            except:
                continue
    
    return None

async def get_user_id_from_message(msg: Message) -> Optional[int]:
    if msg.reply_markup and hasattr(msg.reply_markup, 'inline_keyboard'):
        for row in msg.reply_markup.inline_keyboard:
            for button in row:
                if hasattr(button, 'user') and button.user:
                    return button.user.id
                if button.callback_data:
                    match = re.search(r'(\d{9,})', button.callback_data)
                    if match:
                        try:
                            return int(match.group(1))
                        except:
                            continue
                if button.url and 't.me' in button.url:
                    match = re.search(r't\.me/(\w+)', button.url)
                    if match and not match.group(1).startswith('+'):
                        return None
    
    text = msg.text or msg.caption or ""
    user_id = extract_user_id_from_message(text)
    if user_id:
        return user_id
    
    if msg.entities:
        for entity in msg.entities:
            if entity.type.name == "MENTION" and hasattr(entity, 'user') and entity.user:
                return entity.user.id
    
    if msg.forward_from:
        return msg.forward_from.id
    
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user.id
    
    return None

async def send_default_message(client: Client, user_id: int):
    try:
        await client.send_message(user_id, DEFAULT_MESSAGE)
        logging.info(f"Отправлено приветственное сообщение пользователю {user_id}")
        
        active_conversations[user_id] = {
            "state": "waiting_response",
            "started_at": datetime.now().isoformat()
        }
        conversations_history[user_id] = [
            {"role": "system", "content": f"{AI_ROLE}\n\n{AI_PROMPT}"},
            {"role": "assistant", "content": DEFAULT_MESSAGE}
        ]
    except FloodWait as e:
        wait = int(getattr(e, "value", getattr(e, "seconds", 60)))
        logging.warning(f"Превышен лимит запросов, ожидание {wait} сек...")
        await asyncio.sleep(wait + 2)
        await send_default_message(client, user_id)
    except Exception as e:
        logging.error(f"Не удалось отправить приветственное сообщение пользователю {user_id}: {e}")

async def get_ai_response(user_id: int, user_message: str) -> str:
    if user_id not in conversations_history:
        conversations_history[user_id] = [
            {"role": "system", "content": f"{AI_ROLE}\n\n{AI_PROMPT}"}
        ]
    
    conversations_history[user_id].append({"role": "user", "content": user_message})
    
    try:
        response = await openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=conversations_history[user_id],
            temperature=0.7,
            max_tokens=150
        )
        
        ai_message = response.choices[0].message.content.strip()
        conversations_history[user_id].append({"role": "assistant", "content": ai_message})
        
        return ai_message
    except Exception as e:
        logging.error(f"Ошибка OpenAI API: {e}")
        return "Хм, интересно... А что ты думаешь?"

async def handle_trigger_message(client: Client, msg: Message):
    user_id = await get_user_id_from_message(msg)
    
    if not user_id:
        if msg.reply_markup and hasattr(msg.reply_markup, 'inline_keyboard'):
            for row in msg.reply_markup.inline_keyboard:
                for button in row:
                    if button.url and 't.me' in button.url:
                        match = re.search(r't\.me/(\w+)', button.url)
                        if match:
                            try:
                                target_user = await client.get_users(match.group(1))
                                user_id = target_user.id
                                break
                            except:
                                pass
                    elif hasattr(button, 'user') and button.user:
                        user_id = button.user.id
                        break
                if user_id:
                    break
    
    if not user_id:
        logging.warning(f"Не удалось извлечь ID пользователя из сообщения с триггером")
        return
    
    if user_id and user_id not in active_conversations:
        await send_default_message(client, user_id)

async def handle_user_response(client: Client, msg: Message):
    user_id = msg.from_user.id if msg.from_user else None
    
    if not user_id or user_id not in active_conversations:
        return
    
    user_text = msg.text or msg.caption or ""
    if not user_text.strip():
        return
    
    logging.info(f"Пользователь {user_id} ответил: {user_text[:50]}")
    
    ai_response = await get_ai_response(user_id, user_text)
    
    try:
        await client.send_message(user_id, ai_response)
        logging.info(f"Отправлен ответ ИИ пользователю {user_id}")
    except FloodWait as e:
        wait = int(getattr(e, "value", getattr(e, "seconds", 60)))
        logging.warning(f"Превышен лимит запросов, ожидание {wait} сек...")
        await asyncio.sleep(wait + 2)
        await client.send_message(user_id, ai_response)
    except Exception as e:
        logging.error(f"Не удалось отправить ответ ИИ пользователю {user_id}: {e}")

async def message_handler(client: Client, msg: Message):
    if not msg.chat:
        return
    
    text = (msg.text or msg.caption or "").strip()
    
    if not text:
        return
    
    chat_username = getattr(msg.chat, "username", None)
    chat_id = msg.chat.id
    
    is_from_daving = False
    if chat_username and chat_username.lower() == DAVING_BOT.lower():
        is_from_daving = True
    elif msg.from_user:
        if msg.from_user.username and msg.from_user.username.lower() == DAVING_BOT.lower():
            is_from_daving = True
        elif msg.from_user.is_bot and chat_id < 0:
            try:
                chat_info = await client.get_chat(chat_id)
                if hasattr(chat_info, 'username') and chat_info.username and chat_info.username.lower() == DAVING_BOT.lower():
                    is_from_daving = True
            except:
                pass
    
    if is_from_daving and is_trigger_message(text):
        logging.info(f"Обнаружен триггер: {text[:50]}")
        await handle_trigger_message(client, msg)
        return
    
    if msg.from_user and msg.from_user.id in active_conversations:
        if not is_from_daving:
            await handle_user_response(client, msg)

async def main():
    client = make_client(SESSION_NAME)
    
    try:
        await client.start()
        me = await client.get_me()
        logging.info(f"Авторизован как {getattr(me, 'username', None)} (id={me.id})")
        
        @client.on_message()
        async def _(c: Client, m: Message):
            await message_handler(c, m)
        
        logging.info("Бот запущен, ожидание сообщений...")
        await asyncio.Event().wait()
        
    except KeyboardInterrupt:
        logging.info("Остановлено пользователем")
    finally:
        await client.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logging.exception("Критическая ошибка")
        sys.exit(1)

