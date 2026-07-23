import json
import urllib.parse
import urllib.request
import secrets
from datetime import datetime, timedelta, timezone

from . import db
from .config import ADMIN_USERNAME, CRYPTO_BOT_TOKEN, CUSTOM_MAX_DAYS, CUSTOM_MIN_DAYS, FAMILY_DEVICE_LIMIT, FAMILY_PLANS, PLANS, PREMIUM_DEVICE_LIMIT, REFERRAL_DAYS, REFERRAL_PERCENT


def calculate_price(days):
    days=int(days)
    if days in PLANS:return PLANS[days]
    if not CUSTOM_MIN_DAYS<=days<=CUSTOM_MAX_DAYS:raise ValueError("invalid days")
    if days<=7:return max(30,round(days*50/7))
    if days<=30:return round(50+(days-7)*150/23)
    if days<=90:return round(200+(days-30)*200/60)
    if days<=365:return round(400+(days-90)*400/275)
    return round(800+(days-365)*2)
def calculate_plan_price(days,plan="premium"):
    if plan=="premium":return calculate_price(days)
    days=int(days)
    if plan!="family" or days not in FAMILY_PLANS:raise ValueError("invalid family plan")
    return FAMILY_PLANS[days]

def public_user(user):
    return {key:(float(value) if key in ("balance","referral_earned","discount_percent") else value.isoformat() if hasattr(value,"isoformat") else value) for key,value in user.items() if key not in ("ban_reason",)}
def checkout(dbx,user,days,promo_code=None,plan="premium"):
    base=float(calculate_plan_price(days,plan));discount_percent=float(user.get("discount_percent") or 0);promo=None
    if promo_code:
        rows=dbx.run("select code,kind,value,max_uses,used_count,active,expires_at,min_amount,new_users_only from app_promos where code=:code",code=promo_code.upper())
        if not rows:raise ValueError("promo not found")
        row=rows[0]
        if not row[5] or int(row[4])>=int(row[3]) or (row[6] and row[6]<datetime.now(timezone.utc)) or base<float(row[7]):raise ValueError("promo unavailable")
        if dbx.run("select 1 from app_promo_redemptions where code=:code and telegram_id=:id",code=promo_code.upper(),id=user["telegram_id"]):raise ValueError("promo already used")
        if row[1]=="percent":discount_percent=max(discount_percent,float(row[2]));promo=row[0]
        elif row[1]=="fixed":promo=row[0]
    discount=round(base*discount_percent/100,2)
    if promo_code and promo:
        row=dbx.run("select kind,value from app_promos where code=:code",code=promo)[0]
        if row[0]=="fixed":discount=max(discount,min(base-1,float(row[1])))
    return {"days":int(days),"plan":plan,"base":base,"discount":discount,"amount":max(1,round(base-discount,2)),"promo":promo,"discount_percent":discount_percent}
def manual_payment_url(order_id,user,quote):
    message=f"FluxVPN order #{order_id}\nID: {user['telegram_id']}\nPlan: {quote['days']} days\nAmount: {quote['amount']} RUB"
    return "https://t.me/"+ADMIN_USERNAME+"?text="+urllib.parse.quote(message)
def crypto_call(method,payload):
    if not CRYPTO_BOT_TOKEN:raise RuntimeError("CryptoBot is not configured")
    data=json.dumps(payload).encode();request=urllib.request.Request("https://pay.crypt.bot/api/"+method,data=data,headers={"Content-Type":"application/json","Crypto-Pay-API-Token":CRYPTO_BOT_TOKEN},method="POST")
    with urllib.request.urlopen(request,timeout=30) as response:result=json.loads(response.read().decode())
    if not result.get("ok"):raise RuntimeError(str(result))
    return result["result"]
def create_crypto_invoice(order_id,amount,description):
    return crypto_call("createInvoice",{"currency_type":"fiat","fiat":"RUB","amount":str(amount),"description":description,"payload":f"order:{order_id}","expires_in":3600,"allow_comments":False,"allow_anonymous":True})
def get_crypto_invoice(invoice_id):
    result=crypto_call("getInvoices",{"invoice_ids":str(invoice_id)})
    items=result.get("items",[]) if isinstance(result,dict) else result
    return items[0] if items else None
def create_order(dbx,user,days,method,promo_code=None,plan="premium"):
    quote=checkout(dbx,user,days,promo_code,plan)
    if method=="balance" and float(user.get("balance") or 0)<quote["amount"]: raise ValueError("insufficient balance")
    rows=dbx.run("insert into app_orders(telegram_id,days,base_amount,discount_amount,amount,method,promo_code,plan_code) values(:id,:days,:base,:discount,:amount,:method,:promo,:plan) returning id",id=user["telegram_id"],days=days,base=quote["base"],discount=quote["discount"],amount=quote["amount"],method=method,promo=quote["promo"],plan=plan)
    order_id=int(rows[0][0]);result={"order_id":order_id,"status":"pending",**quote}
    if method=="balance":
        fulfill_order(dbx,order_id,"balance",None);result["status"]="paid"
    elif method=="manual":result["payment_url"]=manual_payment_url(order_id,user,quote)
    elif method=="crypto":
        invoice=create_crypto_invoice(order_id,quote["amount"],f"FluxVPN {plan.title()} {days} days");invoice_id=str(invoice.get("invoice_id") or invoice.get("id"));url=invoice.get("bot_invoice_url") or invoice.get("pay_url") or invoice.get("mini_app_invoice_url")
        dbx.run("update app_orders set external_id=:external where id=:id",external=invoice_id,id=order_id);result.update(invoice_id=invoice_id,payment_url=url)
    else:raise ValueError("invalid payment method")
    return result
def create_topup(dbx,user,amount,method):
    amount=float(amount)
    if amount not in (100,200,500,1000): raise ValueError("invalid top-up amount")
    order_id=int(dbx.run("insert into app_orders(telegram_id,kind,days,base_amount,amount,method) values(:id,'topup',0,:amount,:amount,:method) returning id",id=user["telegram_id"],amount=amount,method=method)[0][0])
    result={"order_id":order_id,"status":"pending","amount":amount}
    if method=="manual": result["payment_url"]="https://t.me/"+ADMIN_USERNAME+"?text="+urllib.parse.quote(f"FluxVPN top-up #{order_id}\nID: {user['telegram_id']}\nAmount: {amount} RUB")
    elif method=="crypto":
        invoice=create_crypto_invoice(order_id,amount,f"FluxVPN balance top-up {amount} RUB");invoice_id=str(invoice.get("invoice_id") or invoice.get("id"));url=invoice.get("bot_invoice_url") or invoice.get("pay_url") or invoice.get("mini_app_invoice_url")
        dbx.run("update app_orders set external_id=:external where id=:id",external=invoice_id,id=order_id);result.update(invoice_id=invoice_id,payment_url=url)
    else: raise ValueError("invalid top-up method")
    return result

def fulfill_order(dbx,order_id,method,external_id):
    rows=dbx.run("select id,telegram_id,kind,days,amount,status,promo_code,plan_code from app_orders where id=:id for update",id=int(order_id))
    if not rows:raise ValueError("order not found")
    order=rows[0]
    if order[5]=="paid":return db.get_user(dbx,order[1])
    if order[5] not in ("pending","checking"):raise ValueError("order cannot be paid")
    kind,days,amount,promo,plan_code=order[2],order[3],float(order[4]),order[6],order[7]
    if method=="balance":db.balance_change(dbx,order[1],-amount,"purchase","Premium purchase",order_id)
    dbx.run("update app_orders set status='paid',updated_at=now(),external_id=coalesce(:external,external_id) where id=:id",external=external_id,id=order_id)
    dbx.run("insert into app_payments(order_id,telegram_id,kind,amount,method,external_id) values(:order,:user,:kind,:amount,:method,:external) on conflict(external_id) do nothing",order=order_id,user=order[1],kind=kind,amount=amount,method=method,external=external_id)
    if kind=="topup":
        db.balance_change(dbx,order[1],amount,"topup","Balance top-up",order_id)
        return db.get_user(dbx,order[1])
    family=plan_code=="family"
    user=db.extend_subscription(dbx,order[1],days,"Family" if family else "Premium",FAMILY_DEVICE_LIMIT if family else PREMIUM_DEVICE_LIMIT)
    dbx.run("update app_users set discount_percent=0 where telegram_id=:id",id=order[1])
    if promo:
        dbx.run("insert into app_promo_redemptions(code,telegram_id,order_id) values(:code,:user,:order) on conflict do nothing",code=promo,user=order[1],order=order_id)
        dbx.run("update app_promos set used_count=used_count+1 where code=:code",code=promo)
    db.reward_referrer(dbx,order[1],order_id,amount)
    return user
def check_crypto_order(dbx,order_id):
    rows=dbx.run("select external_id,status from app_orders where id=:id",id=int(order_id))
    if not rows:raise ValueError("order not found")
    if rows[0][1]=="paid":return "paid"
    invoice=get_crypto_invoice(rows[0][0])
    if invoice and invoice.get("status")=="paid":fulfill_order(dbx,order_id,"crypto","crypto:"+str(rows[0][0]));return "paid"
    return invoice.get("status","pending") if invoice else "pending"
def redeem_promo(dbx,user,code):
    code=code.strip().upper();rows=dbx.run("select kind,value,max_uses,used_count,active,expires_at from app_promos where code=:code",code=code)
    if not rows:raise ValueError("promo not found")
    kind,value,maximum,used,active,expires=rows[0]
    if not active or used>=maximum or (expires and expires<datetime.now(timezone.utc)):raise ValueError("promo unavailable")
    if dbx.run("select 1 from app_promo_redemptions where code=:code and telegram_id=:id",code=code,id=user["telegram_id"]):raise ValueError("promo already used")
    if kind=="days":db.extend_subscription(dbx,user["telegram_id"],int(value),"Promo")
    elif kind=="balance":db.balance_change(dbx,user["telegram_id"],float(value),"promo",f"Promo {code}")
    elif kind=="percent":dbx.run("update app_users set discount_percent=greatest(discount_percent,:value) where telegram_id=:id",value=value,id=user["telegram_id"])
    else:raise ValueError("use this promo during checkout")
    dbx.run("insert into app_promo_redemptions(code,telegram_id) values(:code,:id)",code=code,id=user["telegram_id"]);dbx.run("update app_promos set used_count=used_count+1 where code=:code",code=code)
    return {"kind":kind,"value":float(value)}


def create_gift(dbx,creator_id,kind,value):
    creator=db.get_user(dbx,creator_id)
    if not creator:raise ValueError("creator not found")
    if kind=="subscription":
        days=int(value);cost=float(calculate_price(days));gift_value=days
    elif kind=="balance":
        gift_value=float(value)
        if gift_value not in (50,100,200,500,1000):raise ValueError("invalid gift amount")
        cost=gift_value
    else:raise ValueError("invalid gift kind")
    recent=dbx.run("select token from app_gifts where creator_id=:creator and kind=:kind and value=:value and status='pending' and created_at>now()-interval '10 minutes' order by created_at desc limit 1",creator=int(creator_id),kind=kind,value=gift_value)
    if recent:token=recent[0][0]
    else:
        token=secrets.token_urlsafe(18)
        dbx.run("insert into app_gifts(token,creator_id,kind,value,cost) values(:token,:creator,:kind,:value,:cost)",token=token,creator=int(creator_id),kind=kind,value=gift_value,cost=cost)
    return {"token":token,"kind":kind,"value":gift_value,"cost":cost}

def claim_gift(dbx,token,claimer_id):
    try:
        dbx.run("begin")
        rows=dbx.run("select creator_id,kind,value,cost,status,expires_at from app_gifts where token=:token for update",token=token)
        if not rows:raise ValueError("Подарок не найден")
        creator,kind,value,cost,status,expires=rows[0]
        if status!="pending":raise ValueError("Подарок уже использован")
        if int(creator)==int(claimer_id):raise ValueError("Нельзя забрать собственный подарок")
        if expires and expires<datetime.now(timezone.utc):raise ValueError("Срок подарка истёк")
        db.balance_change(dbx,creator,-float(cost),"gift_sent",f"Gift to {claimer_id}")
        if kind=="subscription":
            recipient=db.get_user(dbx,claimer_id);family=bool(recipient and recipient.get("plan")=="Family" and db.active(recipient))
            db.extend_subscription(dbx,claimer_id,int(value),"Family" if family else "Gift",FAMILY_DEVICE_LIMIT if family else PREMIUM_DEVICE_LIMIT)
        else:db.balance_change(dbx,claimer_id,float(value),"gift_received",f"Gift from {creator}")
        dbx.run("update app_gifts set status='claimed',claimed_by=:claimer,claimed_at=now() where token=:token",claimer=int(claimer_id),token=token)
        dbx.run("commit")
        return {"kind":kind,"value":float(value),"creator":int(creator)}
    except Exception:
        try:dbx.run("rollback")
        except Exception:pass
        raise

def set_auto_renew(dbx,user_id,enabled,days=30,plan="premium"):
    calculate_plan_price(days,plan)
    dbx.run("update app_subscriptions set auto_renew=:enabled,auto_renew_days=:days,auto_renew_plan=:plan,updated_at=now() where telegram_id=:id",enabled=bool(enabled),days=int(days),plan=plan,id=int(user_id))
    return db.get_user(dbx,user_id)

def process_auto_renew(dbx,user_id):
    try:
        dbx.run("begin")
        rows=dbx.run("select auto_renew,auto_renew_days,auto_renew_plan,expires_at from app_subscriptions where telegram_id=:id for update",id=int(user_id))
        if not rows or not rows[0][0] or not rows[0][3]:dbx.run("rollback");return None
        enabled,days,plan,expires=rows[0];now=datetime.now(timezone.utc)
        if expires.tzinfo is None:expires=expires.replace(tzinfo=timezone.utc)
        if expires>now+timedelta(hours=1):dbx.run("rollback");return None
        price=float(calculate_plan_price(days,plan))
        db.balance_change(dbx,user_id,-price,"auto_renew",f"Auto-renew {plan} {days} days")
        family=plan=="family"
        db.extend_subscription(dbx,user_id,days,"Family" if family else "Premium",FAMILY_DEVICE_LIMIT if family else PREMIUM_DEVICE_LIMIT)
        dbx.run("commit")
        return {"days":int(days),"plan":plan,"amount":price}
    except Exception:
        try:dbx.run("rollback")
        except Exception:pass
        raise

