import hashlib
import hmac
import json
import time
import urllib.parse

from .config import BOT_TOKEN, INIT_DATA_MAX_AGE_SECONDS

class AuthError(Exception):
    pass

def verify_init_data(raw):
    if not raw:
        raise AuthError("Open this app from Telegram")
    values = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        raise AuthError("Missing Telegram signature")
    auth_date = int(values.get("auth_date", "0") or 0)
    if auth_date <= 0 or abs(int(time.time()) - auth_date) > INIT_DATA_MAX_AGE_SECONDS:
        raise AuthError("Telegram session expired")
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise AuthError("Invalid Telegram signature")
    try:
        user = json.loads(values.get("user", "{}"))
    except Exception as error:
        raise AuthError("Invalid Telegram user") from error
    if not user.get("id"):
        raise AuthError("Telegram user missing")
    return user
