from datetime import datetime
import aiosqlite
from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, duration INTEGER NOT NULL, price INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS staff(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL, customer_name TEXT NOT NULL, phone TEXT NOT NULL, service_id INTEGER NOT NULL, staff_id INTEGER NOT NULL, starts_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed', reminder_sent INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(staff_id, starts_at));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

async def connect():
    database = await aiosqlite.connect(settings.database_path)
    database.row_factory = aiosqlite.Row
    await database.execute("PRAGMA foreign_keys=ON")
    return database

async def _columns(database, table):
    cursor = await database.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}

async def init_db():
    database = await connect()
    await database.executescript(SCHEMA)
    columns = await _columns(database, "bookings")
    if "source" not in columns:
        await database.execute("ALTER TABLE bookings ADD COLUMN source TEXT NOT NULL DEFAULT 'telegram'")
    if "public_token" not in columns:
        await database.execute("ALTER TABLE bookings ADD COLUMN public_token TEXT")
    count = (await (await database.execute("SELECT COUNT(*) n FROM services")).fetchone())["n"]
    if not count:
        await database.executemany("INSERT INTO services(name,duration,price) VALUES(?,?,?)", [("Женская стрижка",60,2000),("Окрашивание",120,5500),("Укладка",45,1800),("Консультация колориста",30,0)])
        await database.executemany("INSERT INTO staff(name) VALUES(?)", [("Анна",),("Елена",)])
        await database.executemany("INSERT INTO settings(key,value) VALUES(?,?)", [("hold_minutes","5"),("minimum_notice_hours","2"),("reminder_hours","24")])
    await database.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_public_token ON bookings(public_token) WHERE public_token IS NOT NULL")
    await database.commit()
    await database.close()

async def rows(sql, params=()):
    database=await connect()
    cursor=await database.execute(sql,params)
    result=[dict(item) for item in await cursor.fetchall()]
    await database.close()
    return result

async def row(sql, params=()):
    result=await rows(sql,params)
    return result[0] if result else None

async def execute(sql, params=()):
    database=await connect()
    try:
        cursor=await database.execute(sql,params)
        await database.commit()
        return cursor.lastrowid
    finally:
        await database.close()

async def services(enabled_only=True):
    return await rows("SELECT * FROM services" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id")

async def staff():
    return await rows("SELECT * FROM staff WHERE enabled=1 ORDER BY id")

async def free_slots(staff_id:int, day:str):
    taken={item['starts_at'][11:16] for item in await rows("SELECT starts_at FROM bookings WHERE staff_id=? AND starts_at LIKE ? AND status='confirmed'",(staff_id,f"{day}%"))}
    return [time for time in ["10:00","11:30","13:00","15:00","17:30","19:00"] if time not in taken and datetime.fromisoformat(f"{day}T{time}") > datetime.now()]
