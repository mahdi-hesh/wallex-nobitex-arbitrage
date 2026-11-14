import os, requests
token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not token:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN first.")
data = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json()
if not data.get("ok"):
    raise SystemExit(data)
for item in data.get("result", []):
    chat = item.get("message", {}).get("chat") or item.get("channel_post", {}).get("chat")
    if chat:
        print("chat_id:", chat.get("id"), "type:", chat.get("type"))
