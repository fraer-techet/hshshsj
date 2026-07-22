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
from .config import ADMIN_ID, BRAND, CUSTOM_MAX_DAYS, CUSTOM_MIN_DAYS, PLANS, PORT, PREMIUM_DEVICE_LIMIT, PUBLIC_URL, REFERRAL_DAYS, REFERRAL_PERCENT, TRIAL_DAYS, TRIAL_DEVICE_LIMIT
from .telegram import call as telegram_call, miniapp_keyboard, send

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
    return {"user":services.public_user(user),"subscription":subscription_state(user),"recentOrders":recent,"plans":[{"days":d,"price":p} for d,p in PLANS.items()],"custom":{"min":CUSTOM_MIN_DAYS,"max":CUSTOM_MAX_DAYS},"servers":servers,"devices":devices,"deviceLimit":TRIAL_DEVICE_LIMIT if user["status"]=="trial" else PREMIUM_DEVICE_LIMIT,"referral":{"days":REFERRAL_DAYS,"percent":REFERRAL_PERCENT,"url":"https://t.me/"+BOT_USERNAME+"?start=ref_"+user["referral_code"] if BOT_USERNAME else "ref_"+user["referral_code"]},"isAdmin":admin(user)}
def dashboard(dbx):
    scalar=lambda sql:float(dbx.run(sql)[0][0] or 0)
    return {"users":int(scalar("select count(*) from app_users")),"active":int(scalar("select count(*) from app_subscriptions where expires_at>now()")),"orders":int(scalar("select count(*) from app_orders")),"pending":int(scalar("select count(*) from app_orders where status='pending'")),"tickets":int(scalar("select count(*) from app_tickets where status='open'")),"servers":int(scalar("select count(*) from app_servers where enabled=true")),"revenue":scalar("select coalesce(sum(amount),0) from app_payments"),"today":scalar("select coalesce(sum(amount),0) from app_payments where created_at>=date_trunc('day',now())"),"week":scalar("select coalesce(sum(amount),0) from app_payments where created_at>=now()-interval '7 days'"),"month":scalar("select coalesce(sum(amount),0) from app_payments where created_at>=now()-interval '30 days'")}
def user_details(dbx,user_id):
    user=db.get_user(dbx,user_id)
    if not user:return None
    orders=[dict(zip(("id","days","amount","method","status","createdAt"),r)) for r in dbx.run("select id,days,amount,method,status,created_at from app_orders where telegram_id=:id order by created_at desc limit 30",id=user_id)]
    return {"user":services.public_user(user),"subscription":subscription_state(user),"orders":orders}

def device_hash(agent,ip):return hashlib.sha256(((agent or "")+"|"+(ip or "")).encode()).hexdigest()[:32]
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

BOT_USERNAME=""
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def reply(self,status,payload):
        data=json.dumps(json_safe(payload),ensure_ascii=False).encode();self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
    def file(self,path):
        target=os.path.join(STATIC,path)
        if not os.path.isfile(target):self.send_error(404);return
        data=open(target,"rb").read();self.send_response(200);self.send_header("Content-Type",mimetypes.guess_type(target)[0] or "application/octet-stream");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
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
        if method=="POST" and path=="/api/quote":return services.checkout(dbx,user,int(payload["days"]),payload.get("promoCode"))
        if method=="POST" and path=="/api/topups":
            result=services.create_topup(dbx,user,float(payload["amount"]),payload["method"])
            if payload["method"]=="manual":
                try:send(ADMIN_ID,f"💳 <b>Пополнение #{result['order_id']}</b>\n<code>{uid}</code> · {result['amount']} ₽",miniapp_keyboard(PUBLIC_URL+"/app"))
                except Exception:pass
            return result
        if method=="POST" and path=="/api/orders":
            result=services.create_order(dbx,user,int(payload["days"]),payload["method"],payload.get("promoCode"))
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
        if method=="GET" and path=="/api/admin/tickets":return {"tickets":[dict(zip(("id","userId","category","subject","status","updatedAt"),r)) for r in dbx.run("select id,telegram_id,category,subject,status,updated_at from app_tickets order by updated_at desc limit 200")]}
        m=re.fullmatch(r"/api/admin/tickets/(\d+)/close",path)
        if method=="POST" and m:dbx.run("update app_tickets set status='closed',updated_at=now() where id=:id",id=int(m.group(1)));return {"ok":True}
        if method=="GET" and path=="/api/admin/servers":return {"servers":[dict(zip(("id","name","enabled","sort"),r)) for r in dbx.run("select id,name,enabled,sort_order from app_servers order by sort_order,id")]}
        if method=="POST" and path=="/api/admin/servers":server=int(dbx.run("insert into app_servers(name,config,sort_order) values(:name,:config,:sort) returning id",name=payload["name"][:100],config=payload["config"][:4000],sort=int(payload.get("sort",0)))[0][0]);return {"id":server}
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
            forwarded=self.headers.get("X-Forwarded-For","");ip=(forwarded.split(",")[0] if forwarded else self.client_address[0])[:64];fingerprint=device_hash(agent,ip);existing=dbx.run("select id,blocked from app_devices where telegram_id=:id and device_hash=:hash",id=user["telegram_id"],hash=fingerprint)
            if existing and existing[0][1]:sub_response(self,dummy("FluxVPN | Устройство удалено"),user);return
            limit=TRIAL_DEVICE_LIMIT if user["status"]=="trial" else PREMIUM_DEVICE_LIMIT
            if not existing and int(dbx.run("select count(*) from app_devices where telegram_id=:id and blocked=false",id=user["telegram_id"])[0][0])>=limit:sub_response(self,dummy("FluxVPN | Лимит устройств"),user);return
            if existing:dbx.run("update app_devices set last_seen=now(),last_ip=:ip,user_agent=:agent where id=:id",ip=ip,agent=agent[:300],id=existing[0][0])
            else:dbx.run("insert into app_devices(telegram_id,device_hash,device_name,user_agent,last_ip) values(:id,:hash,:name,:agent,:ip)",id=user["telegram_id"],hash=fingerprint,name=device_name(agent),agent=agent[:300],ip=ip)
            servers=dbx.run("select name,config from app_servers where enabled=true order by sort_order,id");lines=[]
            for name,config in servers:
                cut=config.rfind("#");lines.append((config[:cut] if cut>=0 else config)+"#"+name)
            sub_response(self,"\n".join(lines)+("\n" if lines else ""),user)

def bot_loop():
    global BOT_USERNAME
    me=telegram_call("getMe");BOT_USERNAME=me.get("username","");telegram_call("deleteWebhook",{"drop_pending_updates":False});offset=0
    while True:
        try:
            updates=telegram_call("getUpdates",{"offset":offset,"timeout":50,"allowed_updates":["message"]},60)
            for update in updates:
                offset=update["update_id"]+1;message=update.get("message")
                if not message or not (message.get("text") or "").startswith("/start"):continue
                source=message["from"];username=source.get("username");name="@"+username if username else source.get("first_name","друг")
                argument=(message.get("text") or "").split(maxsplit=1);ref=argument[1][4:] if len(argument)>1 and argument[1].startswith("ref_") else None
                with db.session() as dbx:db.ensure_user(dbx,source,ref)
                send(message["chat"]["id"],f"Привет, <b>{html.escape(name)}</b>",miniapp_keyboard(PUBLIC_URL+"/app"))
        except Exception:print("BOT",traceback.format_exc(),flush=True);time.sleep(3)
def start():
    server=ThreadingHTTPServer(("0.0.0.0",PORT),Handler);threading.Thread(target=server.serve_forever,daemon=True).start();threading.Thread(target=bot_loop,daemon=True).start();return server

