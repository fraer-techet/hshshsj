import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from . import db, services
from .config import PREMIUM_DEVICE_LIMIT

COUNTRIES = [("CA","🇨🇦 Канада"),("KZ","🇰🇿 Казахстан"),("JP","🇯🇵 Япония"),("SG","🇸🇬 Сингапур"),("TR","🇹🇷 Турция"),("CH","🇨🇭 Швейцария"),("ES","🇪🇸 Испания"),("IT","🇮🇹 Италия")]
KNOWLEDGE = [
    {"id":"refresh","title":"Подписка не обновляется","body":"Удалите старую подписку из клиента, заново добавьте URL из кабинета и нажмите обновление."},
    {"id":"limit","title":"Лимит устройств","body":"Откройте Кабинет → Устройства, удалите старое устройство или купите дополнительное место."},
    {"id":"connect","title":"VPN не подключается","body":"Обновите подписку, выберите другой Reality-сервер и отключите энергосбережение для VPN-клиента."},
    {"id":"expired","title":"Подписка истекла","body":"Продлите подписку или включите автопродление с баланса."},
]

def transfer_balance(dbx, sender_id, target, amount):
    amount=round(float(amount),2)
    if amount<10 or amount>5000:raise ValueError("Сумма перевода: 10–5000 ₽")
    if str(target).isdigit():target_id=int(target)
    else:
        rows=dbx.run("select telegram_id from app_users where lower(username)=lower(:name)",name=str(target).lstrip("@"))
        if not rows:raise ValueError("Пользователь не найден")
        target_id=int(rows[0][0])
    if target_id==int(sender_id):raise ValueError("Нельзя переводить себе")
    today=float(dbx.run("select coalesce(-sum(amount),0) from app_balance_transactions where telegram_id=:id and kind='transfer_sent' and created_at>=date_trunc('day',now())",id=sender_id)[0][0] or 0)
    if today+amount>5000:raise ValueError("Суточный лимит переводов — 5000 ₽")
    try:
        dbx.run("begin")
        db.balance_change(dbx,sender_id,-amount,"transfer_sent",f"Transfer to {target_id}")
        db.balance_change(dbx,target_id,amount,"transfer_received",f"Transfer from {sender_id}")
        db.security_event(dbx,sender_id,"balance_transfer",json.dumps({"to":target_id,"amount":amount}))
        dbx.run("commit")
    except Exception:
        try:dbx.run("rollback")
        except Exception:pass
        raise
    return {"target":target_id,"amount":amount}

def freeze_subscription(dbx,user_id,days):
    days=int(days)
    if not 1<=days<=14:raise ValueError("Заморозка: 1–14 дней")
    user=db.get_user(dbx,user_id)
    if not db.active(user) or user.get("status")=="trial":raise ValueError("Заморозка доступна только Premium")
    used=int(user.get("freeze_days_used") or 0)
    if used+days>14:raise ValueError("Лимит заморозки — 14 дней")
    now=datetime.now(timezone.utc);until=now+timedelta(days=days);expires=user["expires_at"]+timedelta(days=days)
    dbx.run("update app_subscriptions set frozen_until=:until,expires_at=:expires,freeze_days_used=freeze_days_used+:days where telegram_id=:id",until=until,expires=expires,days=days,id=user_id)
    db.security_event(dbx,user_id,"subscription_frozen",json.dumps({"days":days}))
    return {"frozenUntil":until,"expiresAt":expires}

def buy_extra_device(dbx,user_id,count=1):
    count=int(count)
    if count not in (1,2):raise ValueError("Можно купить 1 или 2 места")
    cost=50 if count==1 else 90
    user=db.get_user(dbx,user_id)
    if not db.active(user):raise ValueError("Нужна активная подписка")
    if int(user.get("extra_devices") or 0)+count>4:raise ValueError("Максимум 4 дополнительных устройства")
    try:
        dbx.run("begin");db.balance_change(dbx,user_id,-cost,"extra_device",f"Extra devices +{count}")
        dbx.run("update app_subscriptions set extra_devices=extra_devices+:count,device_limit=device_limit+:count where telegram_id=:id",count=count,id=user_id);dbx.run("commit")
    except Exception:
        try:dbx.run("rollback")
        except Exception:pass
        raise
    return {"count":count,"cost":cost}

def claim_task(dbx,user,task_id,channel_member=False):
    rows=dbx.run("select id,code,reward_kind,reward_value,condition_type from app_bonus_tasks where id=:id and active=true",id=int(task_id))
    if not rows:raise ValueError("Задание не найдено")
    task,code,kind,value,condition=rows[0];uid=int(user["telegram_id"])
    if dbx.run("select 1 from app_bonus_completions where task_id=:task and telegram_id=:id",task=task,id=uid):raise ValueError("Награда уже получена")
    ok=channel_member if condition=="channel" else bool(dbx.run("select 1 from app_orders where telegram_id=:id and status='paid' and kind='premium'",id=uid)) if condition=="first_purchase" else bool(dbx.run("select 1 from app_referral_rewards where inviter_id=:id and kind='purchase'",id=uid))
    if not ok:raise ValueError("Условие ещё не выполнено")
    if kind=="balance":db.balance_change(dbx,uid,float(value),"bonus_task",code)
    else:db.extend_subscription(dbx,uid,int(value),"Bonus",user.get("device_limit") or PREMIUM_DEVICE_LIMIT)
    dbx.run("insert into app_bonus_completions(task_id,telegram_id) values(:task,:id)",task=task,id=uid)
    return {"kind":kind,"value":float(value)}

def probe_uri(config,timeout=2.5):
    try:
        parsed=urlsplit(config);host=parsed.hostname;port=parsed.port
        if not host or not port:return None
        started=time.monotonic()
        with socket.create_connection((host,port),timeout=timeout):pass
        return max(1,int((time.monotonic()-started)*1000))
    except Exception:return -1

def monitor_once():
    with db.session() as dbx:rows=dbx.run("select id,config,failure_count from app_servers order by id")
    def check(row):
        server_id,config,failures=row;return server_id,probe_uri(config),int(failures or 0)
    with ThreadPoolExecutor(max_workers=32) as pool:results=list(pool.map(check,rows))
    with db.session() as dbx:
        for server_id,latency,failures in results:
            if latency is None:continue
            if latency>=0:dbx.run("update app_servers set health_status='online',latency_ms=:latency,failure_count=0,last_checked_at=now(),enabled=true where id=:id",latency=latency,id=server_id)
            else:
                failures+=1;dbx.run("update app_servers set health_status=:status,latency_ms=null,failure_count=:failures,last_checked_at=now(),enabled=case when :failures>=3 then false else enabled end where id=:id",status="offline" if failures>=3 else "degraded",failures=failures,id=server_id)
    return len(results)

def audience_sql(audience):
    if audience=="premium":return "select u.telegram_id from app_users u join app_subscriptions s using(telegram_id) where u.banned=false and s.expires_at>now()"
    if audience=="expired":return "select u.telegram_id from app_users u join app_subscriptions s using(telegram_id) where u.banned=false and (s.expires_at is null or s.expires_at<=now())"
    if audience=="trial":return "select u.telegram_id from app_users u join app_subscriptions s using(telegram_id) where u.banned=false and s.status='trial'"
    if audience=="autorenew_low":return "select u.telegram_id from app_users u join app_subscriptions s using(telegram_id) where u.banned=false and s.auto_renew=true and u.balance<200"
    return "select telegram_id from app_users where banned=false"
