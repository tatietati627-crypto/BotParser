import os
import re
import json
import asyncio
from aiohttp import web
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

# ================= КОНФИГУРАЦИЯ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))
STREAM_BASE_URL = os.getenv("STREAM_BASE_URL", "").rstrip("/")
DELAY = int(os.getenv("DELAY_BETWEEN_REQUESTS", 4))
PORT = int(os.getenv("PORT", 8080))

DB_FILE = "movies.json"

bot_client = None
user_client = None

movie_queue = []
is_running = False

# ================= РАБОТА С БАЗОЙ ДАННЫХ =================
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

# ================= ЛОГИКА ВОРКЕРА =================
async def fetch_movie(movie_title: str):
    global user_client
    if not user_client or not user_client.is_connected():
        return False, "Юзербот не подключен! Проверь SESSION_STRING."

    bot_username = "kinosmotrubot"

    try:
        # 1. Поиск фильма в инлайне
        results = await user_client.inline_query(bot_username, movie_title)
        if not results:
            return False, f"Ничего не найдено по запросу: {movie_title}"

        first_result = results[0]
        meta = parse_metadata(f"{first_result.title} {first_result.description}")

        # 2. Нажимаем на результат в Saved Messages, чтобы бот вернул код фильма (kino_...)
        sent_inline = await first_result.click("me")
        await asyncio.sleep(2)

        inline_msg = await user_client.get_messages("me", ids=sent_inline.id)
        code_text = inline_msg.raw_text.strip()
        await inline_msg.delete()

        if not code_text:
            return False, f"Не удалось получить код фильма для: {first_result.title}"

        # 3. Отправляем полученный код боту @kinosmotrubot в ЛС
        await user_client.send_message(bot_username, f"/start {code_text}")
        await asyncio.sleep(4)

        # 4. Проверяем ответ от бота (ищем видеосообщение)
        bot_msgs = await user_client.get_messages(bot_username, limit=1)
        if not bot_msgs or not (bot_msgs[0].video or bot_msgs[0].document):
            # Если бот не среагировал на команду с /start, пробуем отправить голый код
            await user_client.send_message(bot_username, code_text)
            await asyncio.sleep(4)
            bot_msgs = await user_client.get_messages(bot_username, limit=1)

        target_media_msg = bot_msgs[0]

        if not (target_media_msg.video or target_media_msg.document):
            return False, f"Бот вернул текст вместо файла видео: {target_media_msg.raw_text[:50]}"

        # 5. Пересылаем полученное видео в целевой канал
        forwarded = await user_client.forward_messages(TARGET_CHANNEL_ID, target_media_msg)
        channel_msg_id = forwarded.id

        # 6. Добавляем фильм в movies.json
        entry = {
            "title": first_result.title,
            "code": code_text,
            "year": meta["year"],
            "kp": meta["kp"],
            "imdb": meta["imdb"],
            "channel_message_id": channel_msg_id,
            "stream_url": f"{STREAM_BASE_URL}/stream/{channel_msg_id}/video.mp4"
        }

        db = load_db()
        db.append(entry)
        save_db(db)

        return True, f"Готово: {first_result.title} переслан в канал (ID: {channel_msg_id})"

    except FloodWaitError as e:
        await asyncio.sleep(e.seconds)
        return False, f"FloodWait от Telegram: пауза {e.seconds} сек."
    except Exception as e:
        return False, f"Ошибка при обработке '{movie_title}': {str(e)}"

async def run_worker():
    global is_running, movie_queue
    while is_running and movie_queue:
        title = movie_queue.pop(0)
        await bot_client.send_message(ADMIN_ID, f"⏳ Запрашиваю: **{title}**...")
        
        success, log_msg = await fetch_movie(title)
        await bot_client.send_message(ADMIN_ID, f"📌 {log_msg}")

        await asyncio.sleep(DELAY)

    is_running = False
    await bot_client.send_message(ADMIN_ID, "✅ Очередь завершена или сбор остановлен!", buttons=get_keyboard())

# ================= КЛАВИАТУРА И ХЕНДЛЕРЫ =================
def get_keyboard():
    return [
        [Button.inline("▶️ Старт", b"start_work"), Button.inline("⏹ Стоп", b"stop_work")],
        [Button.inline("📊 Прогресс", b"status"), Button.inline("📁 База JSON", b"get_db")]
    ]

def setup_handlers():
    @bot_client.on(events.NewMessage(chats=ADMIN_ID))
    async def handle_admin_messages(event):
        global movie_queue
        text = event.raw_text.strip()

        if text == "/start":
            await event.reply(
                "🎬 **Панель управления сбором фильмов**\n\n"
                "Отправь названия фильмов (каждое с новой строки), чтобы добавить их в очередь.",
                buttons=get_keyboard()
            )
            return

        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if lines:
            movie_queue.extend(lines)
            await event.reply(f"📥 Добавлено в очередь: **{len(lines)}** шт. Всего: **{len(movie_queue)}**.")

    @bot_client.on(events.CallbackQuery)
    async def handle_buttons(event):
        global is_running, movie_queue
        if event.sender_id != ADMIN_ID:
            return

        data = event.data
        if data == b"start_work":
            if is_running:
                await event.answer("Сбор уже идет!", alert=True)
                return
            if not movie_queue:
                await event.answer("Очередь пуста! Отправь названия фильмов текстом.", alert=True)
                return

            is_running = True
            await event.edit("🚀 Сбор запущен!", buttons=get_keyboard())
            asyncio.create_task(run_worker())

        elif data == b"stop_work":
            is_running = False
            await event.edit("⏹ Сбор будет остановлен после текущего фильма.", buttons=get_keyboard())

        elif data == b"status":
            await event.answer(f"В очереди: {len(movie_queue)} шт.\nСтатус: {'Работает' if is_running else 'Стоит'}", alert=True)

        elif data == b"get_db":
            if os.path.exists(DB_FILE):
                await bot_client.send_file(ADMIN_ID, DB_FILE, caption="📁 Актуальная база movies.json")
            else:
                await event.answer("База пока пуста.", alert=True)

# ================= ВЕБ-СЕРВЕР ДЛЯ RENDER =================
async def health_check(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Веб-сервер запущен на порту {PORT}")

# ================= ТОЧКА ВХОДА =================
async def main():
    global bot_client, user_client

    bot_client = TelegramClient("control_bot", API_ID, API_HASH)
    setup_handlers()
    await bot_client.start(bot_token=BOT_TOKEN)
    print("Бот управления запущен.")

    if SESSION_STRING:
        user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        await user_client.start()
        print("Юзербот подключен по SESSION_STRING!")

    await start_web_server()
    await bot_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
