import base64
import hashlib
import html
import json
import mimetypes
import os
import re
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db, services
from .auth import AuthError, verify_init_data
from .config import ADMIN_ID, BRAND, CUSTOM_MAX_DAYS, CUSTOM_MIN_DAYS, FAMILY_DEVICE_LIMIT, FAMILY_PLANS, PLANS, PORT, PREMIUM_DEVICE_LIMIT, PUBLIC_URL, REFERRAL_DAYS, REFERRAL_PERCENT, REQUIRED_CHANNEL_URL, TRIAL_DAYS, TRIAL_DEVICE_LIMIT
from .telegram import call as telegram_call, channel_keyboard, is_channel_member, miniapp_keyboard, send

ROOT=os.path.dirname(os.path.dirname(__file__));STATIC=os.path.join(ROOT,"static")

def json_safe(value):
    if isinstance(value,dict):return {k:json_safe(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [json_safe(v) for v in value]
    if isinstance(value,datetime):return value.isoformat()
    try:
        from decimal import Decimal
        if isinstance(value,Decimal):return float(value)
    except Exception:pass
    return value
def body(handler):
    length=int(handler.headers.get("Content-Length","0") or 0)
    if length>1_000_000:raise ValueError("request too large")
    raw=handler.rfile.read(length) if length else b"{}"
    return json.loads(raw.decode() or "{}")
def current(handler,dbx):
    raw=handler.headers.get("X-Telegram-Init-Data") or handler.headers.get("Authorization","").removeprefix("tma ")
    telegram_user=verify_init_data(raw);ref=handler.headers.get("X-Referral-Code")
    return db.ensure_user(dbx,telegram_user,ref)
def admin(user):return int(user["telegram_id"])==int(ADMIN_ID)
def subscription_state(user):
    active=db.active(user);expires=user.get("expires_at")
    remaining=0
    if active:
        if expires.tzinfo is None:expires=expires.replace(tzinfo=timezone.utc)
        remaining=max(1,int(((expires-datetime.now(timezone.utc)).total_seconds()+86399)//86400))
    return {"active":active,"status":user["status"],"plan":user["plan"],"expiresAt":expires,"daysLeft":remaining,"trialUsed":user["trial_used"],"url":PUBLIC_URL+"/sub/"+user["sub_token"]}
def bootstrap(dbx,user):
    servers=[{"id":r[0],"name":r[1]} for r in dbx.run("select id,name from app_servers where enabled=true order by sort_order,id")]
    devices=[{"id":r[0],"name":r[1],"lastSeen":r[2]} for r in dbx.run("select id,device_name,last_seen from app_devices where telegram_id=:id and blocked=false order by last_seen desc",id=user["telegram_id"])]
    recent=[dict(zip(("id","kind","days","amount","method","status","createdAt"),r)) for r in dbx.run("select id,kind,days,amount,method,status,created_at from app_orders where telegram_id=:id order by created_at desc limit 10",id=user["telegram_id"])]
    return {"user":services.public_user(user),"subscription":subscription_state(user),"recentOrders":recent,"plans":[{"days":d,"price":p} for d,p in PLANS.items()],"familyPlans":[{"days":d,"price":p} for d,p in FAMILY_PLANS.items()],"custom":{"min":CUSTOM_MIN_DAYS,"max":CUSTOM_MAX_DAYS},"servers":servers,"devices":devices,"deviceLimit":TRIAL_DEVICE_LIMIT if user["status"]=="trial" else int(user.get("device_limit") or PREMIUM_DEVICE_LIMIT),"referral":{"days":REFERRAL_DAYS,"percent":REFERRAL_PERCENT,"url":"https://t.me/"+BOT_USERNAME+"?start=ref_"+user["referral_code"] if BOT_USERNAME else "ref_"+user["referral_code"]},"botUsername":BOT_USERNAME,"isAdmin":admin(user)}
def dashboard(dbx):
    scalar=lambda sql:float(dbx.run(sql)[0][0] or 0)
    return {"users":int(scalar("select count(*) from app_users")),"active":int(scalar("select count(*) from app_subscriptions where expires_at>now()")),"orders":int(scalar("select count(*) from app_orders")),"pending":int(scalar("select count(*) from app_orders where status='pending'")),"tickets":int(scalar("select count(*) from app_tickets where status='open'")),"servers":int(scalar("select count(*) from app_servers where enabled=true")),"revenue":scalar("select coalesce(sum(amount),0) from app_payments"),"today":scalar("select coalesce(sum(amount),0) from app_payments where created_at>=date_trunc('day',now())"),"week":scalar("select coalesce(sum(amount),0) from app_payments where created_at>=now()-interval '7 days'"),"month":scalar("select coalesce(sum(amount),0) from app_payments where created_at>=now()-interval '30 days'"),"family":int(scalar("select count(*) from app_subscriptions where plan='Family' and expires_at>now()")),"autoRenew":int(scalar("select count(*) from app_subscriptions where auto_renew=true")),"gifts":int(scalar("select count(*) from app_gifts where status='claimed'"))}
def user_details(dbx,user_id):
    user=db.get_user(dbx,user_id)
    if not user:return None
    orders=[dict(zip(("id","days","amount","method","status","createdAt"),r)) for r in dbx.run("select id,days,amount,method,status,created_at from app_orders where telegram_id=:id order by created_at desc limit 30",id=user_id)]
    return {"user":services.public_user(user),"subscription":subscription_state(user),"orders":orders}

def normalize_agent(agent):
    value=(agent or "unknown").lower().strip()
    value=re.sub(r"(?<=[/ ])v?\d+(?:\.\d+){0,4}","",value)
    value=re.sub(r"\s+"," ",value)
    return value[:240]
def device_hash(value):return hashlib.sha256((value or "unknown").encode()).hexdigest()[:32]
def device_identity(handler,agent):
    for header in ("X-Device-ID","X-Client-ID","Device-ID","X-Installation-ID","X-Hwid"):
        value=(handler.headers.get(header) or "").strip()
        if value:return "id:"+value[:200]
    return "ua:"+normalize_agent(agent)
def device_name(agent):
    lower=(agent or "").lower()
    for mark,name in (("happ","Happ"),("hiddify","Hiddify"),("v2ray","v2rayNG"),("clash","Clash"),("shadowrocket","Shadowrocket"),("streisand","Streisand"),("nekobox","NekoBox"),("sing-box","sing-box")):
        if mark in lower:return name
    return ((agent or "Device").split("/")[0].split(" ")[0] or "Device")[:40]
def vpn_client(agent):return any(x in (agent or "").lower() for x in ("happ","hiddify","v2ray","clash","sing-box","nekobox","shadowrocket","streisand","okhttp"))
def sub_response(handler,text,user):
    data=text.encode();expires=user.get("expires_at");timestamp=int(expires.timestamp()) if expires else 0
    handler.send_response(200)
    for k,v in {"Content-Type":"text/plain; charset=utf-8","Cache-Control":"no-store","Profile-Title":"base64:Rmx1eFZQTg","Profile-Update-Interval":"1","Subscription-Userinfo":f"upload=0; download=0; total=0; expire={timestamp}","Content-Length":str(len(data))}.items():handler.send_header(k,v)
    handler.end_headers();handler.wfile.write(data)
def dummy(title):return "vless://00000000-0000-0000-0000-000000000000@127.0.0.1:1?encryption=none&security=none&type=tcp#"+urllib.parse.quote(title,safe="")+"\n"

SUPPORTED_URI_SCHEMES={"vless","trojan","ss","vmess","socks","hysteria2","hy2","tuic","ssr"}
def b64decode_loose(value):
    raw=value.encode();raw+=b"="*((4-len(raw)%4)%4)
    return base64.urlsafe_b64decode(raw)
def validate_share_uri(value):
    config=str(value or "").strip()
    if not config or "\n" in config or "\r" in config:raise ValueError("one URI per server")
    scheme=config.split(":",1)[0].lower() if ":" in config else ""
    if scheme not in SUPPORTED_URI_SCHEMES:raise ValueError("supported: vless, trojan, ss, vmess, socks, hysteria2, hy2, tuic, ssr")
    if scheme=="vmess":
        try:
            payload=config.split("#",1)[0][8:];parsed=json.loads(b64decode_loose(payload).decode())
            if not isinstance(parsed,dict) or not parsed.get("add") or not parsed.get("port"):raise ValueError
        except Exception:raise ValueError("invalid vmess URI")
    return config
def brand_share_uri(config,name):
    config=str(config).strip();scheme=config.split(":",1)[0].lower()
    if scheme=="vmess":
        try:
            payload=config.split("#",1)[0][8:];parsed=json.loads(b64decode_loose(payload).decode());parsed["ps"]=name
            encoded=base64.b64encode(json.dumps(parsed,ensure_ascii=False,separators=(",",":")).encode()).decode()
            return "vmess://"+encoded
        except Exception:return config
    if scheme=="ssr":return config
    cut=config.rfind("#");return (config[:cut] if cut>=0 else config)+"#"+urllib.parse.quote(name,safe="")

BOT_USERNAME=""
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,status,payload):
        data=json.dumps(json_safe(payload),ensure_ascii=False).encode();self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
    def file(self,path):
        target=os.path.join(STATIC,path)
        if not os.path.isfile(target):self.send_error(404);return
        data=open(target,"rb").read();self.send_response(200);self.send_header("Content-Type",mimetypes.guess_type(target)[0] or "application/octet-stream");self.send_header("Cache-Control","no-store, max-age=0");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
    def do_HEAD(self):
        path=urllib.parse.urlparse(self.path).path
        if path in ("/","/app","/app/","/health"):
            self.send_response(200);self.send_header("Content-Length","0");self.send_header("Cache-Control","no-store");self.end_headers();return
        self.send_response(404);self.send_header("Content-Length","0");self.end_headers()
    def do_GET(self):self.route("GET")
    def do_POST(self):self.route("POST")
    def do_DELETE(self):self.route("DELETE")
    def route(self,method):
        path=urllib.parse.urlparse(self.path).path
        try:
            if path in ("/","/app","/app/"):self.file("index.html");return
            if path=="/health":self.reply(200,{"ok":True,"service":"FluxVPN"});return
            if path.startswith("/static/"):self.file(path[len("/static/"):]);return
            if path.startswith("/sub/"):self.subscription(path);return
            if not path.startswith("/api/"):self.reply(404,{"error":"not found"});return
            with db.session() as dbx:
                user=current(self,dbx);payload=body(self) if method=="POST" else {}
                if user.get("banned") and not admin(user):self.reply(403,{"error":"banned","reason":user.get("ban_reason")});return
                if not admin(user) and not is_channel_member(user["telegram_id"]):
                    self.reply(403,{"error":"Подпишись на канал FluxVPN, чтобы продолжить","code":"CHANNEL_SUBSCRIPTION_REQUIRED","channelUrl":REQUIRED_CHANNEL_URL});return
                result=self.api(dbx,user,method,path,payload);self.reply(200,result)
        except AuthError as error:self.reply(401,{"error":str(error)})
        except ValueError as error:self.reply(400,{"error":str(error)})
        except PermissionError as error:self.reply(403,{"error":str(error)})
        except Exception as error:print("API",path,traceback.format_exc(),flush=True);self.reply(500,{"error":"server error"})
    def api(self,dbx,user,method,path,payload):
        uid=user["telegram_id"]
        if method=="GET" and path=="/api/bootstrap":return bootstrap(dbx,user)
        if method=="POST" and path=="/api/settings":
            language=payload.get("language")
            if language not in ("ru","en"):raise ValueError("invalid language")
            dbx.run("update app_users set language=:language where telegram_id=:id",language=language,id=uid);return {"ok":True}
        if method=="POST" and path=="/api/trial":
            if user["trial_used"]:raise ValueError("trial already used")
            if db.active(user):raise ValueError("subscription already active")
            expires=datetime.now(timezone.utc)+timedelta(days=TRIAL_DAYS);dbx.run("update app_subscriptions set status='trial',plan='Trial',trial_used=true,expires_at=:expires where telegram_id=:id",expires=expires,id=uid);return bootstrap(dbx,db.get_user(dbx,uid))
        if method=="POST" and path=="/api/quote":return services.checkout(dbx,user,int(payload["days"]),payload.get("promoCode"),payload.get("plan","premium"))
        if method=="POST" and path=="/api/topups":
            result=services.create_topup(dbx,user,float(payload["amount"]),payload["method"])
            if payload["method"]=="manual":
                try:send(ADMIN_ID,f"💳 <b>Пополнение #{result['order_id']}</b>\n<code>{uid}</code> · {result['amount']} ₽",miniapp_keyboard(PUBLIC_URL+"/app"))
                except Exception:pass
            return result
        if method=="POST" and path=="/api/orders":
            result=services.create_order(dbx,user,int(payload["days"]),payload["method"],payload.get("promoCode"),payload.get("plan","premium"))
            if payload["method"]=="manual":
                try:send(ADMIN_ID,f"🛒 <b>Новый заказ #{result['order_id']}</b>\n<code>{uid}</code> · {result['days']} дней · {result['amount']} ₽",miniapp_keyboard(PUBLIC_URL+"/app"))
                except Exception:pass
            return result
        m=re.fullmatch(r"/api/orders/(\d+)/check",path)
        if method=="POST" and m:
            order_id=int(m.group(1));rows=dbx.run("select telegram_id from app_orders where id=:id",id=order_id)
            if not rows or (int(rows[0][0])!=uid and not admin(user)):raise PermissionError("not your order")
            return {"status":services.check_crypto_order(dbx,order_id)}
        if method=="POST" and path=="/api/promos/redeem":return services.redeem_promo(dbx,user,payload["code"])
        if method=="POST" and path=="/api/auto-renew":
            updated=services.set_auto_renew(dbx,uid,bool(payload.get("enabled")),int(payload.get("days",30)),payload.get("plan","premium"));return {"enabled":updated["auto_renew"],"days":updated["auto_renew_days"],"plan":updated["auto_renew_plan"]}
        if method=="GET" and path=="/api/diagnostics":
            active_count=int(dbx.run("select count(*) from app_devices where telegram_id=:id and blocked=false",id=uid)[0][0]);server_count=int(dbx.run("select count(*) from app_servers where enabled=true")[0][0])
            return {"subscription":db.active(user),"status":user["status"],"expiresAt":user.get("expires_at"),"devices":active_count,"deviceLimit":TRIAL_DEVICE_LIMIT if user["status"]=="trial" else int(user.get("device_limit") or PREMIUM_DEVICE_LIMIT),"servers":server_count,"channel":True}
        if method=="POST" and path=="/api/subscription/rotate":
            import secrets
            token=secrets.token_hex(24);dbx.run("update app_subscriptions set sub_token=:token where telegram_id=:id",token=token,id=uid);return {"url":PUBLIC_URL+"/sub/"+token}
        m=re.fullmatch(r"/api/devices/(\d+)",path)
        if method=="DELETE" and m:dbx.run("update app_devices set blocked=true where id=:device and telegram_id=:user",device=int(m.group(1)),user=uid);return {"ok":True}
        if method=="GET" and path=="/api/tickets":
            tickets=[dict(zip(("id","category","subject","status","createdAt","updatedAt"),r)) for r in dbx.run("select id,category,subject,status,created_at,updated_at from app_tickets where telegram_id=:id order by updated_at desc",id=uid)];return {"tickets":tickets}
        if method=="POST" and path=="/api/tickets":
            ticket=int(dbx.run("insert into app_tickets(telegram_id,category,subject) values(:id,:category,:subject) returning id",id=uid,category=payload.get("category","other")[:30],subject=payload["message"][:80])[0][0]);dbx.run("insert into app_ticket_messages(ticket_id,sender_id,sender_role,body) values(:ticket,:sender,'user',:body)",ticket=ticket,sender=uid,body=payload["message"][:4000])
            try:send(ADMIN_ID,f"🎫 <b>Новый тикет #{ticket}</b>\n<code>{uid}</code> · {html.escape(payload['message'][:500])}",miniapp_keyboard(PUBLIC_URL+"/app"))
            except Exception:pass
            return {"ticketId":ticket}
        m=re.fullmatch(r"/api/tickets/(\d+)/messages",path)
        if m:
            ticket=int(m.group(1));owner=dbx.run("select telegram_id from app_tickets where id=:id",id=ticket)
            if not owner or (int(owner[0][0])!=uid and not admin(user)):raise PermissionError("ticket denied")
            if method=="GET":return {"messages":[dict(zip(("id","senderId","role","body","createdAt"),r)) for r in dbx.run("select id,sender_id,sender_role,body,created_at from app_ticket_messages where ticket_id=:id order by id",id=ticket)]}
            if method=="POST":
                role="admin" if admin(user) else "user";dbx.run("insert into app_ticket_messages(ticket_id,sender_id,sender_role,body) values(:ticket,:sender,:role,:body)",ticket=ticket,sender=uid,role=role,body=payload["message"][:4000]);dbx.run("update app_tickets set updated_at=now(),status='open' where id=:id",id=ticket)
                if role=="admin":
                    try:send(int(owner[0][0]),f"💬 <b>Ответ поддержки · тикет #{ticket}</b>\n\n"+html.escape(payload["message"][:3000]),miniapp_keyboard(PUBLIC_URL+"/app"))
                    except Exception:pass
                return {"ok":True}
        if path.startswith("/api/admin/"):
            if not admin(user):raise PermissionError("admin only")
            return self.admin_api(dbx,user,method,path,payload)
        raise ValueError("unknown endpoint")
    def admin_api(self,dbx,user,method,path,payload):
        if method=="GET" and path=="/api/admin/dashboard":return dashboard(dbx)
        if method=="GET" and path=="/api/admin/users":
            query=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q",[""])[0];pattern="%"+query+"%"
            rows=dbx.run("select telegram_id,username,first_name,balance,banned,created_at from app_users where cast(telegram_id as text) like :q or coalesce(username,'') ilike :q or coalesce(first_name,'') ilike :q order by created_at desc limit 100",q=pattern);return {"users":[dict(zip(("id","username","name","balance","banned","createdAt"),r)) for r in rows]}
        m=re.fullmatch(r"/api/admin/users/(\d+)",path)
        if method=="GET" and m:return user_details(dbx,int(m.group(1))) or (_ for _ in ()).throw(ValueError("user not found"))
        m=re.fullmatch(r"/api/admin/users/(\d+)/action",path)
        if method=="POST" and m:
            target=int(m.group(1));action=payload["action"]
            if action=="grant":db.extend_subscription(dbx,target,int(payload["days"]),"Premium")
            elif action=="revoke":dbx.run("update app_subscriptions set status='free',plan='Free',expires_at=null where telegram_id=:id",id=target)
            elif action=="ban":dbx.run("update app_users set banned=true,ban_reason=:reason where telegram_id=:id",reason=payload.get("reason","Blocked"),id=target)
            elif action=="unban":dbx.run("update app_users set banned=false,ban_reason=null where telegram_id=:id",id=target)
            elif action=="balance":db.balance_change(dbx,target,float(payload["amount"]),"admin","Admin adjustment")
            elif action=="reset_devices":dbx.run("update app_devices set blocked=true where telegram_id=:id",id=target)
            else:raise ValueError("unknown action")
            db.audit(dbx,user["telegram_id"],action,"user",target,json.dumps(payload,ensure_ascii=False));return {"ok":True}
        if method=="GET" and path=="/api/admin/orders":return {"orders":[dict(zip(("id","userId","days","amount","method","status","createdAt"),r)) for r in dbx.run("select id,telegram_id,days,amount,method,status,created_at from app_orders order by created_at desc limit 200")]}
        m=re.fullmatch(r"/api/admin/orders/(\d+)/action",path)
        if method=="POST" and m:
            order=int(m.group(1));action=payload["action"]
            if action=="approve":
                target=dbx.run("select telegram_id,days from app_orders where id=:id",id=order)[0];services.fulfill_order(dbx,order,"admin",None)
                try:send(int(target[0]),f"✅ Оплата подтверждена. Premium активирован на <b>{target[1]} дней</b>.",miniapp_keyboard(PUBLIC_URL+"/app"))
                except Exception:pass
            elif action=="reject":dbx.run("update app_orders set status='rejected',updated_at=now() where id=:id and status='pending'",id=order)
            else:raise ValueError("unknown action")
            db.audit(dbx,user["telegram_id"],action,"order",order);return {"ok":True}
        if method=="GET" and path=="/api/admin/promos":return {"promos":[dict(zip(("code","kind","value","maxUses","used","active","expiresAt"),r)) for r in dbx.run("select code,kind,value,max_uses,used_count,active,expires_at from app_promos order by created_at desc")]}
        if method=="POST" and path=="/api/admin/promos":
            code=payload["code"].strip().upper();kind=payload["kind"]
            if kind not in ("days","balance","percent","fixed"):raise ValueError("invalid promo kind")
            dbx.run("insert into app_promos(code,kind,value,max_uses) values(:code,:kind,:value,:uses) on conflict(code) do update set kind=:kind,value=:value,max_uses=:uses,active=true,updated_at=now()",code=code,kind=kind,value=float(payload["value"]),uses=int(payload.get("maxUses",100)));db.audit(dbx,user["telegram_id"],"promo_save","promo",code);return {"ok":True}
        if method=="GET" and path=="/api/admin/gifts":
            rows=dbx.run("select token,creator_id,kind,value,cost,status,claimed_by,created_at,expires_at from app_gifts order by created_at desc limit 200");return {"gifts":[dict(zip(("token","creatorId","kind","value","cost","status","claimedBy","createdAt","expiresAt"),r)) for r in rows]}
        if method=="GET" and path=="/api/admin/tickets":return {"tickets":[dict(zip(("id","userId","category","subject","status","updatedAt"),r)) for r in dbx.run("select id,telegram_id,category,subject,status,updated_at from app_tickets order by updated_at desc limit 200")]}
        m=re.fullmatch(r"/api/admin/tickets/(\d+)/close",path)
        if method=="POST" and m:dbx.run("update app_tickets set status='closed',updated_at=now() where id=:id",id=int(m.group(1)));return {"ok":True}
        if method=="GET" and path=="/api/admin/servers":return {"servers":[dict(zip(("id","name","enabled","sort"),r)) for r in dbx.run("select id,name,enabled,sort_order from app_servers order by sort_order,id")]}
        if method=="POST" and path=="/api/admin/servers":
            config=validate_share_uri(payload["config"]);server=int(dbx.run("insert into app_servers(name,config,sort_order) values(:name,:config,:sort) returning id",name=payload["name"][:100],config=config,sort=int(payload.get("sort",0)))[0][0]);return {"id":server,"protocol":config.split(":",1)[0].lower()}
        m=re.fullmatch(r"/api/admin/servers/(\d+)",path)
        if method=="DELETE" and m:dbx.run("delete from app_servers where id=:id",id=int(m.group(1)));return {"ok":True}
        if method=="POST" and path=="/api/admin/broadcast":
            ok=failed=0
            for row in dbx.run("select telegram_id from app_users where banned=false"):
                try:send(row[0],"📣 <b>FluxVPN</b>\n\n"+html.escape(payload["message"][:4000]));ok+=1
                except Exception:failed+=1
            return {"ok":ok,"failed":failed}
        raise ValueError("unknown admin endpoint")
    def subscription(self,path):
        token=path.split("/sub/",1)[1].split("/",1)[0]
        with db.session() as dbx:
            rows=dbx.run("select telegram_id from app_subscriptions where sub_token=:token",token=token)
            if not rows:self.send_error(404);return
            user=db.get_user(dbx,rows[0][0]);agent=self.headers.get("User-Agent","")
            if not db.active(user) or user.get("banned"):sub_response(self,dummy("FluxVPN | Подписка истекла"),user);return
            if not vpn_client(agent):self.send_response(302);self.send_header("Location",PUBLIC_URL+"/app");self.end_headers();return
            forwarded=self.headers.get("X-Forwarded-For","");ip=(forwarded.split(",")[0] if forwarded else self.client_address[0])[:64]
            identity=device_identity(self,agent);fingerprint=device_hash(identity)
            existing=dbx.run("select id,blocked from app_devices where telegram_id=:id and device_hash=:hash",id=user["telegram_id"],hash=fingerprint)
            # Migrate old IP-based fingerprints and merge duplicates produced by changing mobile IPs.
            if not existing and identity.startswith("ua:"):
                candidates=dbx.run("select id,device_hash,user_agent,blocked from app_devices where telegram_id=:id order by last_seen desc",id=user["telegram_id"])
                matches=[row for row in candidates if normalize_agent(row[2])==normalize_agent(agent)]
                blocked=next((row for row in matches if row[3]),None)
                if blocked:existing=[(blocked[0],True)]
                elif matches:
                    keep=matches[0];dbx.run("delete from app_devices where telegram_id=:id and id<>:keep and user_agent is not null and lower(split_part(user_agent,'/',1))=lower(split_part(:agent,'/',1))",id=user["telegram_id"],keep=keep[0],agent=agent[:300])
                    try:dbx.run("update app_devices set device_hash=:hash where id=:id",hash=fingerprint,id=keep[0])
                    except Exception:pass
                    existing=[(keep[0],False)]
            if existing and existing[0][1]:sub_response(self,dummy("FluxVPN | Устройство удалено"),user);return
            limit=TRIAL_DEVICE_LIMIT if user["status"]=="trial" else int(user.get("device_limit") or PREMIUM_DEVICE_LIMIT)
            if not existing and int(dbx.run("select count(*) from app_devices where telegram_id=:id and blocked=false",id=user["telegram_id"])[0][0])>=limit:sub_response(self,dummy("FluxVPN | Лимит устройств"),user);return
            if existing:dbx.run("update app_devices set last_seen=now(),last_ip=:ip,user_agent=:agent,device_name=:name where id=:id",ip=ip,agent=agent[:300],name=device_name(agent),id=existing[0][0])
            else:dbx.run("insert into app_devices(telegram_id,device_hash,device_name,user_agent,last_ip) values(:id,:hash,:name,:agent,:ip)",id=user["telegram_id"],hash=fingerprint,name=device_name(agent),agent=agent[:300],ip=ip)
            servers=dbx.run("select name,config from app_servers where enabled=true order by sort_order,id");lines=[]
            for name,config in servers:
                lines.append(brand_share_uri(config,name))
            sub_response(self,"\n".join(lines)+("\n" if lines else ""),user)

def gift_message(result):
    if result["kind"]=="subscription":return f"🎁 <b>Подарок FluxVPN</b>\n\nПодписка на <b>{int(result['value'])} дней</b>. Нажми кнопку, чтобы забрать."
    return f"🎁 <b>Подарок FluxVPN</b>\n\nНа баланс: <b>{int(result['value'])} ₽</b>. Нажми кнопку, чтобы забрать."

def claim_gift_for_user(source,token,chat_id):
    with db.session() as dbx:
        db.ensure_user(dbx,source)
        result=services.claim_gift(dbx,token,int(source["id"]))
    text=f"🎉 Подарок получен: {int(result['value'])} дней подписки" if result["kind"]=="subscription" else f"🎉 На баланс зачислено {int(result['value'])} ₽"
    send(chat_id,text,miniapp_keyboard(PUBLIC_URL+"/app"))
    try:send(result["creator"],f"🎁 Твой подарок забрали. С баланса списано <b>{int(result['value'])} ₽</b>." if result["kind"]=="balance" else f"🎁 Твой подарок на <b>{int(result['value'])} дней</b> забрали.")
    except Exception:pass

def handle_inline_query(inline):
    source=inline["from"];uid=int(source["id"])
    if uid!=int(ADMIN_ID) and not is_channel_member(uid):
        telegram_call("answerInlineQuery",{"inline_query_id":inline["id"],"is_personal":True,"cache_time":0,"results":[{"type":"article","id":"join","title":"Сначала подпишись на FluxVPN","description":"После подписки открой меню ещё раз","input_message_content":{"message_text":"Подпишись на @fluxvvpn, чтобы отправлять подарки FluxVPN"},"reply_markup":{"inline_keyboard":[[{"text":"📢 Подписаться","url":REQUIRED_CHANNEL_URL}]]}}]});return
    query=(inline.get("query") or "").strip().lower();options=[]
    if query:
        numbers=re.findall(r"\d+",query);value=int(numbers[0]) if numbers else 0
        if query.startswith(("бал","balance","rub","₽")) and value in (50,100,200,500,1000):options=[("balance",value)]
        elif query.startswith(("под","sub","days","дн")) and CUSTOM_MIN_DAYS<=value<=CUSTOM_MAX_DAYS:options=[("subscription",value)]
    else:options=[("subscription",7),("subscription",30),("subscription",90),("balance",100),("balance",200),("balance",500)]
    results=[]
    with db.session() as dbx:
        db.ensure_user(dbx,source)
        for kind,value in options:
            gift=services.create_gift(dbx,uid,kind,value);link=f"https://t.me/{BOT_USERNAME}?start=gift_{gift['token']}"
            title=f"🎁 Подписка на {int(gift['value'])} дней" if kind=="subscription" else f"🎁 {int(gift['value'])} ₽ на баланс"
            results.append({"type":"article","id":gift["token"],"title":title,"description":f"Спишется {int(gift['cost'])} ₽ после получения","input_message_content":{"message_text":gift_message(gift),"parse_mode":"HTML"},"reply_markup":{"inline_keyboard":[[{"text":"🎁 Забрать подарок","url":link}]]}})
    if not results:results=[{"type":"article","id":"help","title":"Формат подарка","description":"Напиши: подписка 30 или баланс 100","input_message_content":{"message_text":"Для подарка напиши: подписка 30 или баланс 100"}}]
    telegram_call("answerInlineQuery",{"inline_query_id":inline["id"],"is_personal":True,"cache_time":0,"results":results})

def handle_bot_update(update):
    inline=update.get("inline_query")
    if inline:handle_inline_query(inline);return
    callback=update.get("callback_query")
    if callback and (callback.get("data") or "").startswith("check_channel_subscription"):
        source=callback["from"];joined=int(source["id"])==int(ADMIN_ID) or is_channel_member(source["id"]);data=callback.get("data") or ""
        telegram_call("answerCallbackQuery",{"callback_query_id":callback["id"],"text":"Подписка подтверждена ✅" if joined else "Сначала подпишись на канал","show_alert":not joined})
        if joined:
            message=callback.get("message") or {};chat_id=message.get("chat",{}).get("id",source["id"])
            if "|gift_" in data:
                try:claim_gift_for_user(source,data.split("|gift_",1)[1],chat_id)
                except Exception as error:send(chat_id,"Не удалось получить подарок: "+html.escape(str(error)))
            else:send(chat_id,"Подписка подтверждена ✅",miniapp_keyboard(PUBLIC_URL+"/app"))
        return
    message=update.get("message")
    if not message or not (message.get("text") or "").startswith("/start"):return
    source=message["from"];username=source.get("username");name="@"+username if username else source.get("first_name","друг");parts=(message.get("text") or "").split(maxsplit=1);argument=parts[1] if len(parts)>1 else "";ref=argument[4:] if argument.startswith("ref_") else None
    with db.session() as dbx:db.ensure_user(dbx,source,ref)
    gift_token=argument[5:] if argument.startswith("gift_") else None
    if int(source["id"])!=int(ADMIN_ID) and not is_channel_member(source["id"]):
        callback_data="check_channel_subscription"+("|gift_"+gift_token if gift_token else "")
        send(message["chat"]["id"],f"Привет, <b>{html.escape(name)}</b>\n\nЧтобы пользоваться FluxVPN, подпишись на канал и нажми «Проверить подписку».",channel_keyboard(True,callback_data));return
    if gift_token:
        try:claim_gift_for_user(source,gift_token,message["chat"]["id"])
        except Exception as error:send(message["chat"]["id"],"Не удалось получить подарок: "+html.escape(str(error)))
        return
    send(message["chat"]["id"],f"Привет, <b>{html.escape(name)}</b>",miniapp_keyboard(PUBLIC_URL+"/app"))

def bot_loop():
    global BOT_USERNAME
    lock_key=8928692194
    while True:
        lock_db=None
        try:
            lock_db=db.connection();announced=False
            while not bool(lock_db.run("select pg_try_advisory_lock(:key)",key=lock_key)[0][0]):
                if not announced:print("BOT polling standby: another instance is active",flush=True);announced=True
                time.sleep(5)
            print("BOT polling lock acquired",flush=True);me=telegram_call("getMe");BOT_USERNAME=me.get("username","");telegram_call("deleteWebhook",{"drop_pending_updates":False});offset=0
            while True:
                updates=telegram_call("getUpdates",{"offset":offset,"timeout":50,"allowed_updates":["message","callback_query","inline_query"]},60);lock_db.run("select 1")
                for update in updates:
                    offset=update["update_id"]+1
                    try:handle_bot_update(update)
                    except Exception:print("BOT UPDATE",traceback.format_exc(),flush=True)
        except Exception:print("BOT",traceback.format_exc(),flush=True);time.sleep(5)
        finally:
            if lock_db is not None:
                try:lock_db.close()
                except Exception:pass

def start():
    server=ThreadingHTTPServer(("0.0.0.0",PORT),Handler);threading.Thread(target=server.serve_forever,daemon=True).start();threading.Thread(target=bot_loop,daemon=True).start();return server
