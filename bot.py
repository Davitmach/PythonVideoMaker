import os
import time
import base64
import jwt
import logging
import requests
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")  # Токен твоего бота
ACCESS_KEY = os.getenv("ACCESS_KEY")  # Твой Access Key от Kling AI
SECRET_KEY = os.getenv("SECRET_KEY")  # Твой Secret Key от Kling AI

API_BASE = "https://api-singapore.klingai.com"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO
)

user_state = {}

def generate_jwt(ak: str, sk: str) -> str:
    now = int(time.time())
    payload = {
        "iss": ak,
        "exp": now + 1800,
        "nbf": now - 5
    }
    headers = {"alg": "HS256", "typ": "JWT"}
    token = jwt.encode(payload, sk, algorithm="HS256", headers=headers)
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token.strip()

def encode_file_to_base64(file_path: str) -> str:
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

def create_video_task(image_base64: str, prompt: str, token: str) -> str:
    url = f"{API_BASE}/v1/videos/image2video"
    
    # Вариант 1: с Bearer
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # Если с Bearer не работает, попробуй раскомментировать и использовать вариант 2:
    # headers = {
    #     "Authorization": token,
    #     "Content-Type": "application/json"
    # }
    
    json_data = {
        "model_name": "kling-v1-6",
        "mode": "pro",
        "duration": "5",
        "image": image_base64,
        "prompt": prompt,
        "cfg_scale": 0.5
    }
    resp = requests.post(url, headers=headers, json=json_data, timeout=60)
    if resp.status_code != 200:
        logging.error(f"Ошибка API при создании задачи: {resp.status_code} {resp.text}")
        resp.raise_for_status()
    data = resp.json()
    if "data" not in data or "task_id" not in data["data"]:
        raise RuntimeError(f"Не удалось получить task_id из ответа API: {resp.text}")
    return data["data"]["task_id"]

def poll_task_status(task_id: str, token: str, timeout: int = 300) -> str | None:
    url = f"{API_BASE}/v1/videos/image2video/{task_id}"
    
    # Вариант 1: с Bearer
    headers = {
        "Authorization": f"Bearer {token}"
    }
    # Если с Bearer не работает, попробуй раскомментировать и использовать вариант 2:
    # headers = {
    #     "Authorization": token
    # }
    
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            logging.error(f"Ошибка API при опросе задачи: {resp.status_code} {resp.text}")
            resp.raise_for_status()
        data = resp.json().get("data", {})
        status = data.get("task_status") or data.get("status")
        if status and status.lower() == "succeed":
            videos = data.get("task_result", {}).get("videos")
            if videos and isinstance(videos, list) and len(videos) > 0:
                return videos[0].get("url")
            return None
        elif status and status.lower() == "failed":
            raise RuntimeError("Задача завершилась с ошибкой на стороне Kling AI")
        time.sleep(5)
    raise TimeoutError("Превышено время ожидания результата задачи")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь мне фото, а потом текст с описанием (prompt) — я создам видео через Kling AI."
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    file_path = f"temp_{chat_id}.jpg"
    await file.download_to_drive(file_path)

    image_base64 = encode_file_to_base64(file_path)
    os.remove(file_path)

    user_state[chat_id] = {"image_base64": image_base64}
    await update.message.reply_text("Фото получено! Теперь отправь описание (prompt) для видео.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = user_state.get(chat_id)

    if not state or "image_base64" not in state:
        await update.message.reply_text("Сначала отправь фото, пожалуйста.")
        return

    prompt = update.message.text.strip()
    image_base64 = state["image_base64"]

    await update.message.reply_text("Создаю видео... Подожди немного.")

    token = generate_jwt(ACCESS_KEY, SECRET_KEY)
    logging.info(f"Сгенерирован JWT токен: {token}")

    try:
        print(token,'JWT')
        task_id = await asyncio.to_thread(create_video_task, image_base64, prompt, token)
    except Exception as e:
        logging.exception("Ошибка создания задачи")
        await update.message.reply_text(f"Ошибка при создании задачи: {e}")
        return

    await update.message.reply_text(f"Задача создана. Жду результат...")

    async def wait_and_send():
        try:
            video_url = await asyncio.to_thread(poll_task_status, task_id, token)
            if video_url:
                await context.bot.send_video(chat_id, video=video_url, caption="Вот твоё видео 🎬")
            else:
                await context.bot.send_message(chat_id, text="Видео готово, но URL получить не удалось.")
        except Exception as e:
            logging.exception("Ошибка при опросе задачи")
            await context.bot.send_message(chat_id, text=f"Ошибка при обработке задачи: {e}")

    asyncio.create_task(wait_and_send())
    user_state.pop(chat_id, None)

def main():
    if not BOT_TOKEN or not ACCESS_KEY or not SECRET_KEY:
        print("Ошибка: BOT_TOKEN, ACCESS_KEY и SECRET_KEY должны быть заданы в .env файле")
        return
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling()

if __name__ == "__main__":
    main()

