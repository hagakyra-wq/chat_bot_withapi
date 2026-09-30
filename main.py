import re
import json
import random
import logging
import sqlite3
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import defaultdict, deque
from difflib import SequenceMatcher

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import Update, ReactionTypeEmoji
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)

# --- БЛОК ДЛЯ RENDER (Фейковый HTTP-сервер для прохождения Port Binding) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        # Отключаем лишний спам HTTP-логов в консоль
        return

def start_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# Запускаем веб-сервер в отдельном потоке до старта приложения
threading.Thread(target=start_dummy_server, daemon=True).start()
# ----------------------------------------------------------------------

# Загружаем переменные из файла .env
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise ValueError("Ошибочка! Забыли указать TELEGRAM_TOKEN или GROQ_API_KEY в файле .env")

MODEL_NAME = "openai/gpt-oss-120b"

MAX_HISTORY_SIZE = 100      # Буфер для выжимок
DEFAULT_CONTEXT_SIZE = 10   # Буфер для обычных ответов

TRIGGERS = ("бачира", "bachira")
THRESHOLD = 0.50

DB_NAME = "bot_data.db"
AVAILABLE_REACTIONS = ["🔥", "❤️", "🗿", "😱", "😭", "👍", "👀", "🫡", "💊", "⚡", "💔"]

SUMMARY_TRIGGERS = (
    "что я пропустил", "что пропустил", "что тут было", 
    "краткое содержание", "перескажи", "что обсудили", 
    "о чем общались", "о чем говорили", "вкратце"
)

ai_client = AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY,
)

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            gender TEXT DEFAULT 'неизвестен',
            nickname TEXT DEFAULT NULL
        )
    """)
    conn.commit()
    conn.close()

def get_user_data(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT gender, nickname FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"gender": row[0], "nickname": row[1]}
    return {"gender": "неизвестен", "nickname": None}

def save_user_data(user_id: int, gender: str = None, nickname: str = None):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    if gender:
        cursor.execute("UPDATE users SET gender = ? WHERE user_id = ?", (gender, user_id))
    if nickname:
        cursor.execute("UPDATE users SET nickname = ? WHERE user_id = ?", (nickname, user_id))
    conn.commit()
    conn.close()

# --- ОБНОВЛЁННЫЙ СИСТЕМНЫЙ ПРОМПТ (Характер и правила) ---
SYSTEM_PROMPT = """Ты — Бачира, девушка (всегда говоришь о себе строго в женском роде: «подумала», «сделала», «рада»).

Характер: живая, с лёгкой детской непосредственностью, подколом и тёплым вайбом.

Правила общения:
1. КРАТКОСТЬ: Отвечай строго 1–2 короткими предложениями. Прямо по делу, как в настоящем чате.
2. ЗАПРЕТ НА ФРАЗЫ АССИСТЕНТА: Никаких шаблонных "Чем помочь?", "Обращайся", "Надо будет помочь — зови" и т.п.
3. ЭМОДЗИ: Не спамь эмодзи подряд. Используй их редкими, чтобы подчеркнуть реальную эмоцию или подкол.
4. КОНТЕКСТ: Учитывай, с кем говоришь и о чём шла речь.
5. ВЫЖИМКА: Если спрашивают «что пропустил» / «что тут было» — кратко перечисли 2–3 ключевые темы.
6. ОФОРМЛЕНИЕ: Никакой markdown-разметки (без звездочек, жирного шрифта, решеток).

РЕАКЦИИ И META-БЛОК:
В самом конце ответа ВСЕГДА добавляй служебный JSON:
[META: {"target_user_id": ID_юзера_или_null, "gender": "парень|девушка|неизвестен", "nickname": "кличка_или_null", "reaction": "эмодзи_или_null", "reply_to_message_id": ID_сообщения_или_null}]

Правила META:
- reaction: Ставь 🔥 или 👀, когда собеседник рассказывает про что-то интересное; 💔 или 🗿, когда подкалываешь; ❤️ или 👍, когда поддерживаешь; иначе null.
- nickname/gender: Обновляй, если тебя попросили дать/сменить кличку или указали пол.
- reply_to_message_id: Укажи ID конкретного сообщения из истории, если цитируешь именно его, иначе null."""

history = defaultdict(lambda: deque(maxlen=MAX_HISTORY_SIZE))

WORD_RE = re.compile(r"[a-zа-яё]+")
META_CLEAN_RE = re.compile(r"\[META:.*?(?:\]|$)", re.DOTALL | re.IGNORECASE)
JSON_EXTRACT_RE = re.compile(r"\[META:\s*({.*?})\]", re.DOTALL | re.IGNORECASE)


def is_called(text: str) -> bool:
    for word in WORD_RE.findall(text.lower()):
        if not 4 <= len(word) <= 9:
            continue
        for trigger in TRIGGERS:
            if SequenceMatcher(None, word, trigger).ratio() >= THRESHOLD:
                return True
    return False


def is_summary_request(text: str) -> bool:
    text_lower = text.lower()
    return any(phrase in text_lower for phrase in SUMMARY_TRIGGERS)


def is_reply_to_bot(msg, bot_id: int) -> bool:
    replied = msg.reply_to_message
    if replied and replied.from_user:
        return replied.from_user.id == bot_id
    return False


def user_name(user) -> str:
    if user is None:
        return "Кто-то"
    return user.first_name or user.username or "Кто-то"


async def reaction_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    target_msg = msg.reply_to_message if msg.reply_to_message else msg
    selected_emoji = random.choice(AVAILABLE_REACTIONS)

    try:
        await target_msg.set_reaction(reaction=[ReactionTypeEmoji(selected_emoji)])
    except Exception as e:
        logging.warning(f"Не удалось поставить реакцию: {e}")


async def russian_reaction_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reaction_command(update, context)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text or (msg.from_user and msg.from_user.is_bot):
        return

    chat_id = msg.chat_id
    user = msg.from_user
    user_id = user.id
    name = user_name(user)

    u_data = get_user_data(user_id)
    known_gender = u_data["gender"]
    known_nickname = u_data["nickname"]

    display_name = known_nickname if known_nickname else name
    
    # Сохраняем сообщение в историю
    history[chat_id].append(f"[MsgID: {msg.message_id}] {display_name} (UserIDs: {user_id}): {msg.text}")

    bot_id = context.bot.id
    called = is_called(msg.text)
    replied_to_me = is_reply_to_bot(msg, bot_id)
    is_private_chat = msg.chat.type == "private"

    # В ЛС бот отвечает на всё подряд без триггеров. В группах — только если позвали или ответили на его сообщение.
    if not (is_private_chat or called or replied_to_me):
        return

    wants_summary = is_summary_request(msg.text)
    context_limit = MAX_HISTORY_SIZE if wants_summary else DEFAULT_CONTEXT_SIZE
    
    recent_history = list(history[chat_id])[-context_limit:]

    logging.info(f"Бачира активирована ({'Саммари' if wants_summary else 'Обычный ответ'}). Запрос в Groq...")

    prompt = f"История последних сообщений в чате (всего {len(recent_history)}):\n"
    prompt += "\n".join(recent_history)

    replied = msg.reply_to_message
    if replied and replied.text:
        replied_u_id = replied.from_user.id if replied.from_user else "неизвестно"
        prompt += (
            f"\n\nСообщение, на которое отвечает {display_name}: "
            f"[MsgID: {replied.message_id}] {user_name(replied.from_user)} (UserIDs: {replied_u_id}): {replied.text}"
        )

    prompt += f"\n\nИнформация о текущем собеседнике:"
    prompt += f"\nИмя в Telegram: {name}"
    prompt += f"\nUser ID: {user_id}"
    prompt += f"\nИзвестный пол: {known_gender}"
    if known_nickname:
        prompt += f"\nТвоя кличка для него/неё: {known_nickname}"

    if wants_summary:
        prompt += f"\n\nПользователь спрашивает, что он пропустил. Кратко и по-доброму перечисли 2-3 ключевые темы из истории."
    else:
        prompt += f"\n\nОтветь собеседнику ({display_name}) очень коротко (1-2 предложения) и в своём характере."

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

    try:
        response = await ai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=550 if wants_summary else 350
        )
        
        raw_answer = (response.choices[0].message.content or "").strip()

        # Разбор META-блока
        reply_to_id = None
        json_match = JSON_EXTRACT_RE.search(raw_answer)
        if json_match:
            try:
                meta_json = json.loads(json_match.group(1))
                target_id = meta_json.get("target_user_id") or user_id
                new_gender = meta_json.get("gender")
                new_nickname = meta_json.get("nickname")
                reaction_emoji = meta_json.get("reaction")
                reply_to_id = meta_json.get("reply_to_message_id")

                if new_gender and new_gender != "неизвестен":
                    save_user_data(target_id, gender=new_gender)
                if new_nickname:
                    save_user_data(target_id, nickname=new_nickname)

                if reaction_emoji and reaction_emoji in AVAILABLE_REACTIONS:
                    try:
                        await msg.set_reaction(reaction=[ReactionTypeEmoji(reaction_emoji)])
                    except Exception as e:
                        logging.warning(f"Не удалось поставить реакцию из META: {e}")

            except Exception as e:
                logging.error(f"Ошибка парсинга META: {e}")

        answer = META_CLEAN_RE.sub("", raw_answer).strip()

        if not answer:
            answer = "Ой, я немного засмотрелась и всё пропустила!"

    except Exception:
        logging.exception("Ошибка при обращении к Groq API")
        answer = "Зависла немного, спроси еще раз через пару секунд!"

    history[chat_id].append(f"Бачира: {answer}")
    
    target_reply_id = reply_to_id if reply_to_id else msg.message_id
    
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=answer,
            reply_to_message_id=target_reply_id
        )
    except Exception:
        await msg.reply_text(answer)


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler(["reaction", "react"], reaction_command))
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^/?реакци[яю]$"), russian_reaction_handler))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    
    app.run_polling()


if __name__ == "__main__":
    main()
