from datetime import datetime, timedelta
from pathlib import Path
import aiosqlite
from .config import settings

SCHEMA="""
CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,duration INTEGER NOT NULL,price INTEGER NOT NULL,enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS staff(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY AUTOINCREMENT,telegram_id INTEGER NOT NULL,customer_name TEXT NOT NULL,phone TEXT NOT NULL,service_id INTEGER NOT NULL,staff_id INTEGER NOT NULL,starts_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'confirmed',reminder_sent INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(staff_id,starts_at));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS booking_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,ip_hash TEXT NOT NULL,phone TEXT NOT NULL DEFAULT '',outcome TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS phone_reputation(phone TEXT PRIMARY KEY,no_shows INTEGER NOT NULL DEFAULT 0,cancellations INTEGER NOT NULL DEFAULT 0,completed INTEGER NOT NULL DEFAULT 0,blocked INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS verification_sessions(token TEXT PRIMARY KEY,phone TEXT NOT NULL,telegram_user_id INTEGER,status TEXT NOT NULL DEFAULT 'pending',expires_at TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,used_at TEXT);
"""

async def connect():
    path=Path(settings.database_path).expanduser(); path.parent.mkdir(parents=True,exist_ok=True)
    database=await aiosqlite.connect(str(path)); database.row_factory=aiosqlite.Row
    await database.execute("PRAGMA foreign_keys=ON"); await database.execute("PRAGMA busy_timeout=5000")
    return database

async def _columns(database,table):
    cursor=await database.execute(f"PRAGMA table_info({table})"); return {row[1] for row in await cursor.fetchall()}

async def init_db():
    database=await connect(); await database.executescript(SCHEMA)
    columns=await _columns(database,"bookings")
    migrations={"source":"TEXT NOT NULL DEFAULT 'telegram'","public_token":"TEXT","risk_score":"INTEGER NOT NULL DEFAULT 0","client_ip_hash":"TEXT","phone_verified":"INTEGER NOT NULL DEFAULT 0"}
    for name,definition in migrations.items():
        if name not in columns: await database.execute(f"ALTER TABLE bookings ADD COLUMN {name} {definition}")
    count=(await (await database.execute("SELECT COUNT(*) n FROM services")).fetchone())["n"]
    if not count:
        await database.executemany("INSERT INTO services(name,duration,price) VALUES(?,?,?)",[("Женская стрижка",60,2000),("Окрашивание",120,5500),("Укладка",45,1800),("Консультация колориста",30,0)])
        await database.executemany("INSERT INTO staff(name) VALUES(?)",[("Анна",),("Елена",)])
        await database.executemany("INSERT INTO settings(key,value) VALUES(?,?)",[("hold_minutes","5"),("minimum_notice_hours","2"),("reminder_hours","24")])
    await database.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_public_token ON bookings(public_token) WHERE public_token IS NOT NULL")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_attempts_ip_time ON booking_attempts(ip_hash,created_at)")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_bookings_phone_time ON bookings(phone,starts_at)")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_verify_user ON verification_sessions(telegram_user_id,status)")
    await database.commit(); await database.close()

async def rows(sql,params=()):
    database=await connect(); cursor=await database.execute(sql,params); result=[dict(item) for item in await cursor.fetchall()]; await database.close(); return result
async def row(sql,params=()):
    result=await rows(sql,params); return result[0] if result else None
async def execute(sql,params=()):
    database=await connect()
    try: cursor=await database.execute(sql,params); await database.commit(); return cursor.lastrowid
    finally: await database.close()
async def services(enabled_only=True): return await rows("SELECT * FROM services"+(" WHERE enabled=1" if enabled_only else "")+" ORDER BY id")
async def staff(): return await rows("SELECT * FROM staff WHERE enabled=1 ORDER BY id")

async def free_slots(staff_id:int,day:str):
    taken={item['starts_at'][11:16] for item in await rows("SELECT starts_at FROM bookings WHERE staff_id=? AND starts_at LIKE ? AND status IN ('confirmed','pending')",(staff_id,f"{day}%"))}
    return [time for time in ["10:00","11:30","13:00","15:00","17:30","19:00"] if time not in taken and datetime.fromisoformat(f"{day}T{time}")>datetime.now()]

async def create_verification(token:str,phone:str):
    expires=(datetime.utcnow()+timedelta(minutes=10)).isoformat(timespec="seconds")
    database=await connect()
    await database.execute("DELETE FROM verification_sessions WHERE expires_at<? OR used_at IS NOT NULL",(datetime.utcnow().isoformat(timespec="seconds"),))
    await database.execute("INSERT INTO verification_sessions(token,phone,expires_at) VALUES(?,?,?)",(token,phone,expires)); await database.commit(); await database.close()
    return expires

async def attach_verification(token:str,user_id:int):
    database=await connect(); now=datetime.utcnow().isoformat(timespec="seconds")
    cursor=await database.execute("UPDATE verification_sessions SET telegram_user_id=? WHERE token=? AND status='pending' AND expires_at>?",(user_id,token,now)); await database.commit(); changed=cursor.rowcount; await database.close(); return changed>0

async def pending_verification(user_id:int):
    now=datetime.utcnow().isoformat(timespec="seconds")
    return await row("SELECT * FROM verification_sessions WHERE telegram_user_id=? AND status='pending' AND expires_at>? ORDER BY created_at DESC LIMIT 1",(user_id,now))

async def verify_contact(user_id:int,phone:str):
    database=await connect(); now=datetime.utcnow().isoformat(timespec="seconds")
    session=await (await database.execute("SELECT * FROM verification_sessions WHERE telegram_user_id=? AND status='pending' AND expires_at>? ORDER BY created_at DESC LIMIT 1",(user_id,now))).fetchone()
    if not session: await database.close(); return "missing"
    if session['phone']!=phone: await database.close(); return "mismatch"
    await database.execute("UPDATE verification_sessions SET status='verified' WHERE token=?",(session['token'],)); await database.commit(); await database.close(); return "verified"

async def verification_status(token:str):
    now=datetime.utcnow().isoformat(timespec="seconds")
    item=await row("SELECT phone,status,expires_at,used_at FROM verification_sessions WHERE token=?",(token,))
    if not item: return {"status":"missing"}
    if item['expires_at']<=now and item['status']!='verified': return {"status":"expired"}
    return {"status":item['status'] if not item['used_at'] else "used"}

async def secure_web_booking(*,ip_hash,phone,name,service_id,staff_id,starts_at,token,verification_token):
    database=await connect()
    try:
        await database.execute("BEGIN IMMEDIATE"); now=datetime.utcnow().isoformat(timespec="seconds")
        verification=await (await database.execute("SELECT * FROM verification_sessions WHERE token=? AND phone=? AND status='verified' AND used_at IS NULL AND expires_at>?",(verification_token,phone,now))).fetchone()
        if not verification: raise PermissionError("Сначала подтвердите номер через Telegram")
        await database.execute("DELETE FROM booking_attempts WHERE created_at < datetime('now','-31 days')")
        hour=(await (await database.execute("SELECT COUNT(*) n FROM booking_attempts WHERE ip_hash=? AND created_at>=datetime('now','-1 hour')",(ip_hash,))).fetchone())["n"]
        day=(await (await database.execute("SELECT COUNT(*) n FROM booking_attempts WHERE ip_hash=? AND created_at>=datetime('now','-1 day')",(ip_hash,))).fetchone())["n"]
        reputation=await (await database.execute("SELECT * FROM phone_reputation WHERE phone=?",(phone,))).fetchone()
        active=(await (await database.execute("SELECT COUNT(*) n FROM bookings WHERE phone=? AND status IN ('confirmed','pending') AND starts_at>datetime('now')",(phone,))).fetchone())["n"]
        if hour>=8 or day>=20: raise PermissionError("Слишком много попыток. Попробуйте завтра")
        if reputation and (reputation['blocked'] or reputation['no_shows']>=2): raise PermissionError("Онлайн-запись ограничена. Свяжитесь с администратором")
        if active>=1: raise FileExistsError("У этого номера уже есть активная запись")
        occupied=await (await database.execute("SELECT id FROM bookings WHERE staff_id=? AND starts_at=? AND status IN ('confirmed','pending')",(staff_id,starts_at))).fetchone()
        if occupied: raise FileExistsError("Это окно уже заняли")
        risk=(reputation['cancellations'] if reputation else 0)*10+(reputation['no_shows'] if reputation else 0)*40
        cursor=await database.execute("INSERT INTO bookings(telegram_id,customer_name,phone,service_id,staff_id,starts_at,source,public_token,risk_score,client_ip_hash,phone_verified) VALUES(?,?,?,?,?,?,'web',?,?,?,1)",(verification['telegram_user_id'] or 0,name,phone,service_id,staff_id,starts_at,token,risk,ip_hash))
        await database.execute("UPDATE verification_sessions SET used_at=? WHERE token=?",(now,verification_token))
        await database.execute("INSERT INTO phone_reputation(phone) VALUES(?) ON CONFLICT(phone) DO UPDATE SET updated_at=CURRENT_TIMESTAMP",(phone,))
        await database.execute("INSERT INTO booking_attempts(ip_hash,phone,outcome) VALUES(?,?,'created')",(ip_hash,phone)); await database.commit(); return cursor.lastrowid,risk
    except Exception:
        if database.in_transaction: await database.rollback()
        raise
    finally: await database.close()

async def mark_attendance(booking_id:int,status:str):
    database=await connect()
    try:
        await database.execute("BEGIN IMMEDIATE"); item=await (await database.execute("SELECT phone,status FROM bookings WHERE id=?",(booking_id,))).fetchone()
        if not item: raise LookupError("Запись не найдена")
        await database.execute("UPDATE bookings SET status=? WHERE id=?",(status,booking_id))
        if status=='completed': await database.execute("INSERT INTO phone_reputation(phone,completed) VALUES(?,1) ON CONFLICT(phone) DO UPDATE SET completed=completed+1,updated_at=CURRENT_TIMESTAMP",(item['phone'],))
        elif status=='no_show': await database.execute("INSERT INTO phone_reputation(phone,no_shows) VALUES(?,1) ON CONFLICT(phone) DO UPDATE SET no_shows=no_shows+1,updated_at=CURRENT_TIMESTAMP",(item['phone'],))
        elif status=='cancelled' and item['status']!='cancelled': await database.execute("INSERT INTO phone_reputation(phone,cancellations) VALUES(?,1) ON CONFLICT(phone) DO UPDATE SET cancellations=cancellations+1,updated_at=CURRENT_TIMESTAMP",(item['phone'],))
        await database.commit()
    except Exception:
        if database.in_transaction: await database.rollback()
        raise
    finally: await database.close()
