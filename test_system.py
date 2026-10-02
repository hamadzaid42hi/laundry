import sqlite3, tempfile, unittest
from decimal import Decimal
from pathlib import Path
import server

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); server.DB=Path(self.tmp.name)/'test.sqlite3'; server.RUNTIME=Path(self.tmp.name); server.init_db()
    def tearDown(self): self.tmp.cleanup()
    def test_uae_phone_variants(self):
        for value in ('0501234567','501234567','971501234567','+971 50 123 4567'): self.assertEqual(server.normalize_phone(value),'+971501234567')
    def test_invalid_phone(self):
        with self.assertRaises(ValueError): server.normalize_phone('123')
    def test_catalog_has_25_items(self): self.assertEqual(sum(len(x) for x in server.CATALOG.values()),25)
    def test_combined_price_is_sum(self):
        u,line,*_=server.price_for({'category':'ملابس رجالية','item':'كندورة','service':'غسيل + كوي','quantity':2})
        self.assertEqual(u,Decimal('5.00'));self.assertEqual(line,Decimal('10.00'))
    def test_explicit_combined_price(self):
        u,line,*_=server.price_for({'category':'ملابس نسائية','item':'فستان نسائي','size':'Medium','service':'غسيل + كوي','quantity':1})
        self.assertEqual(u,Decimal('15.00'))
    def test_carpet_area_price(self):
        u,line,*_=server.price_for({'category':'إكسسوارات ومفروشات','item':'سجادة (م2)','service':'غسيل بالمتر','quantity':2,'width':2,'height':3})
        self.assertEqual(u,Decimal('8.00'));self.assertEqual(line,Decimal('96.00'))
    def test_vat_rounding(self): self.assertEqual(server.money(Decimal('15')*server.VAT),Decimal('.75'))
    def test_password_hash(self):
        salt,h=server.hashpw('secret');self.assertEqual(server.hashpw('secret',bytes.fromhex(salt))[1],h);self.assertNotEqual(server.hashpw('wrong',bytes.fromhex(salt))[1],h)
    def test_notification_deduplication(self):
        with server.tx() as c:
            s,h=server.hashpw('x');c.execute("INSERT OR IGNORE INTO users(username,name,role,salt,password_hash,active,created_at) VALUES('x','x','staff',?,?,1,?)",(s,h,server.now()));c.execute("INSERT INTO customers(name,phone,created_at,updated_at) VALUES('A','+971501234567',?,?)",(server.now(),server.now()));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute("INSERT INTO orders(order_ref,customer_id,status,subtotal_cents,vat_cents,total_cents,client_key,created_by,created_at,updated_at) VALUES('LR-X',?,'received',100,5,105,'k','x',?,?)",(cid,server.now(),server.now()));oid=c.execute('SELECT last_insert_rowid()').fetchone()[0];server.queue_event(c,oid,cid,'+971501234567','received');server.queue_event(c,oid,cid,'+971501234567','received');self.assertEqual(c.execute('SELECT count(*) FROM whatsapp_events').fetchone()[0],1)
    def test_duplicate_payment_key(self):
        with server.tx() as c:
            s,h=server.hashpw('x');c.execute("INSERT OR IGNORE INTO users(username,name,role,salt,password_hash,active,created_at) VALUES('x','x','staff',?,?,1,?)",(s,h,server.now()));c.execute("INSERT INTO customers(name,phone,created_at,updated_at) VALUES('A','+971501234567',?,?)",(server.now(),server.now()));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute("INSERT INTO orders(order_ref,customer_id,status,subtotal_cents,vat_cents,total_cents,client_key,created_by,created_at,updated_at) VALUES('LR-X',?,'received',100,5,105,'o','x',?,?)",(cid,server.now(),server.now()));oid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute("INSERT INTO payments(order_id,amount_cents,method,client_key,actor,created_at) VALUES(?,100,'cash','pay','x',?)",(oid,server.now()));
            with self.assertRaises(sqlite3.IntegrityError): c.execute("INSERT INTO payments(order_id,amount_cents,method,client_key,actor,created_at) VALUES(?,100,'cash','pay','x',?)",(oid,server.now()))
    def test_roles_seeded(self):
        with server.db() as c:self.assertEqual({x[0] for x in c.execute('SELECT role FROM users')},{'staff','admin'})
    def test_integrity(self):
        with server.db() as c:self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
    def test_operational_migration(self):
        with server.db() as c:
            tables={x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'refunds','expenses','business_settings','audit_events'}.issubset(tables))
            self.assertIn('discount_cents',{x['name'] for x in c.execute('PRAGMA table_info(orders)')})
            self.assertIn('last_login',{x['name'] for x in c.execute('PRAGMA table_info(users)')})
    def test_permission_rules(self):
        self.assertTrue(server.allowed({'role':'staff','permissions':'["orders.create"]'},'orders.create'))
        self.assertFalse(server.allowed({'role':'staff','permissions':'[]'},'orders.cancel'))
        self.assertTrue(server.allowed({'role':'admin','permissions':'[]'},'anything'))
    def test_refund_reduces_net_paid(self):
        with server.tx() as c:
            s,h=server.hashpw('x');c.execute("INSERT OR IGNORE INTO users(username,name,role,salt,password_hash,active,created_at) VALUES('x','x','staff',?,?,1,?)",(s,h,server.now()));c.execute("INSERT INTO customers(name,phone,created_at,updated_at) VALUES('A','+971501234567',?,?)",(server.now(),server.now()));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute("INSERT INTO orders(order_ref,customer_id,status,subtotal_cents,vat_cents,total_cents,client_key,created_by,created_at,updated_at) VALUES('LR-R',?,'received',1000,50,1050,'r','x',?,?)",(cid,server.now(),server.now()));oid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute("INSERT INTO payments(order_id,amount_cents,method,client_key,actor,created_at) VALUES(?,500,'cash','p','x',?)",(oid,server.now()));c.execute("INSERT INTO refunds(order_id,amount_cents,method,reason,client_key,actor,created_at) VALUES(?,200,'cash','test','r2','x',?)",(oid,server.now()));self.assertEqual(server.order_row(c,oid)['paid_cents'],300)

if __name__=='__main__': unittest.main(verbosity=2)
