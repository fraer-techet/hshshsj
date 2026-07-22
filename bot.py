import threading
import time
import traceback
from datetime import datetime, timezone

from backend import app, db
from backend.config import ADMIN_ID, validate
from backend.telegram import send


def notifications():
    while True:
        try:
            with db.session() as database:
                now=datetime.now(timezone.utc)
                rows=database.run("select u.telegram_id,u.language,s.expires_at from app_users u join app_subscriptions s using(telegram_id) where s.expires_at is not null and u.banned=false")
                for user_id,language,expires in rows:
                    if expires.tzinfo is None:expires=expires.replace(tzinfo=timezone.utc)
                    left=(expires-now).total_seconds();kind=message=None
                    if left<=0:kind,message="expired","❌ Подписка закончилась. Продли, братан — доступ вернётся сразу."
                    elif left<=3600:kind,message="1h","⚡ До конца подписки около часа. Продли заранее."
                    elif left<=86400:kind,message="1d","⏰ До конца подписки 1 день. Лучше продли сейчас."
                    elif left<=172800:kind,message="2d","⏰ До конца подписки 2 дня. Продли заранее, братан."
                    if kind:
                        created=database.run("insert into app_notification_log(telegram_id,kind,subscription_expires_at) values(:id,:kind,:expires) on conflict do nothing returning kind",id=user_id,kind=kind,expires=expires)
                        if created:
                            try:send(user_id,"<b>FluxVPN</b>\n\n"+message)
                            except Exception:pass
        except Exception:print("NOTIFICATIONS",traceback.format_exc(),flush=True)
        time.sleep(60)

def main():
    validate()
    with db.session():pass
    app.start()
    threading.Thread(target=notifications,daemon=True).start()
    print("FluxVPN Mini App started",flush=True)
    while True:time.sleep(3600)
if __name__=="__main__":main()
