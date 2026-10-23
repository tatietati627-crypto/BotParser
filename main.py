import os
import re
import json
import asyncio
from aiohttp import web
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError

# ================= КОНФИГУРАЦИЯ И ПЕРЕМЕННЫЕ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING", "")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))
STREAM_BASE_URL = os.getenv("STREAM_BASE_URL", "").rstrip("/")
DELAY = int(os.getenv("DELAY_BETWEEN_REQUESTS", 4))
PORT = int(os.getenv("PORT", 8080))

DB_FILE = "movies.json"

# Клиент бота-пульта
bot_client = TelegramClient("control_bot", API_ID, API_HASH)

# Клиент юзербота (создается только если есть SESSION_STRING)
user_client = None
if SESSION_STRING:
    user_client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# Состояния в памяти
auth_state = {}
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

# ================= ЛОГИКА ЮЗЕРБОТА =================
async def fetch_movie(movie_title: str):
    global user_client
    if not user_client or not user_client.is_connected():
        return False, "Юзербот не подключен! Проверь SESSION_STRING."

    try:
        results = await user_client.inline_query("kinosmotrubot", movie_title)
        if not results:
            return False, f"Ничего не найдено по запросу: {movie_title}"

        first_result = results[0]
        meta = parse_metadata(f"{first_result.title} {first_result.description}")

        sent_msg = await first_result.click("me")
        await asyncio.sleep(3)

        fresh_msg = await user_client.get_messages("me", ids=sent_msg.id)
        forwarded = await user_client.forward_messages(TARGET_CHANNEL_ID, fresh_msg)
        channel_msg_id = forwarded.id

        entry = {
            "title": first_result.title,
            "year": meta["year"],
            "kp": meta["kp"],
            "imdb": meta["imdb"],
            "channel_message_id": channel_msg_id,
            "stream_url": f"{STREAM_BASE_URL}/stream/{channel_msg_id}/video.mp4"
        }

        db = load_db()
        db.append(entry)
        save_db(db)

        await fresh_msg.delete()
        return True, f"Успешно сохранен: {first_result.title} (ID: {channel_msg_id})"

    except FloodWaitError as e:
        await asyncio.sleep(e.seconds)
        return False, f"FloodWait от Telegram: пауза {e.seconds} сек."
    except Exception as e:
        return False, f"Ошибка при сборе '{movie_title}': {str(e)}"

async def run_worker():
    global is_running, movie_queue
    while is_running and movie_queue:
        title = movie_queue.pop(0)
        await bot_client.send_message(ADMIN_ID, f"⏳ Запрашиваю: **{title}**...")
        
        success, log_msg = await fetch_movie(title)
        await bot_client.send_message(ADMIN_ID, f"📌 {log_msg}")

        await asyncio.sleep(DELAY)

    is_running = False
    await bot_client.send_message(ADMIN_ID, "✅ Очередь обработана или сбор остановлен!", buttons=get_keyboard())

# ================= КЛАВИАТУРА И ХЕНДЛЕРЫ БОТА =================
def get_keyboard():
    return [
        [Button.inline("▶️ Старт", b"start_work"), Button.inline("⏹ Стоп", b"stop_work")],
        [Button.inline("📊 Прогресс", b"status"), Button.inline("📁 База JSON", b"get_db")]
    ]

@bot_client.on(events.NewMessage(chats=ADMIN_ID))
async def handle_admin_messages(event):
    global movie_queue, auth_state
    text = event.raw_text.strip()
    chat_id = event.chat_id

    # 1. Авторизация аккаунта прямо из чата
    if text == "/login":
        auth_state[chat_id] = {
            "step": "phone",
            "client": TelegramClient(StringSession(), API_ID, API_HASH)
        }
        await auth_state[chat_id]["client"].connect()
        await event.reply("📱 **Шаг 1/3:** Отправь номер телефона в международном формате (например, `+380XXXXXXXXX`):")
        return

    if chat_id in auth_state:
        state = auth_state[chat_id]
        client = state["client"]

        if state["step"] == "phone":
            phone = text.replace(" ", "")
            state["phone"] = phone
            try:
                sent = await client.send_code_request(phone)
                state["phone_code_hash"] = sent.phone_code_hash
                state["step"] = "code"
                await event.reply("📩 **Шаг 2/3:** Код выслан в Telegram! Пришли его (лучше через пробелы, например: `1 2 3 4 5`):")
            except Exception as e:
                await event.reply(f"❌ Ошибка отправки кода: {e}\nНачни заново: /login")
                del auth_state[chat_id]
            return

        elif state["step"] == "code":
            code = text.replace(" ", "").replace("-", "")
            try:
                await client.sign_in(phone=state["phone"], code=code, phone_code_hash=state["phone_code_hash"])
                session_str = client.session.save()
                await event.reply(
                    "🎉 **Авторизация прошла успешно!**\n\n"
                    "Скопируй строку ниже и вставь в переменную `SESSION_STRING` на Render:\n\n"
                    f"`{session_str}`"
                )
                del auth_state[chat_id]
            except SessionPasswordNeededError:
                state["step"] = "password"
                await event.reply("🔒 **Шаг 3/3:** Включена двухфакторная защита (2FA). Пришли пароль от аккаунта:")
            except Exception as e:
                await event.reply(f"❌ Ошибка кода: {e}\nПопробуй заново: /login")
                del auth_state[chat_id]
            return

        elif state["step"] == "password":
            try:
                await client.sign_in(password=text)
                session_str = client.session.save()
                await event.reply(
                    "🎉 **Авторизация прошла успешно!**\n\n"
                    "Скопируй строку ниже и вставь в переменную `SESSION_STRING` на Render:\n\n"
                    f"`{session_str}`"
                )
            except Exception as e:
                await event.reply(f"❌ Ошибка пароля: {e}\nПопробуй заново: /login")
            finally:
                del auth_state[chat_id]
            return

    # 2. Обычные команды
    if text == "/start":
        await event.reply(
            "🎬 **Панель управления Movie Cinema**\n\n"
            "• Чтобы получить ключ сессии, отправь `/login`.\n"
            "• Чтобы добавить фильмы в очередь, просто отправь их названия списком (каждое с новой строки).",
            buttons=get_keyboard()
        )
        return

    # Добавление названий в очередь
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
        if not SESSION_STRING:
            await event.answer("Сначала настрой SESSION_STRING через команду /login!", alert=True)
            return
        if is_running:
            await event.answer("Сбор уже запущен!", alert=True)
            return
        if not movie_queue:
            await event.answer("Очередь пуста! Пришли названия фильмов текстом.", alert=True)
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
            await bot_client.send_file(ADMIN_ID, DB_FILE, caption="📁 Текущая база movies.json")
        else:
            await event.answer("База пока пуста.", alert=True)

# ================= ВЕБ-СЕРВЕР ДЛЯ RENDER WEB SERVICE =================
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
    await bot_client.start(bot_token=BOT_TOKEN)
    print("Бот управления успешно авторизован.")

    if SESSION_STRING:
        try:
            await user_client.start()
            print("Юзербот подключен по SESSION_STRING!")
        except Exception as e:
            print(f"Ошибка подключения юзербота: {e}")

    await start_web_server()
    await bot_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
