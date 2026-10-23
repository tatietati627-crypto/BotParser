import os
import re
import json
import asyncio
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID"))
STREAM_BASE_URL = os.getenv("STREAM_BASE_URL", "").rstrip("/")
DELAY = int(os.getenv("DELAY_BETWEEN_REQUESTS", 4))

DB_FILE = "movies.json"

# Клиент юзербота (сессия сохранится в файл user_session)
user_client = TelegramClient("user_session", API_ID, API_HASH)

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def parse_metadata(text):
    """
    Парсит kp, imdb и год из текста кнопки или описания.
    Пример: 'сериал | kp:8.003 | imdb:7.6 | 2023'
    """
    data = {"kp": None, "imdb": None, "year": None}
    
    kp_match = re.search(r"kp:([\d\.]+)", text, re.IGNORECASE)
    if kp_match:
        try:
            data["kp"] = float(kp_match.group(1))
        except ValueError:
            pass

    imdb_match = re.search(r"imdb:([\d\.]+)", text, re.IGNORECASE)
    if imdb_match:
        try:
            data["imdb"] = float(imdb_match.group(1))
        except ValueError:
            pass

    year_match = re.search(r"\b(19\d\d|20\d\d)\b", text)
    if year_match:
        data["year"] = int(year_match.group(1))

    return data

async def fetch_movie(movie_title: str):
    """Ищет фильм в @kinosmotrubot и отправляет в целевой канал."""
    bot_username = "kinosmotrubot"
    
    try:
        # 1. Инлайн-поиск
        results = await user_client.inline_query(bot_username, movie_title)
        if not results:
            return False, f"Ничего не найдено по запросу: {movie_title}"

        first_result = results[0]
        parsed_info = parse_metadata(f"{first_result.title} {first_result.description}")

        # 2. Отправляем инлайн-результат себе в 'Saved Messages' (me), чтобы получить видео
        sent_msg = await first_result.click("me")
        
        # Ждем 3 секунды, пока бот отдаст медиафайл
        await asyncio.sleep(3)
        
        # Обновляем объект сообщения (чтобы получить прикрепленное видео)
        fresh_msg = await user_client.get_messages("me", ids=sent_msg.id)

        # 3. Пересылаем в целевой канал-хранилище
        forwarded = await user_client.forward_messages(TARGET_CHANNEL_ID, fresh_msg)
        channel_msg_id = forwarded.id

        # 4. Формируем запись
        movie_entry = {
            "title": first_result.title,
            "year": parsed_info["year"],
            "kp": parsed_info["kp"],
            "imdb": parsed_info["imdb"],
            "channel_message_id": channel_msg_id,
            "stream_url": f"{STREAM_BASE_URL}/stream/{channel_msg_id}/video.mp4"
        }

        # 5. Сохраняем в JSON
        current_db = load_db()
        current_db.append(movie_entry)
        save_db(current_db)

        # Удаляем временное сообщение из Избранного, чтобы не засорять
        await fresh_msg.delete()

        return True, f"Успешно: {first_result.title} (ID сообщения: {channel_msg_id})"

    except FloodWaitError as e:
        await asyncio.sleep(e.seconds)
        return False, f"FloodWait: ждали {e.seconds} сек."
    except Exception as e:
        return False, f"Ошибка при обработке {movie_title}: {str(e)}"
