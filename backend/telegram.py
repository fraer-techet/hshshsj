import json
import urllib.error
import urllib.request

from .config import BOT_TOKEN

BASE_URL = "https://api.telegram.org/bot" + BOT_TOKEN

def call(method, payload=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(BASE_URL + "/" + method, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"Telegram {method}: {error.code}: {detail}")
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method}: {result}")
    return result["result"]

def send(chat_id, message, keyboard=None):
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard is not None:
        payload["reply_markup"] = keyboard
    return call("sendMessage", payload)

def miniapp_keyboard(url):
    return {"inline_keyboard": [[{"text": "🚀 Открыть FluxVPN", "web_app": {"url": url}}]]}
