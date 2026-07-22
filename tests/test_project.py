import hashlib,hmac,json,os,sys,time,types,urllib.parse
os.environ.update(BOT_TOKEN='123:testtoken',DATABASE_URL='postgresql://u:p@localhost/db',PUBLIC_URL='https://example.com',ADMIN_ID='6049379160')
pg=types.ModuleType('pg8000');native=types.ModuleType('pg8000.native');native.Connection=object;pg.native=native;sys.modules['pg8000']=pg;sys.modules['pg8000.native']=native
from backend.auth import verify_init_data
from backend.config import ADMIN_ID
from backend import db,services

def signed(user):
    values={'auth_date':str(int(time.time())),'query_id':'q','user':json.dumps(user,separators=(',',':'))}
    check='\n'.join(f'{k}={values[k]}' for k in sorted(values));secret=hmac.new(b'WebAppData',os.environ['BOT_TOKEN'].encode(),hashlib.sha256).digest();values['hash']=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest();return urllib.parse.urlencode(values)
assert verify_init_data(signed({'id':6049379160,'username':'owner'}))['id']==ADMIN_ID
try:verify_init_data(signed({'id':1})+'x')
except Exception:pass
else:raise AssertionError('tampered initData accepted')
assert services.calculate_price(7)==50 and services.calculate_price(30)==200 and services.calculate_price(90)==400 and services.calculate_price(365)==800
assert services.calculate_price(45)>200
versions=[m[0] for m in db.MIGRATIONS];assert versions==sorted(set(versions)) and len(versions)>=4
sql='\n'.join(statement for _,_,items in db.MIGRATIONS for statement in items)
for table in ('app_users','app_subscriptions','app_orders','app_payments','app_balance_transactions','app_promos','app_tickets','app_admin_audit'):assert table in sql
source=open('backend/app.py',encoding='utf-8').read()
for route in ('/api/bootstrap','/api/trial','/api/orders','/api/topups','/api/promos/redeem','/api/tickets','/api/admin/dashboard','/api/admin/users','/api/admin/orders','/api/admin/promos','/api/admin/servers','/api/admin/broadcast'):assert route in source,route
bot=open('backend/app.py',encoding='utf-8').read();assert 'Привет, <b>' in bot and 'miniapp_keyboard' in bot
print('PYTHON TESTS PASSED')
