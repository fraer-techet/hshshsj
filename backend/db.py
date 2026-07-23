import secrets
import threading
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pg8000.native

from .config import DATABASE_URL, REFERRAL_DAYS, REFERRAL_PERCENT

MIGRATIONS = [
(1, "core", [
"create table if not exists app_users(telegram_id bigint primary key,username text,first_name text,last_name text,language text not null default 'ru',balance numeric(14,2) not null default 0,banned boolean not null default false,ban_reason text,referral_code text not null unique,referred_by bigint,referral_count integer not null default 0,referral_earned numeric(14,2) not null default 0,discount_percent numeric(5,2) not null default 0,created_at timestamptz not null default now(),updated_at timestamptz not null default now())",
"create table if not exists app_subscriptions(telegram_id bigint primary key references app_users(telegram_id) on delete cascade,status text not null default 'free',plan text not null default 'Free',expires_at timestamptz,trial_used boolean not null default false,sub_token text not null unique,created_at timestamptz not null default now(),updated_at timestamptz not null default now())",
"create table if not exists app_servers(id bigserial primary key,name text not null,config text not null,enabled boolean not null default true,sort_order integer not null default 0,created_at timestamptz not null default now(),updated_at timestamptz not null default now())",
"create table if not exists app_devices(id bigserial primary key,telegram_id bigint not null references app_users(telegram_id) on delete cascade,device_hash text not null,device_name text not null,user_agent text,last_ip text,blocked boolean not null default false,created_at timestamptz not null default now(),last_seen timestamptz not null default now(),unique(telegram_id,device_hash))",
]),
(2, "commerce", [
"create table if not exists app_orders(id bigserial primary key,telegram_id bigint not null references app_users(telegram_id),kind text not null default 'premium',days integer not null default 0,base_amount numeric(14,2) not null,discount_amount numeric(14,2) not null default 0,amount numeric(14,2) not null,method text not null,status text not null default 'pending',promo_code text,external_id text unique,created_at timestamptz not null default now(),updated_at timestamptz not null default now())",
"create table if not exists app_payments(id bigserial primary key,order_id bigint references app_orders(id),telegram_id bigint not null references app_users(telegram_id),kind text not null,amount numeric(14,2) not null,method text not null,external_id text unique,created_at timestamptz not null default now())",
"create table if not exists app_balance_transactions(id bigserial primary key,telegram_id bigint not null references app_users(telegram_id),amount numeric(14,2) not null,kind text not null,description text not null,related_order_id bigint references app_orders(id),created_at timestamptz not null default now())",
"create table if not exists app_promos(code text primary key,kind text not null,value numeric(14,2) not null,max_uses integer not null default 100,used_count integer not null default 0,active boolean not null default true,expires_at timestamptz,min_amount numeric(14,2) not null default 0,new_users_only boolean not null default false,created_at timestamptz not null default now(),updated_at timestamptz not null default now())",
"create table if not exists app_promo_redemptions(code text not null references app_promos(code),telegram_id bigint not null references app_users(telegram_id),order_id bigint references app_orders(id),created_at timestamptz not null default now(),primary key(code,telegram_id))",
"create table if not exists app_referral_rewards(id bigserial primary key,inviter_id bigint not null references app_users(telegram_id),referred_id bigint not null references app_users(telegram_id),order_id bigint references app_orders(id),kind text not null,amount numeric(14,2) not null default 0,days integer not null default 0,created_at timestamptz not null default now(),unique(referred_id,order_id,kind))",
]),
(3, "support_admin", [
"create table if not exists app_tickets(id bigserial primary key,telegram_id bigint not null references app_users(telegram_id),category text not null default 'other',subject text not null,status text not null default 'open',created_at timestamptz not null default now(),updated_at timestamptz not null default now())",
"create table if not exists app_ticket_messages(id bigserial primary key,ticket_id bigint not null references app_tickets(id) on delete cascade,sender_id bigint not null,sender_role text not null,body text not null,created_at timestamptz not null default now())",
"create table if not exists app_admin_audit(id bigserial primary key,admin_id bigint not null,action text not null,target_type text not null,target_id text,details text,created_at timestamptz not null default now())",
"create table if not exists app_notification_log(telegram_id bigint not null,kind text not null,subscription_expires_at timestamptz not null,created_at timestamptz not null default now(),primary key(telegram_id,kind,subscription_expires_at))",
]),
(4, "indexes", [
"create index if not exists app_orders_user_idx on app_orders(telegram_id,created_at desc)",
"create index if not exists app_orders_status_idx on app_orders(status,created_at desc)",
"create index if not exists app_payments_created_idx on app_payments(created_at desc)",
"create index if not exists app_tickets_status_idx on app_tickets(status,updated_at desc)",
"create index if not exists app_devices_user_idx on app_devices(telegram_id,blocked,last_seen desc)",
]),
(5, "server_catalog", [
"alter table app_servers add column if not exists seed_key text",
"create unique index if not exists app_servers_seed_key_uq on app_servers(seed_key)",
]),
(8, "json_profiles", [
"create table if not exists app_json_configs(id bigserial primary key,name text not null,config text not null,enabled boolean not null default true,sort_order integer not null default 0,seed_key text unique,created_at timestamptz not null default now(),updated_at timestamptz not null default now())",
]),
(10, "remove_json_profiles", [
"drop table if exists app_json_configs",
]),
(13, "devices_gifts_family_autorenew", [
"alter table app_subscriptions add column if not exists device_limit integer not null default 4",
"alter table app_subscriptions add column if not exists auto_renew boolean not null default false",
"alter table app_subscriptions add column if not exists auto_renew_days integer not null default 30",
"alter table app_subscriptions add column if not exists auto_renew_plan text not null default 'premium'",
"alter table app_orders add column if not exists plan_code text not null default 'premium'",
"create table if not exists app_gifts(token text primary key,creator_id bigint not null references app_users(telegram_id),kind text not null,value numeric(14,2) not null,cost numeric(14,2) not null,status text not null default 'pending',claimed_by bigint references app_users(telegram_id),created_at timestamptz not null default now(),claimed_at timestamptz,expires_at timestamptz not null default now()+interval '7 days')",
"create index if not exists app_gifts_creator_idx on app_gifts(creator_id,created_at desc)",
"create index if not exists app_gifts_status_idx on app_gifts(status,expires_at)",
]),
(15, "operations_security_growth", [
"alter table app_servers add column if not exists health_status text not null default 'unknown'",
"alter table app_servers add column if not exists latency_ms integer",
"alter table app_servers add column if not exists failure_count integer not null default 0",
"alter table app_servers add column if not exists last_checked_at timestamptz",
"alter table app_devices add column if not exists approved boolean not null default true",
"alter table app_devices add column if not exists approved_at timestamptz",
"alter table app_subscriptions add column if not exists frozen_until timestamptz",
"alter table app_subscriptions add column if not exists freeze_days_used integer not null default 0",
"alter table app_subscriptions add column if not exists extra_devices integer not null default 0",
"alter table app_gifts add column if not exists message text",
"alter table app_gifts add column if not exists anonymous boolean not null default false",
"alter table app_gifts add column if not exists recipient_id bigint",
"alter table app_gifts add column if not exists cancelled_at timestamptz",
"create table if not exists app_security_events(id bigserial primary key,telegram_id bigint not null references app_users(telegram_id),kind text not null,details text,ip text,created_at timestamptz not null default now())",
"create index if not exists app_security_events_user_idx on app_security_events(telegram_id,created_at desc)",
"create table if not exists app_broadcasts(id bigserial primary key,admin_id bigint not null,audience text not null default 'all',message text not null,button_text text,button_url text,scheduled_at timestamptz not null,status text not null default 'scheduled',sent integer not null default 0,failed integer not null default 0,created_at timestamptz not null default now(),finished_at timestamptz)",
"create index if not exists app_broadcasts_due_idx on app_broadcasts(status,scheduled_at)",
"create table if not exists app_news(id bigserial primary key,title text not null,body text not null,button_text text,button_url text,published boolean not null default true,created_at timestamptz not null default now())",
"create table if not exists app_bonus_tasks(id bigserial primary key,code text not null unique,title text not null,reward_kind text not null,reward_value numeric(14,2) not null,condition_type text not null,active boolean not null default true,created_at timestamptz not null default now())",
"create table if not exists app_bonus_completions(task_id bigint not null references app_bonus_tasks(id),telegram_id bigint not null references app_users(telegram_id),created_at timestamptz not null default now(),primary key(task_id,telegram_id))",
"create table if not exists app_country_votes(telegram_id bigint not null references app_users(telegram_id),country_code text not null,country_name text not null,created_at timestamptz not null default now(),primary key(telegram_id,country_code))",
"insert into app_bonus_tasks(code,title,reward_kind,reward_value,condition_type) values('channel','Подписка на канал FluxVPN','balance',10,'channel'),('first_purchase','Первая покупка Premium','days',2,'first_purchase'),('referral_purchase','Друг совершил покупку','balance',20,'referral_purchase') on conflict(code) do nothing",
]),
(16, "incident_status", [
"create table if not exists app_runtime_state(key text primary key,value text,updated_at timestamptz not null default now())",
]),
]

def connection():
    parsed=urllib.parse.urlparse(DATABASE_URL)
    return pg8000.native.Connection(user=urllib.parse.unquote(parsed.username or ""),password=urllib.parse.unquote(parsed.password or ""),host=parsed.hostname,port=parsed.port or 5432,database=(parsed.path or "/neondb").lstrip("/"),ssl_context=True)

_MIGRATED=False
_MIGRATION_LOCK=threading.Lock()

@contextmanager
def session():
    global _MIGRATED
    database=connection()
    try:
        if not _MIGRATED:
            with _MIGRATION_LOCK:
                if not _MIGRATED:
                    migrate(database);_MIGRATED=True
        yield database
    finally:
        database.close()

def migrate(db):
    db.run("create table if not exists app_schema_migrations(version integer primary key,name text not null,applied_at timestamptz not null default now())")
    applied={int(row[0]) for row in db.run("select version from app_schema_migrations")}
    for version,name,statements in MIGRATIONS:
        if version in applied:continue
        try:
            db.run("begin")
            for statement in statements:db.run(statement)
            db.run("insert into app_schema_migrations(version,name) values(:version,:name)",version=version,name=name)
            db.run("commit")
        except Exception:
            try:db.run("rollback")
            except Exception:pass
            raise
    import_legacy(db)
    seed_server_catalog(db)
    seed_server_catalog_2(db)
    seed_server_catalog_3(db)

def seed_server_catalog(db):
    marker = 6
    if db.run("select 1 from app_schema_migrations where version=:version", version=marker):
        return
    from .server_catalog import SERVER_CATALOG
    try:
        db.run("begin")
        for sort_order, (seed_key, name, config) in enumerate(SERVER_CATALOG, start=100):
            db.run(
                "insert into app_servers(name,config,enabled,sort_order,seed_key) "
                "select :name,:config,true,:sort,:key "
                "where not exists (select 1 from app_servers where seed_key=:key or config=:config)",
                name=name, config=config, sort=sort_order, key=seed_key,
            )
        db.run("insert into app_schema_migrations(version,name) values(:version,'server_catalog_data')", version=marker)
        db.run("commit")
    except Exception:
        try:
            db.run("rollback")
        except Exception:
            pass
        raise

def seed_server_catalog_2(db):
    marker = 7
    if db.run("select 1 from app_schema_migrations where version=:version", version=marker):
        return
    from .server_catalog_2 import SERVER_CATALOG_2
    try:
        db.run("begin")
        for sort_order, (seed_key, name, config) in enumerate(SERVER_CATALOG_2, start=1000):
            db.run(
                "insert into app_servers(name,config,enabled,sort_order,seed_key) "
                "select :name,:config,true,:sort,:key "
                "where not exists (select 1 from app_servers where seed_key=:key or config=:config)",
                name=name, config=config, sort=sort_order, key=seed_key,
            )
        db.run("insert into app_schema_migrations(version,name) values(:version,'server_catalog_data_2')", version=marker)
        db.run("commit")
    except Exception:
        try:
            db.run("rollback")
        except Exception:
            pass
        raise

def seed_server_catalog_3(db):
    marker = 14
    if db.run("select 1 from app_schema_migrations where version=:version", version=marker):
        return
    from .server_catalog_3 import SERVER_CATALOG_3
    try:
        db.run("begin")
        db.run("delete from app_servers where seed_key like 'batch3-%' or seed_key like 'batch4-%'")
        for sort_order, (seed_key, name, config) in enumerate(SERVER_CATALOG_3, start=2000):
            db.run(
                "insert into app_servers(name,config,enabled,sort_order,seed_key) "
                "select :name,:config,true,:sort,:key "
                "where not exists (select 1 from app_servers where seed_key=:key or config=:config)",
                name=name, config=config, sort=sort_order, key=seed_key,
            )
        db.run("insert into app_schema_migrations(version,name) values(:version,'server_catalog_top100_replacement')", version=marker)
        db.run("commit")
    except Exception:
        try:
            db.run("rollback")
        except Exception:
            pass
        raise

def table_exists(db,name):
    rows=db.run("select to_regclass(:name)",name="public."+name)
    return bool(rows and rows[0][0])
def import_legacy(db):
    marker=99
    if db.run("select 1 from app_schema_migrations where version=:version",version=marker):return
    try:
        db.run("begin")
        if table_exists(db,"users"):
            columns={row[0] for row in db.run("select column_name from information_schema.columns where table_name='users'")}
            if "telegram_id" in columns:
                rows=db.run("select telegram_id from users")
                for row in rows:
                    user_id=int(row[0]);ensure_user(db,{"id":user_id},allow_migrate=False)
                    old=db.run("select status,trial_used,subscription_expires,sub_token from users where telegram_id=:id",id=user_id)
                    if old:
                        db.run("update app_subscriptions set status=:status,plan=:plan,trial_used=:trial,expires_at=:expires,sub_token=coalesce(:token,sub_token) where telegram_id=:id",status=old[0][0] or "free",plan="Premium" if old[0][0]=="premium" else ("Trial" if old[0][0]=="trial" else "Free"),trial=bool(old[0][1]),expires=old[0][2],token=old[0][3],id=user_id)
        if table_exists(db,"server_pool") and int(db.run("select count(*) from app_servers")[0][0])==0:
            try:db.run("insert into app_servers(name,config) select coalesce(custom_name,'Server'),raw_config from server_pool where raw_config is not null")
            except Exception:pass
        db.run("insert into app_schema_migrations(version,name) values(:version,'legacy_import')",version=marker)
        db.run("commit")
    except Exception:
        try:db.run("rollback")
        except Exception:pass
        raise

def ensure_user(db,telegram_user,referral_code=None,allow_migrate=True):
    user_id=int(telegram_user["id"])
    rows=db.run("select telegram_id from app_users where telegram_id=:id",id=user_id)
    if not rows:
        inviter=None
        if referral_code:
            found=db.run("select telegram_id from app_users where referral_code=:code",code=referral_code)
            if found and int(found[0][0])!=user_id:inviter=int(found[0][0])
        db.run("insert into app_users(telegram_id,username,first_name,last_name,language,referral_code,referred_by) values(:id,:username,:first,:last,:language,:code,:inviter)",id=user_id,username=telegram_user.get("username"),first=telegram_user.get("first_name"),last=telegram_user.get("last_name"),language=telegram_user.get("language_code") if telegram_user.get("language_code") in ("ru","en") else "ru",code=secrets.token_hex(6),inviter=inviter)
        db.run("insert into app_subscriptions(telegram_id,sub_token) values(:id,:token)",id=user_id,token=secrets.token_hex(24))
        if inviter:
            db.run("update app_users set referral_count=referral_count+1 where telegram_id=:id",id=inviter)
            # Referral is registered now; rewards unlock only after the first paid purchase.
    else:
        db.run("update app_users set username=:username,first_name=:first,last_name=:last,updated_at=now() where telegram_id=:id",username=telegram_user.get("username"),first=telegram_user.get("first_name"),last=telegram_user.get("last_name"),id=user_id)
    return get_user(db,user_id)

def get_user(db,user_id):
    rows=db.run("select u.telegram_id,u.username,u.first_name,u.last_name,u.language,u.balance,u.banned,u.ban_reason,u.referral_code,u.referred_by,u.referral_count,u.referral_earned,u.discount_percent,u.created_at,s.status,s.plan,s.expires_at,s.trial_used,s.sub_token,s.device_limit,s.auto_renew,s.auto_renew_days,s.auto_renew_plan,s.frozen_until,s.freeze_days_used,s.extra_devices from app_users u join app_subscriptions s using(telegram_id) where u.telegram_id=:id",id=int(user_id))
    if not rows:return None
    keys=("telegram_id","username","first_name","last_name","language","balance","banned","ban_reason","referral_code","referred_by","referral_count","referral_earned","discount_percent","created_at","status","plan","expires_at","trial_used","sub_token","device_limit","auto_renew","auto_renew_days","auto_renew_plan","frozen_until","freeze_days_used","extra_devices")
    return dict(zip(keys,rows[0]))
def active(user):
    expires=user.get("expires_at")
    if user.get("status") not in ("trial","premium") or not expires:return False
    frozen=user.get("frozen_until")
    if frozen:
        if frozen.tzinfo is None:frozen=frozen.replace(tzinfo=timezone.utc)
        if frozen>datetime.now(timezone.utc):return False
    if expires.tzinfo is None:expires=expires.replace(tzinfo=timezone.utc)
    return expires>datetime.now(timezone.utc)
def extend_subscription(db,user_id,days,plan="Premium",device_limit=None):
    user=get_user(db,user_id);now=datetime.now(timezone.utc);base=now
    if active(user) and user["expires_at"]>now:base=user["expires_at"]
    expires=base+timedelta(days=int(days))
    db.run("update app_subscriptions set status='premium',plan=:plan,expires_at=:expires,device_limit=coalesce(:limit,device_limit),updated_at=now() where telegram_id=:id",plan=plan,expires=expires,limit=device_limit,id=int(user_id))
    return get_user(db,user_id)
def audit(db,admin_id,action,target_type,target_id=None,details=None):
    db.run("insert into app_admin_audit(admin_id,action,target_type,target_id,details) values(:admin,:action,:type,:target,:details)",admin=int(admin_id),action=action,type=target_type,target=str(target_id) if target_id is not None else None,details=details)
def balance_change(db,user_id,amount,kind,description,order_id=None):
    amount=float(amount)
    rows=db.run("update app_users set balance=balance+:amount,updated_at=now() where telegram_id=:id and balance+:amount>=0 returning balance",amount=amount,id=int(user_id))
    if not rows:raise ValueError("insufficient balance")
    db.run("insert into app_balance_transactions(telegram_id,amount,kind,description,related_order_id) values(:id,:amount,:kind,:description,:order)",id=int(user_id),amount=amount,kind=kind,description=description,order=order_id)
    return float(rows[0][0])
def reward_referrer(db,referred_id,order_id,amount):
    user=get_user(db,referred_id);inviter=user.get("referred_by") if user else None
    if not inviter:return 0
    if int(inviter)==int(referred_id):return 0
    inviter_user=get_user(db,inviter)
    if not inviter_user or inviter_user.get("banned"):return 0
    qualified=db.run("insert into app_referral_rewards(inviter_id,referred_id,order_id,kind,days) select :inviter,:referred,:order,'qualified_signup',:days where not exists (select 1 from app_referral_rewards where referred_id=:referred and kind='qualified_signup') returning id",inviter=int(inviter),referred=int(referred_id),order=int(order_id),days=REFERRAL_DAYS)
    if qualified:extend_subscription(db,inviter,REFERRAL_DAYS,"Referral bonus")
    reward=round(float(amount)*REFERRAL_PERCENT/100,2)
    rows=db.run("insert into app_referral_rewards(inviter_id,referred_id,order_id,kind,amount) values(:inviter,:referred,:order,'purchase',:amount) on conflict do nothing returning id",inviter=int(inviter),referred=int(referred_id),order=int(order_id),amount=reward)
    if rows:
        balance_change(db,inviter,reward,"referral",f"{REFERRAL_PERCENT}% from referral purchase",order_id)
        db.run("update app_users set referral_earned=referral_earned+:amount where telegram_id=:id",amount=reward,id=int(inviter))
        return reward
    return 0


def security_event(db,user_id,kind,details=None,ip=None):
    db.run("insert into app_security_events(telegram_id,kind,details,ip) values(:id,:kind,:details,:ip)",id=int(user_id),kind=kind,details=details,ip=ip)
