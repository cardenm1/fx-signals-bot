import os
import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_message(text):
    if not TOKEN or not CHAT_ID:
        print("Ошибка: не заданы TELEGRAM_TOKEN или TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        response = requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=10)
        response.raise_for_status()
        print("Сообщение отправлено")
    except Exception as e:
        print(f"Ошибка отправки: {e}")

if __name__ == "__main__":
    send_message("✅ Тест связи из GitHub Actions. Бот работает.")
