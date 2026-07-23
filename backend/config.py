import os
from pathlib import Path

def _load_env_file():
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

_load_env_file()

BRAND = "FluxVPN"
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN", "")
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6049379160"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "zrdws").lstrip("@")
REQUIRED_CHANNEL = (os.getenv("REQUIRED_CHANNEL") or "@fluxvvpn").strip()
REQUIRED_CHANNEL_URL = "https://t.me/" + REQUIRED_CHANNEL.lstrip("@")
PORT = int(os.getenv("PORT", "10000"))
REFERRAL_DAYS = 5
REFERRAL_PERCENT = 10
TRIAL_DAYS = 7
TRIAL_DEVICE_LIMIT = 2
PREMIUM_DEVICE_LIMIT = 4
FAMILY_DEVICE_LIMIT = 8
CUSTOM_MIN_DAYS = 3
CUSTOM_MAX_DAYS = 730
PLANS = {7: 50, 30: 200, 90: 400, 365: 800}
FAMILY_PLANS = {30: 350, 90: 700, 365: 1400}
TOPUP_AMOUNTS = (100, 200, 500, 1000)
INIT_DATA_MAX_AGE_SECONDS = 86400

def validate():
    missing = [name for name, value in (("BOT_TOKEN", BOT_TOKEN), ("DATABASE_URL", DATABASE_URL), ("PUBLIC_URL", PUBLIC_URL)) if not value]
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))
