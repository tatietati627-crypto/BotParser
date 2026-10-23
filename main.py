import os
import asyncio
from telethon import TelegramClient, events, Button
from dotenv import load_dotenv

import worker

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DELAY = int(os.getenv("DELAY_BETWEEN_REQUESTS", 4))

# Бот управления
bot_client = TelegramClient("control_bot_session", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# Очередь и статус сбора
movie_queue = []
is_running = False

def get_keyboard():
    return [
        [Button.inline("▶️ Старт", b"start_work"), Button.inline("⏹ Стоп", b"stop_work")],
        [Button.inline("📊 Прогресс", b"status"), Button.inline("📁 База JSON", b"get_db")]
    ]

@bot_client.on(events.NewMessage(chats=ADMIN_ID))
async def handle_admin_text(event):
    global movie_queue
    if event.raw_text.startswith("/"):
        if event.raw_text == "/start":
            await event.reply(
                "🎬 **Панель управления сбором фильмов**\n\n"
                "Отправь мне названия фильмов (каждое с новой строки), чтобы добавить их в очередь.",
                buttons=get_keyboard()
            )
        return

    # Добавление фильмов в очередь
    lines = [line.strip() for line in event.raw_text.split("\n") if line.strip()]
    if lines:
        movie_queue.extend(lines)
        await event.reply(f"📥 Добавлено в очередь: **{len(lines)}** шт. Всего в очереди: **{len(movie_queue)}**.")

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
            await event.answer("Очередь пуста! Пришли сначала список фильмов текстом.", alert=True)
            return

        is_running = True
        await event.edit("🚀 Сбор запущен!", buttons=get_keyboard())
        asyncio.create_task(run_worker())

    elif data == b"stop_work":
        is_running = False
        await event.edit("⏹ Сбор будет остановлен после текущего фильма.", buttons=get_keyboard())

    elif data == b"status":
        await event.answer(
            f"В очереди: {len(movie_queue)} шт.\nСтатус: {'Работает' if is_running else 'Стоит'}",
            alert=True
        )

    elif data == b"get_db":
        if os.path.exists("movies.json"):
            await bot_client.send_file(ADMIN_ID, "movies.json", caption="📁 Текущая база фильмов")
        else:
            await event.answer("База пока пуста.", alert=True)

async def run_worker():
    global is_running, movie_queue
    while is_running and movie_queue:
        title = movie_queue.pop(0)
        await bot_client.send_message(ADMIN_ID, f"⏳ Обрабатываю: **{title}**...")
        
        success, log = await worker.fetch_movie(title)
        await bot_client.send_message(ADMIN_ID, f"📌 {log}")

        await asyncio.sleep(DELAY)

    is_running = False
    await bot_client.send_message(ADMIN_ID, "✅ Очередь завершена или сбор остановлен!", buttons=get_keyboard())

async def main():
    # Запускаем юзербота (при первом запуске в консоли попросит код из Telegram)
    await worker.user_client.start()
    print("Юзербот авторизован и готов.")

    print("Бот-панель запущена...")
    await bot_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
