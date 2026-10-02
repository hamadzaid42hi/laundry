import argparse, base64, datetime, os, sqlite3
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT=Path(__file__).resolve().parent; DB=ROOT/'data'/'laundry.sqlite3'; OUT=ROOT/'backups'/'encrypted'
def key():
    raw=os.environ.get('BACKUP_ENCRYPTION_KEY','')
    try: value=base64.urlsafe_b64decode(raw)
    except Exception: value=b''
    if len(value)!=32: raise SystemExit('BACKUP_ENCRYPTION_KEY must be a URL-safe base64 encoded 32-byte key')
    return value
def create():
    OUT.mkdir(parents=True,exist_ok=True);stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S');plain=OUT/f'.{stamp}.sqlite3';dest=sqlite3.connect(plain);src=sqlite3.connect(DB);src.backup(dest);assert dest.execute('pragma integrity_check').fetchone()[0]=='ok';src.close();dest.close();nonce=os.urandom(12);encrypted=AESGCM(key()).encrypt(nonce,plain.read_bytes(),b'al-rams-laundry');target=OUT/f'laundry-{stamp}.sqlite3.aes';target.write_bytes(b'ARL1'+nonce+encrypted);plain.unlink();
    for old in sorted(OUT.glob('*.aes'))[:-30]:old.unlink()
    print(target)
def restore(source,target):
    blob=Path(source).read_bytes();
    if blob[:4]!=b'ARL1':raise SystemExit('Invalid backup format')
    data=AESGCM(key()).decrypt(blob[4:16],blob[16:],b'al-rams-laundry');Path(target).write_bytes(data);c=sqlite3.connect(target);result=c.execute('pragma integrity_check').fetchone()[0];c.close();print('integrity:',result)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--restore');p.add_argument('--target');a=p.parse_args();restore(a.restore,a.target) if a.restore else create()
