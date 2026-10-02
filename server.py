import argparse, base64, csv, hashlib, hmac, json, os, re, secrets, sqlite3, threading, time, urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT=Path(__file__).resolve().parent; DATA=ROOT/'data'; RUNTIME=ROOT/'runtime'; DB=DATA/'laundry.sqlite3'
DATA.mkdir(exist_ok=True); RUNTIME.mkdir(exist_ok=True)
CATALOG=json.loads((ROOT/'prices.json').read_text(encoding='utf-8'))
VAT=Decimal('0.05'); STATUSES=['received','washing','drying','ironing','ready','collected','cancelled']
LOCK=threading.RLock(); LOGIN={}

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def money(v): return Decimal(str(v)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try: return super().__exit__(exc_type, exc, tb)
        finally: self.close()
def db():
    c=sqlite3.connect(DB, timeout=15, isolation_level=None, check_same_thread=False, factory=ClosingConnection); c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON'); c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=15000'); return c
@contextmanager
def tx():
    c=db()
    try: c.execute('BEGIN IMMEDIATE'); yield c; c.commit()
    except: c.rollback(); raise
    finally: c.close()
def hashpw(p,s=None):
    s=s or secrets.token_bytes(16); return s.hex(),hashlib.pbkdf2_hmac('sha256',p.encode(),s,310000).hex()
def normalize_phone(v):
    d=re.sub(r'\D','',str(v or ''))
    if d.startswith('00971'): d=d[2:]
    elif d.startswith('971'): pass
    elif d.startswith('05'): d='971'+d[1:]
    elif d.startswith('5') and len(d)==9: d='971'+d
    if not re.fullmatch(r'9715\d{8}',d): raise ValueError('Enter a valid UAE mobile number')
    return '+'+d
def mask_phone(p): return p[:4]+'*****'+p[-3:]
def price_for(item):
    cat=item.get('category'); name=item.get('item'); size=item.get('size') or ''; service=item.get('service')
    try: node=CATALOG[cat][name]; node=node['sizes'][size] if 'sizes' in node else node
    except KeyError: raise ValueError('Invalid catalog selection')
    if service in node: unit=Decimal(str(node[service]))
    elif service=='غسيل + كوي' and 'غسيل' in node and 'كوي' in node: unit=Decimal(str(node['غسيل']))+Decimal(str(node['كوي']))
    else: raise ValueError('Invalid service')
    qty=int(item.get('quantity',1));
    if qty<1 or qty>999: raise ValueError('Invalid quantity')
    width=height=None; factor=Decimal(qty)
    if name=='سجادة (م2)':
        width=Decimal(str(item.get('width',0))); height=Decimal(str(item.get('height',0)))
        if width<=0 or height<=0 or width>100 or height>100: raise ValueError('Invalid carpet dimensions')
        factor*=width*height
    return money(unit),money(unit*factor),qty,width,height
def init_db():
    schema='''
    CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY,name TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN('staff','admin')),salt TEXT NOT NULL,password_hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,username TEXT NOT NULL REFERENCES users(username),csrf TEXT NOT NULL,expires_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY,name TEXT NOT NULL,phone TEXT UNIQUE NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY,order_ref TEXT UNIQUE NOT NULL,customer_id INTEGER NOT NULL REFERENCES customers(id),status TEXT NOT NULL,subtotal_cents INTEGER NOT NULL,vat_cents INTEGER NOT NULL,total_cents INTEGER NOT NULL,expected_at TEXT,notes TEXT,client_key TEXT UNIQUE NOT NULL,created_by TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS order_items(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES orders(id),category TEXT NOT NULL,item TEXT NOT NULL,size TEXT,service TEXT NOT NULL,quantity INTEGER NOT NULL,width TEXT,height TEXT,unit_cents INTEGER NOT NULL,line_cents INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS order_events(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES orders(id),status TEXT NOT NULL,note TEXT,actor TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES orders(id),amount_cents INTEGER NOT NULL,method TEXT NOT NULL CHECK(method IN('cash','card','bank')),client_key TEXT UNIQUE NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS whatsapp_events(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES orders(id),customer_id INTEGER NOT NULL,recipient_masked TEXT NOT NULL,message_type TEXT NOT NULL,template_name TEXT NOT NULL,template_language TEXT NOT NULL,idempotency_key TEXT UNIQUE NOT NULL,provider_message_id TEXT,status TEXT NOT NULL DEFAULT 'queued',retry_count INTEGER NOT NULL DEFAULT 0,failure_reason TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS audit_events(id INTEGER PRIMARY KEY,actor TEXT NOT NULL,action TEXT NOT NULL,entity_type TEXT NOT NULL,entity_id TEXT,details TEXT,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS refunds(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES orders(id),amount_cents INTEGER NOT NULL,method TEXT NOT NULL CHECK(method IN('cash','card','bank')),reason TEXT NOT NULL,client_key TEXT UNIQUE NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY,expense_date TEXT NOT NULL,category TEXT NOT NULL,description TEXT NOT NULL,amount_cents INTEGER NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS business_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_orders_status_date ON orders(status,created_at); CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id); CREATE INDEX IF NOT EXISTS idx_customer_phone ON customers(phone);
    '''
    with tx() as c: c.executescript(schema)
    with tx() as c:
        for table,column,definition in [
            ('customers','important_notes',"TEXT NOT NULL DEFAULT ''"),
            ('orders','discount_cents','INTEGER NOT NULL DEFAULT 0'),
            ('orders','updated_by',"TEXT NOT NULL DEFAULT ''"),
            ('users','permissions',"TEXT NOT NULL DEFAULT '[]'"),
            ('users','last_login','TEXT')]:
            if column not in {x['name'] for x in c.execute(f'PRAGMA table_info({table})')}:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        defaults={'business_phone':'','tax_number':'','receipt_footer':'Thank you · شكراً لكم'}
        for k,v in defaults.items():c.execute('INSERT OR IGNORE INTO business_settings VALUES(?,?,?)',(k,v,now()))
        c.execute("UPDATE users SET permissions='[\"orders.create\",\"orders.status\",\"payments.create\",\"orders.cancel\"]' WHERE role='staff' AND permissions='[]'")
        c.execute("UPDATE users SET permissions='[\"*\"]' WHERE role='admin' AND permissions='[]'")
    with db() as c:
        if c.execute('SELECT count(*) FROM users').fetchone()[0]: return
    admin=secrets.token_urlsafe(12); staff=secrets.token_urlsafe(12)
    with tx() as c:
        for u,n,r,p in [('admin','Owner','admin',admin),('staff','Reception','staff',staff)]:
            s,h=hashpw(p); c.execute('INSERT INTO users(username,name,role,salt,password_hash,active,created_at) VALUES(?,?,?,?,?,?,?)',(u,n,r,s,h,1,now()))
    (RUNTIME/'credentials.txt').write_text(f'ADMIN username: admin\nADMIN password: {admin}\nSTAFF username: staff\nSTAFF password: {staff}\n',encoding='utf-8')
def queue_event(c,oid,cid,phone,kind,language='ar'):
    templates={'received':'laundry_order_received','ready':'laundry_order_ready','collected':'laundry_order_collected','cancelled':'laundry_order_cancelled','payment_reminder':'laundry_payment_reminder'}
    key=f'{oid}:{kind}'
    c.execute('INSERT OR IGNORE INTO whatsapp_events(order_id,customer_id,recipient_masked,message_type,template_name,template_language,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(oid,cid,mask_phone(phone),kind,templates[kind],language,key,now(),now()))
def audit(c,actor,action,etype,eid,details=''): c.execute('INSERT INTO audit_events(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)',(actor,action,etype,str(eid),details,now()))
def allowed(user,permission):
    try: permissions=json.loads(user.get('permissions') or '[]')
    except Exception: permissions=[]
    return user.get('role')=='admin' or '*' in permissions or permission in permissions
def order_row(c,oid):
    r=c.execute('''SELECT o.*,c.name customer_name,c.phone,c.important_notes customer_notes,COALESCE((SELECT SUM(amount_cents) FROM payments p WHERE p.order_id=o.id),0)-COALESCE((SELECT SUM(amount_cents) FROM refunds r WHERE r.order_id=o.id),0) paid_cents,COALESCE((SELECT SUM(amount_cents) FROM refunds r WHERE r.order_id=o.id),0) refunded_cents FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.id=?''',(oid,)).fetchone()
    if not r:return None
    d=dict(r); d['items']=[dict(x) for x in c.execute('SELECT * FROM order_items WHERE order_id=?',(oid,))]; d['balance_cents']=d['total_cents']-d['paid_cents']; return d
def dispatch_whatsapp_events(limit=20):
    token=os.getenv('WHATSAPP_ACCESS_TOKEN','');phone_id=os.getenv('WHATSAPP_PHONE_NUMBER_ID','')
    if not token or not phone_id:return {'queued':True,'configured':False}
    sent=failed=0
    with tx() as c:
        events=c.execute("""SELECT w.*,c.phone,o.order_ref FROM whatsapp_events w JOIN customers c ON c.id=w.customer_id JOIN orders o ON o.id=w.order_id WHERE w.status IN ('queued','failed') AND w.retry_count<3 ORDER BY w.id LIMIT ?""",(limit,)).fetchall()
        for event in events:
            payload={'messaging_product':'whatsapp','to':event['phone'].lstrip('+'),'type':'template','template':{'name':event['template_name'],'language':{'code':event['template_language']}}}
            req=urllib.request.Request(f'https://graph.facebook.com/v22.0/{phone_id}/messages',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='POST')
            try:
                with urllib.request.urlopen(req,timeout=15) as response:result=json.loads(response.read())
                mid=result.get('messages',[{}])[0].get('id');c.execute("UPDATE whatsapp_events SET status='sent',provider_message_id=?,failure_reason=NULL,updated_at=? WHERE id=?",(mid,now(),event['id']));sent+=1
            except Exception as exc:c.execute("UPDATE whatsapp_events SET status='failed',retry_count=retry_count+1,failure_reason=?,updated_at=? WHERE id=?",(str(exc)[:300],now(),event['id']));failed+=1
    return {'configured':True,'sent':sent,'failed':failed}
def notification_worker():
    while True:
        try:
            with tx() as c:
                rows=c.execute("""SELECT o.id,o.customer_id,c.phone FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.status='ready' AND datetime(o.updated_at)<datetime('now','-1 day') AND o.total_cents>COALESCE((SELECT SUM(amount_cents) FROM payments p WHERE p.order_id=o.id),0)-COALESCE((SELECT SUM(amount_cents) FROM refunds r WHERE r.order_id=o.id),0)""").fetchall()
                for row in rows:
                    key=f"{row['id']}:payment_reminder:{datetime.now().strftime('%Y%m%d')}";c.execute("INSERT OR IGNORE INTO whatsapp_events(order_id,customer_id,recipient_masked,message_type,template_name,template_language,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(row['id'],row['customer_id'],mask_phone(row['phone']),'payment_reminder','laundry_payment_reminder','ar',key,now(),now()))
            dispatch_whatsapp_events()
        except Exception as exc:print(json.dumps({'time':now(),'event':'notification_worker_error','error':str(exc)[:200]}))
        time.sleep(60)

class App(SimpleHTTPRequestHandler):
    server_version='AlRamsLaundry/1.0'
    def log_message(self,fmt,*args): print(json.dumps({'time':now(),'remote':self.client_address[0],'event':fmt%args}))
    def send_json(self,obj,status=200,headers=None):
        b=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('X-Frame-Options','DENY'); self.send_header('Referrer-Policy','no-referrer');
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0')); 
        if n>1_000_000: raise ValueError('Request too large')
        return json.loads(self.rfile.read(n) or b'{}')
    def raw_body(self):
        n=int(self.headers.get('Content-Length','0'))
        if n>1_000_000: raise ValueError('Request too large')
        return self.rfile.read(n)
    def auth(self,role=None,csrf=False):
        cookie=SimpleCookie(self.headers.get('Cookie','')); raw=cookie.get('laundry_session'); token=raw.value if raw else ''
        with db() as c:r=c.execute('''SELECT u.username,u.name,u.role,u.permissions,s.csrf FROM sessions s JOIN users u ON u.username=s.username WHERE s.token_hash=? AND s.expires_at>? AND u.active=1''',(hashlib.sha256(token.encode()).hexdigest(),now())).fetchone()
        if not r:return None
        if role=='admin' and r['role']!='admin':return None
        if csrf and not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),r['csrf']):return None
        return dict(r)
    def do_GET(self):
        p=urlparse(self.path); path=p.path; q=parse_qs(p.query)
        if path=='/api/catalog': return self.send_json(CATALOG)
        if path=='/api/me':
            u=self.auth(); return self.send_json({'authenticated':bool(u),'user':u})
        if path=='/api/customer':
            if not (u:=self.auth()):return self.send_json({'error':'Unauthorized'},401)
            try: phone=normalize_phone(q.get('phone',[''])[0])
            except ValueError as e:return self.send_json({'error':str(e)},400)
            with db() as c:
                cust=c.execute('SELECT * FROM customers WHERE phone=?',(phone,)).fetchone(); orders=[] if not cust else [order_row(c,x['id']) for x in c.execute('SELECT id FROM orders WHERE customer_id=? ORDER BY id DESC LIMIT 20',(cust['id'],))]
                stats=None
                if cust:
                    stats=dict(c.execute("""SELECT COUNT(*) visits,COALESCE(SUM(CASE WHEN status!='cancelled' THEN total_cents ELSE 0 END),0) spent_cents FROM orders WHERE customer_id=?""",(cust['id'],)).fetchone())
                    stats['unpaid_cents']=sum(max(0,x['balance_cents']) for x in orders if x['status']!='cancelled')
                    pref=c.execute('''SELECT oi.item,COUNT(*) uses FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE o.customer_id=? GROUP BY oi.item ORDER BY uses DESC LIMIT 1''',(cust['id'],)).fetchone();stats['preferred_item']=pref['item'] if pref else None
            return self.send_json({'customer':dict(cust) if cust else None,'orders':orders,'stats':stats})
        if path in ('/api/orders','/api/admin/orders'):
            role='admin' if path.startswith('/api/admin') else None
            if not self.auth(role):return self.send_json({'error':'Unauthorized'},401)
            term=q.get('q',[''])[0].strip(); status=q.get('status',[''])[0]
            sql='SELECT o.id FROM orders o JOIN customers c ON c.id=o.customer_id WHERE 1=1'; args=[]
            if status: sql+=' AND o.status=?'; args.append(status)
            due=q.get('due',[''])[0]
            if due=='today':sql+=" AND date(o.expected_at)=date('now','localtime')"
            elif due=='overdue':sql+=" AND o.expected_at<datetime('now','localtime') AND o.status NOT IN ('collected','cancelled')"
            if term: sql+=' AND (o.order_ref LIKE ? OR c.phone LIKE ? OR c.name LIKE ?)'; args += ['%'+term+'%']*3
            sql+=' ORDER BY o.id DESC LIMIT 200'
            with db() as c: rows=[order_row(c,x['id']) for x in c.execute(sql,args)]
            return self.send_json({'orders':rows})
        detail=re.fullmatch(r'/api/admin/orders/(\d+)',path)
        if detail:
            if not self.auth('admin'):return self.send_json({'error':'Unauthorized'},401)
            oid=int(detail.group(1))
            with db() as c:
                order=order_row(c,oid)
                if not order:return self.send_json({'error':'Order not found'},404)
                order['payments']=[dict(x) for x in c.execute('SELECT * FROM payments WHERE order_id=? ORDER BY id',(oid,))]
                order['refunds']=[dict(x) for x in c.execute('SELECT * FROM refunds WHERE order_id=? ORDER BY id',(oid,))]
                order['events']=[dict(x) for x in c.execute('SELECT * FROM order_events WHERE order_id=? ORDER BY id',(oid,))]
                order['audit']=[dict(x) for x in c.execute("SELECT * FROM audit_events WHERE entity_type='order' AND entity_id=? ORDER BY id DESC",(str(oid),))]
            return self.send_json({'order':order})
        if path=='/api/admin/customers':
            if not self.auth('admin'):return self.send_json({'error':'Unauthorized'},401)
            term=q.get('q',[''])[0].strip(); args=[]; where=''
            if term:where='WHERE c.name LIKE ? OR c.phone LIKE ?';args=['%'+term+'%']*2
            with db() as c: rows=[dict(x) for x in c.execute(f'''SELECT c.*,COUNT(o.id) visits,COALESCE(SUM(CASE WHEN o.status!='cancelled' THEN o.total_cents ELSE 0 END),0) spent_cents,COALESCE(SUM(CASE WHEN o.status!='cancelled' THEN MAX(0,o.total_cents-COALESCE(p.paid,0)+COALESCE(r.refunded,0)) ELSE 0 END),0) unpaid_cents FROM customers c LEFT JOIN orders o ON o.customer_id=c.id LEFT JOIN(SELECT order_id,SUM(amount_cents) paid FROM payments GROUP BY order_id)p ON p.order_id=o.id LEFT JOIN(SELECT order_id,SUM(amount_cents) refunded FROM refunds GROUP BY order_id)r ON r.order_id=o.id {where} GROUP BY c.id ORDER BY c.updated_at DESC LIMIT 200''',args)]
            return self.send_json({'customers':rows})
        if path=='/api/admin/payments':
            if not self.auth('admin'):return self.send_json({'error':'Unauthorized'},401)
            with db() as c:
                payments=[dict(x) for x in c.execute('''SELECT p.*,o.order_ref,c.name customer_name FROM payments p JOIN orders o ON o.id=p.order_id JOIN customers c ON c.id=o.customer_id ORDER BY p.id DESC LIMIT 300''')]
                refunds=[dict(x) for x in c.execute('''SELECT r.*,o.order_ref,c.name customer_name FROM refunds r JOIN orders o ON o.id=r.order_id JOIN customers c ON c.id=o.customer_id ORDER BY r.id DESC LIMIT 300''')]
            return self.send_json({'payments':payments,'refunds':refunds})
        if path=='/api/admin/staff':
            if not self.auth('admin'):return self.send_json({'error':'Unauthorized'},401)
            with db() as c: rows=[dict(x) for x in c.execute('''SELECT u.username,u.name,u.role,u.active,u.permissions,u.created_at,u.last_login,COUNT(DISTINCT o.id) orders_created,COALESCE((SELECT SUM(p.amount_cents) FROM payments p WHERE p.actor=u.username),0) payments_received_cents FROM users u LEFT JOIN orders o ON o.created_by=u.username GROUP BY u.username ORDER BY u.role,u.name''')]
            return self.send_json({'staff':rows})
        if path=='/api/admin/audit':
            if not self.auth('admin'):return self.send_json({'error':'Unauthorized'},401)
            with db() as c: rows=[dict(x) for x in c.execute('SELECT * FROM audit_events ORDER BY id DESC LIMIT 200')]
            return self.send_json({'events':rows})
        if path=='/api/admin/expenses':
            if not self.auth('admin'):return self.send_json({'error':'Unauthorized'},401)
            with db() as c: rows=[dict(x) for x in c.execute('SELECT * FROM expenses ORDER BY expense_date DESC,id DESC LIMIT 500')]
            return self.send_json({'expenses':rows})
        if path=='/api/business-settings':
            if not self.auth():return self.send_json({'error':'Unauthorized'},401)
            with db() as c: settings={x['key']:x['value'] for x in c.execute('SELECT * FROM business_settings')}
            return self.send_json({'settings':settings})
        if path=='/api/admin/dashboard':
            if not self.auth('admin'):return self.send_json({'error':'Unauthorized'},401)
            start=q.get('date',[datetime.now().astimezone().date().isoformat()])[0]
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',start):return self.send_json({'error':'Invalid date'},400)
            with db() as c:
                metrics=dict(c.execute('''SELECT COUNT(*) orders,COALESCE(SUM(total_cents),0) gross_cents,COALESCE(SUM(vat_cents),0) vat_cents,COALESCE(SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END),0) ready FROM orders WHERE date(created_at)=?''',(start,)).fetchone())
                payments_today=c.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payments WHERE date(created_at)=?',(start,)).fetchone()[0];refunds_today=c.execute('SELECT COALESCE(SUM(amount_cents),0) FROM refunds WHERE date(created_at)=?',(start,)).fetchone()[0];metrics['paid_cents']=payments_today-refunds_today
                metrics['outstanding_cents']=c.execute("SELECT COALESCE(SUM(MAX(0,o.total_cents-COALESCE(p.paid,0)+COALESCE(r.refunded,0))),0) FROM orders o LEFT JOIN (SELECT order_id,SUM(amount_cents) paid FROM payments GROUP BY order_id)p ON p.order_id=o.id LEFT JOIN(SELECT order_id,SUM(amount_cents) refunded FROM refunds GROUP BY order_id)r ON r.order_id=o.id WHERE o.status!='cancelled'").fetchone()[0]
                by_status=[dict(x) for x in c.execute('SELECT status,COUNT(*) count FROM orders GROUP BY status')]
                by_method=[dict(x) for x in c.execute('''SELECT method,SUM(amount_cents) amount_cents FROM(SELECT method,amount_cents FROM payments WHERE date(created_at)=? UNION ALL SELECT method,-amount_cents FROM refunds WHERE date(created_at)=?)GROUP BY method''',(start,start))]
                wa=[dict(x) for x in c.execute('SELECT * FROM whatsapp_events ORDER BY id DESC LIMIT 50')]
                series=[dict(x) for x in c.execute("""WITH RECURSIVE days(d) AS (SELECT date('now','-29 days') UNION ALL SELECT date(d,'+1 day') FROM days WHERE d<date('now')) SELECT d date,COALESCE(SUM(CASE WHEN o.status!='cancelled' THEN o.total_cents ELSE 0 END),0) sales_cents,COUNT(o.id) orders FROM days LEFT JOIN orders o ON date(o.created_at)=d GROUP BY d ORDER BY d""")]
                top_services=[dict(x) for x in c.execute("""SELECT oi.item,oi.service,SUM(oi.quantity) quantity,SUM(oi.line_cents) sales_cents FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE o.status!='cancelled' GROUP BY oi.item,oi.service ORDER BY quantity DESC LIMIT 10""")]
                staff_performance=[dict(x) for x in c.execute("""SELECT created_by username,COUNT(*) orders,SUM(CASE WHEN status!='cancelled' THEN total_cents ELSE 0 END) sales_cents FROM orders GROUP BY created_by ORDER BY sales_cents DESC""")]
                unpaid=[dict(x) for x in c.execute("""SELECT o.order_ref,c.name,c.phone,o.total_cents-COALESCE(p.paid,0)+COALESCE(r.refunded,0) balance_cents FROM orders o JOIN customers c ON c.id=o.customer_id LEFT JOIN(SELECT order_id,SUM(amount_cents) paid FROM payments GROUP BY order_id)p ON p.order_id=o.id LEFT JOIN(SELECT order_id,SUM(amount_cents) refunded FROM refunds GROUP BY order_id)r ON r.order_id=o.id WHERE o.status!='cancelled' AND o.total_cents>COALESCE(p.paid,0)-COALESCE(r.refunded,0) ORDER BY balance_cents DESC LIMIT 100""")]
                expenses=c.execute("SELECT COALESCE(SUM(amount_cents),0) FROM expenses WHERE expense_date=?",(start,)).fetchone()[0];metrics['expenses_cents']=expenses;metrics['profit_cents']=metrics['paid_cents']-expenses
                metrics['due_today']=c.execute("SELECT COUNT(*) FROM orders WHERE date(expected_at)=? AND status NOT IN ('collected','cancelled')",(start,)).fetchone()[0]
                metrics['overdue']=c.execute("SELECT COUNT(*) FROM orders WHERE expected_at<datetime('now','localtime') AND status NOT IN ('collected','cancelled')").fetchone()[0]
                metrics['failed_whatsapp']=c.execute("SELECT COUNT(*) FROM whatsapp_events WHERE status='failed'").fetchone()[0]
                attention={
                    'overdue':[dict(x) for x in c.execute("SELECT o.id,o.order_ref,c.name,o.expected_at FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.expected_at<datetime('now','localtime') AND o.status NOT IN ('collected','cancelled') ORDER BY o.expected_at LIMIT 20")],
                    'ready':[dict(x) for x in c.execute("SELECT o.id,o.order_ref,c.name,o.expected_at FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.status='ready' ORDER BY o.updated_at LIMIT 20")],
                    'failed':[dict(x) for x in c.execute("SELECT id,order_id,message_type,failure_reason FROM whatsapp_events WHERE status='failed' ORDER BY id DESC LIMIT 20")],
                    'cancelled_refunds':[dict(x) for x in c.execute("SELECT o.id,o.order_ref,c.name,COALESCE(p.paid,0)-COALESCE(r.refunded,0) refundable_cents FROM orders o JOIN customers c ON c.id=o.customer_id LEFT JOIN(SELECT order_id,SUM(amount_cents) paid FROM payments GROUP BY order_id)p ON p.order_id=o.id LEFT JOIN(SELECT order_id,SUM(amount_cents) refunded FROM refunds GROUP BY order_id)r ON r.order_id=o.id WHERE o.status='cancelled' AND COALESCE(p.paid,0)>COALESCE(r.refunded,0) LIMIT 20")]
                }
            return self.send_json({'metrics':metrics,'by_status':by_status,'by_method':by_method,'whatsapp':wa,'series':series,'top_services':top_services,'staff_performance':staff_performance,'unpaid':unpaid,'attention':attention})
        if path=='/api/admin/report':
            if not self.auth('admin'):return self.send_json({'error':'Unauthorized'},401)
            end=q.get('to',[datetime.now().astimezone().date().isoformat()])[0]; period=q.get('period',['day'])[0]
            default_days={'day':0,'week':6,'month':29}.get(period,0); start=q.get('from',[(datetime.fromisoformat(end).date()-timedelta(days=default_days)).isoformat()])[0]
            with db() as c:
                summary=dict(c.execute("""SELECT COUNT(*) orders,COALESCE(SUM(CASE WHEN status!='cancelled' THEN subtotal_cents ELSE 0 END),0) subtotal_cents,COALESCE(SUM(CASE WHEN status!='cancelled' THEN vat_cents ELSE 0 END),0) vat_cents,COALESCE(SUM(CASE WHEN status!='cancelled' THEN total_cents ELSE 0 END),0) total_cents FROM orders WHERE date(created_at) BETWEEN ? AND ?""",(start,end)).fetchone())
                paid=c.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payments WHERE date(created_at) BETWEEN ? AND ?',(start,end)).fetchone()[0];refunded=c.execute('SELECT COALESCE(SUM(amount_cents),0) FROM refunds WHERE date(created_at) BETWEEN ? AND ?',(start,end)).fetchone()[0];summary['paid_cents']=paid-refunded;summary['refund_cents']=refunded
                services=[dict(x) for x in c.execute("""SELECT oi.item,oi.service,SUM(oi.quantity) quantity,SUM(oi.line_cents) sales_cents FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE o.status!='cancelled' AND date(o.created_at) BETWEEN ? AND ? GROUP BY oi.item,oi.service ORDER BY sales_cents DESC""",(start,end))]
                summary['expenses_cents']=c.execute('SELECT COALESCE(SUM(amount_cents),0) FROM expenses WHERE expense_date BETWEEN ? AND ?',(start,end)).fetchone()[0]
                summary['discount_cents']=c.execute("SELECT COALESCE(SUM(discount_cents),0) FROM orders WHERE status!='cancelled' AND date(created_at) BETWEEN ? AND ?",(start,end)).fetchone()[0]
                methods=[dict(x) for x in c.execute('''SELECT method,SUM(amount_cents) amount_cents FROM(SELECT method,amount_cents FROM payments WHERE date(created_at) BETWEEN ? AND ? UNION ALL SELECT method,-amount_cents FROM refunds WHERE date(created_at) BETWEEN ? AND ?)GROUP BY method''',(start,end,start,end))]
            summary['difference_cents']=summary['total_cents']-summary['paid_cents'];summary['profit_cents']=summary['paid_cents']-summary['expenses_cents'];return self.send_json({'from':start,'to':end,'summary':summary,'services':services,'by_method':methods})
        if path=='/api/admin/export.csv':
            if not self.auth('admin'):return self.send_json({'error':'Unauthorized'},401)
            with db() as c: rows=c.execute('''SELECT o.order_ref,c.name,c.phone,o.status,o.total_cents,COALESCE(SUM(p.amount_cents),0) paid_cents,o.created_at FROM orders o JOIN customers c ON c.id=o.customer_id LEFT JOIN payments p ON p.order_id=o.id GROUP BY o.id ORDER BY o.id DESC''').fetchall()
            import io; s=io.StringIO(); w=csv.writer(s); w.writerow(['reference','customer','phone','status','total','paid','created_at']); w.writerows(rows); b=s.getvalue().encode('utf-8-sig')
            self.send_response(200); self.send_header('Content-Type','text/csv');self.send_header('Content-Disposition','attachment; filename=orders.csv');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
        if path=='/api/whatsapp/webhook':
            if q.get('hub.mode',[''])[0]=='subscribe' and hmac.compare_digest(q.get('hub.verify_token',[''])[0],os.getenv('WHATSAPP_WEBHOOK_VERIFY_TOKEN','__unset__')):
                b=q.get('hub.challenge',[''])[0].encode();self.send_response(200);self.end_headers();self.wfile.write(b);return
            return self.send_json({'error':'Forbidden'},403)
        if path=='/credentials' or path.startswith('/data/') or path.startswith('/runtime/'):return self.send_json({'error':'Not found'},404)
        if path=='/': self.path='/staff.html'
        return super().do_GET()
    def do_POST(self):
        path=urlparse(self.path).path
        try:
            raw=self.raw_body(); data=json.loads(raw or b'{}')
        except Exception as e:return self.send_json({'error':'Invalid request'},400)
        if path=='/api/login':
            ip=self.client_address[0]; history=[x for x in LOGIN.get(ip,[]) if time.time()-x<300]; LOGIN[ip]=history
            if len(history)>=10:return self.send_json({'error':'Try again later'},429)
            with db() as c:r=c.execute('SELECT * FROM users WHERE username=? AND active=1',(str(data.get('username','')),)).fetchone()
            ok=False
            if r:
                calc=hashpw(str(data.get('password','')),bytes.fromhex(r['salt']))[1];ok=hmac.compare_digest(calc,r['password_hash'])
            if not ok:history.append(time.time());return self.send_json({'error':'Invalid credentials'},401)
            token=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(24); exp=(datetime.now(timezone.utc)+timedelta(hours=12)).isoformat(timespec='seconds')
            with tx() as c:
                c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),r['username'],csrf,exp))
                c.execute('UPDATE users SET last_login=? WHERE username=?',(now(),r['username']))
            return self.send_json({'user':{'username':r['username'],'name':r['name'],'role':r['role']},'csrf':csrf},headers={'Set-Cookie':f'laundry_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200'})
        if path=='/api/whatsapp/webhook':
            secret=os.getenv('META_APP_SECRET',''); sig=self.headers.get('X-Hub-Signature-256','')
            if not secret or not hmac.compare_digest(sig,'sha256='+hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()):return self.send_json({'error':'Forbidden'},403)
            statuses=[]
            for e in data.get('entry',[]):
                for ch in e.get('changes',[]): statuses += ch.get('value',{}).get('statuses',[])
            with tx() as c:
                for s in statuses:c.execute('UPDATE whatsapp_events SET status=?,failure_reason=?,updated_at=? WHERE provider_message_id=?',(s.get('status'),json.dumps(s.get('errors')) if s.get('errors') else None,now(),s.get('id')))
            return self.send_json({'ok':True})
        if not (u:=self.auth(csrf=True)):return self.send_json({'error':'Unauthorized or invalid CSRF token'},401)
        try:
            if path=='/api/logout':
                cookie=SimpleCookie(self.headers.get('Cookie','')); raw=cookie.get('laundry_session');
                if raw:
                    with tx() as c:c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(raw.value.encode()).hexdigest(),))
                return self.send_json({'ok':True},headers={'Set-Cookie':'laundry_session=; Path=/; Max-Age=0'})
            if path=='/api/orders':
                if not allowed(u,'orders.create'):return self.send_json({'error':'Forbidden'},403)
                name=str(data.get('customer_name','')).strip()[:100]; phone=normalize_phone(data.get('phone')); items=data.get('items',[]); key=str(data.get('client_key',''))
                if len(name)<2 or not items or not key:raise ValueError('Customer, phone and items are required')
                calculated=[]; subtotal=Decimal('0')
                for i in items:
                    unit,line,qty,w,h=price_for(i);subtotal+=line;calculated.append((i,unit,line,qty,w,h))
                subtotal=money(subtotal); vat=money(subtotal*VAT); total=subtotal+vat
                with tx() as c:
                    old=c.execute('SELECT id FROM orders WHERE client_key=?',(key,)).fetchone()
                    if old:return self.send_json({'order':order_row(c,old['id']),'duplicate':True},200)
                    t=now(); c.execute('INSERT INTO customers(name,phone,created_at,updated_at) VALUES(?,?,?,?) ON CONFLICT(phone) DO UPDATE SET name=excluded.name,updated_at=excluded.updated_at',(name,phone,t,t));cid=c.execute('SELECT id FROM customers WHERE phone=?',(phone,)).fetchone()[0]
                    ref='LR-'+datetime.now().strftime('%Y%m%d')+'-'+secrets.token_hex(2).upper()
                    c.execute('INSERT INTO orders(order_ref,customer_id,status,subtotal_cents,vat_cents,total_cents,expected_at,notes,client_key,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(ref,cid,'received',int(subtotal*100),int(vat*100),int(total*100),data.get('expected_at'),str(data.get('notes',''))[:1000],key,u['username'],t,t));oid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
                    for i,unit,line,qty,w,h in calculated:c.execute('INSERT INTO order_items(order_id,category,item,size,service,quantity,width,height,unit_cents,line_cents) VALUES(?,?,?,?,?,?,?,?,?,?)',(oid,i['category'],i['item'],i.get('size'),i['service'],qty,str(w) if w else None,str(h) if h else None,int(unit*100),int(line*100)))
                    c.execute('INSERT INTO order_events(order_id,status,note,actor,created_at) VALUES(?,?,?,?,?)',(oid,'received','Order created',u['username'],t));queue_event(c,oid,cid,phone,'received');audit(c,u['username'],'create','order',oid)
                    amount=money(data.get('paid',0) or 0); method=data.get('payment_method')
                    if amount>0:
                        if method not in ('cash','card','bank') or amount>total:raise ValueError('Invalid initial payment')
                        c.execute('INSERT INTO payments(order_id,amount_cents,method,client_key,actor,created_at) VALUES(?,?,?,?,?,?)',(oid,int(amount*100),method,key+':payment',u['username'],t))
                    result=order_row(c,oid)
                return self.send_json({'order':result},201)
            cm=re.fullmatch(r'/api/customers/(\d+)/notes',path)
            if cm:
                cid=int(cm.group(1));notes=str(data.get('notes',''))[:2000]
                with tx() as c:c.execute('UPDATE customers SET important_notes=?,updated_at=? WHERE id=?',(notes,now(),cid));audit(c,u['username'],'notes','customer',cid)
                return self.send_json({'ok':True})
            em=re.fullmatch(r'/api/orders/(\d+)/edit',path)
            if em:
                oid=int(em.group(1));items=data.get('items',[])
                if not items:raise ValueError('Items are required')
                calculated=[];subtotal=Decimal('0')
                for i in items:unit,line,qty,w,h=price_for(i);subtotal+=line;calculated.append((i,unit,line,qty,w,h))
                subtotal=money(subtotal);vat=money(subtotal*VAT)
                with tx() as c:
                    order=order_row(c,oid)
                    if not order or order['status']!='received':raise ValueError('Only received orders can be edited')
                    discount=Decimal(order['discount_cents'])/100;total=max(Decimal('0'),subtotal+vat-discount)
                    if order['paid_cents']>int(total*100):raise ValueError('Edited total cannot be less than paid amount')
                    c.execute('DELETE FROM order_items WHERE order_id=?',(oid,))
                    for i,unit,line,qty,w,h in calculated:c.execute('INSERT INTO order_items(order_id,category,item,size,service,quantity,width,height,unit_cents,line_cents) VALUES(?,?,?,?,?,?,?,?,?,?)',(oid,i['category'],i['item'],i.get('size'),i['service'],qty,str(w) if w else None,str(h) if h else None,int(unit*100),int(line*100)))
                    c.execute('UPDATE orders SET subtotal_cents=?,vat_cents=?,total_cents=?,expected_at=?,notes=?,updated_by=?,updated_at=? WHERE id=?',(int(subtotal*100),int(vat*100),int(total*100),data.get('expected_at'),str(data.get('notes',''))[:1000],u['username'],now(),oid));audit(c,u['username'],'edit','order',oid)
                    result=order_row(c,oid)
                return self.send_json({'order':result})
            dm=re.fullmatch(r'/api/admin/orders/(\d+)/discount',path)
            if dm:
                if u['role']!='admin':return self.send_json({'error':'Forbidden'},403)
                oid=int(dm.group(1));discount=money(data.get('amount',0))
                with tx() as c:
                    order=order_row(c,oid)
                    if not order or discount<0 or int(discount*100)>order['subtotal_cents']+order['vat_cents']:raise ValueError('Invalid discount')
                    total=order['subtotal_cents']+order['vat_cents']-int(discount*100)
                    if total<order['paid_cents']:raise ValueError('Discount exceeds remaining balance')
                    c.execute('UPDATE orders SET discount_cents=?,total_cents=?,updated_by=?,updated_at=? WHERE id=?',(int(discount*100),total,u['username'],now(),oid));audit(c,u['username'],'discount','order',oid,str(discount));result=order_row(c,oid)
                return self.send_json({'order':result})
            rm=re.fullmatch(r'/api/admin/orders/(\d+)/refund',path)
            if rm:
                if u['role']!='admin':return self.send_json({'error':'Forbidden'},403)
                oid=int(rm.group(1));amount=money(data.get('amount'));method=data.get('method');key=str(data.get('client_key',''));reason=str(data.get('reason',''))[:500]
                with tx() as c:
                    order=order_row(c,oid)
                    if not order or amount<=0 or int(amount*100)>order['paid_cents'] or method not in ('cash','card','bank') or not key or not reason:raise ValueError('Invalid refund')
                    c.execute('INSERT INTO refunds(order_id,amount_cents,method,reason,client_key,actor,created_at) VALUES(?,?,?,?,?,?,?)',(oid,int(amount*100),method,reason,key,u['username'],now()));audit(c,u['username'],'refund','order',oid,str(amount));result=order_row(c,oid)
                return self.send_json({'order':result})
            m=re.fullmatch(r'/api/orders/(\d+)/(status|payment)',path)
            if m:
                oid=int(m.group(1)); action=m.group(2)
                with tx() as c:
                    order=order_row(c,oid)
                    if not order:raise ValueError('Order not found')
                    if action=='status':
                        status=data.get('status')
                        if status=='cancelled' and not allowed(u,'orders.cancel'):return self.send_json({'error':'Forbidden'},403)
                        if status!='cancelled' and not allowed(u,'orders.status'):return self.send_json({'error':'Forbidden'},403)
                        if status not in STATUSES:raise ValueError('Invalid status')
                        current=STATUSES.index(order['status']); target=STATUSES.index(status)
                        if status!='cancelled' and target<current:raise ValueError('Status cannot move backwards')
                        c.execute('UPDATE orders SET status=?,updated_at=? WHERE id=?',(status,now(),oid));c.execute('INSERT INTO order_events(order_id,status,note,actor,created_at) VALUES(?,?,?,?,?)',(oid,status,str(data.get('note',''))[:500],u['username'],now()))
                        if status in ('ready','collected','cancelled'):queue_event(c,oid,order['customer_id'],order['phone'],status)
                        audit(c,u['username'],'status','order',oid,status)
                    else:
                        if not allowed(u,'payments.create'):return self.send_json({'error':'Forbidden'},403)
                        if order['status']=='cancelled':raise ValueError('Cannot pay a cancelled order')
                        amount=money(data.get('amount')); method=data.get('method'); key=str(data.get('client_key',''))
                        if amount<=0 or method not in ('cash','card','bank') or not key:raise ValueError('Invalid payment')
                        if int(amount*100)>order['balance_cents']:raise ValueError('Payment exceeds balance')
                        c.execute('INSERT INTO payments(order_id,amount_cents,method,client_key,actor,created_at) VALUES(?,?,?,?,?,?)',(oid,int(amount*100),method,key,u['username'],now()));audit(c,u['username'],'payment','order',oid,str(amount))
                    result=order_row(c,oid)
                return self.send_json({'order':result})
            if path=='/api/admin/staff/save':
                if u['role']!='admin':return self.send_json({'error':'Forbidden'},403)
                username=re.sub(r'[^a-z0-9_.-]','',str(data.get('username','')).lower())[:40];name=str(data.get('name','')).strip()[:100];role=data.get('role','staff');active=1 if data.get('active',True) else 0;permissions=data.get('permissions',[]);password=str(data.get('password',''))
                if not username or len(name)<2 or role not in ('staff','admin') or not isinstance(permissions,list):raise ValueError('Invalid staff account')
                if username==u['username'] and not active:raise ValueError('You cannot deactivate your own account')
                with tx() as c:
                    existing=c.execute('SELECT username FROM users WHERE username=?',(username,)).fetchone()
                    current=c.execute('SELECT role,active FROM users WHERE username=?',(username,)).fetchone()
                    if current and current['role']=='admin' and current['active'] and (role!='admin' or not active) and c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]<=1:raise ValueError('At least one active administrator is required')
                    if existing:
                        c.execute('UPDATE users SET name=?,role=?,active=?,permissions=? WHERE username=?',(name,role,active,json.dumps(permissions),username))
                        if password:
                            salt,pwh=hashpw(password);c.execute('UPDATE users SET salt=?,password_hash=? WHERE username=?',(salt,pwh,username))
                    else:
                        if len(password)<4:raise ValueError('Password must have at least 4 characters')
                        salt,pwh=hashpw(password);c.execute('INSERT INTO users(username,name,role,salt,password_hash,active,created_at,permissions) VALUES(?,?,?,?,?,?,?,?)',(username,name,role,salt,pwh,active,now(),json.dumps(permissions)))
                    if not active:c.execute('DELETE FROM sessions WHERE username=?',(username,))
                    audit(c,u['username'],'save','user',username)
                return self.send_json({'ok':True})
            if path=='/api/admin/expenses':
                if u['role']!='admin':return self.send_json({'error':'Forbidden'},403)
                amount=money(data.get('amount'));date=str(data.get('date',''));category=str(data.get('category',''))[:100];description=str(data.get('description',''))[:500]
                if amount<=0 or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date) or not category:raise ValueError('Invalid expense')
                with tx() as c:c.execute('INSERT INTO expenses(expense_date,category,description,amount_cents,actor,created_at) VALUES(?,?,?,?,?,?)',(date,category,description,int(amount*100),u['username'],now()));audit(c,u['username'],'create','expense',c.execute('SELECT last_insert_rowid()').fetchone()[0])
                return self.send_json({'ok':True},201)
            if path=='/api/admin/business-settings':
                if u['role']!='admin':return self.send_json({'error':'Forbidden'},403)
                with tx() as c:
                    for key in ('business_phone','tax_number','receipt_footer'):
                        if key in data:c.execute('INSERT INTO business_settings VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',(key,str(data[key])[:500],now()))
                    audit(c,u['username'],'update','settings','business')
                return self.send_json({'ok':True})
            wm=re.fullmatch(r'/api/orders/(\d+)/whatsapp',path)
            if wm:
                oid=int(wm.group(1));kind=str(data.get('type','payment_reminder'))
                if kind not in ('received','ready','collected','cancelled','payment_reminder'):raise ValueError('Invalid message type')
                with tx() as c:
                    order=order_row(c,oid)
                    if not order:raise ValueError('Order not found')
                    if kind=='payment_reminder':
                        key=f'{oid}:{kind}:{datetime.now().strftime("%Y%m%d")}'
                        c.execute('INSERT OR IGNORE INTO whatsapp_events(order_id,customer_id,recipient_masked,message_type,template_name,template_language,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(oid,order['customer_id'],mask_phone(order['phone']),kind,'laundry_payment_reminder','ar',key,now(),now()))
                    else:queue_event(c,oid,order['customer_id'],order['phone'],kind)
                    audit(c,u['username'],'queue','whatsapp',oid,kind)
                return self.send_json({'ok':True})
            if path=='/api/admin/whatsapp/dispatch':
                if u['role']!='admin':return self.send_json({'error':'Forbidden'},403)
                return self.send_json(dispatch_whatsapp_events())
        except sqlite3.IntegrityError:return self.send_json({'error':'Duplicate request prevented'},409)
        except (ValueError,decimal.InvalidOperation if False else ValueError) as e:return self.send_json({'error':str(e)},400)
        return self.send_json({'error':'Not found'},404)

if __name__=='__main__':
    init_db();ap=argparse.ArgumentParser();ap.add_argument('--port',type=int,default=8875);args=ap.parse_args();os.chdir(ROOT);threading.Thread(target=notification_worker,daemon=True).start();print(f'Al Rams Laundry running at http://127.0.0.1:{args.port}');ThreadingHTTPServer(('127.0.0.1',args.port),App).serve_forever()
