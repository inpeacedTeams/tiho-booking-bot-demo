import asyncio
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from .config import settings
from . import db
from .bot import build_dispatcher

ROOT = Path(__file__).parent.parent
bot: Bot | None = None
polling_task: asyncio.Task | None = None
reminder_task: asyncio.Task | None = None

async def reminders():
    while True:
        await asyncio.sleep(60)
        if not bot:
            continue
        now=datetime.now()
        edge=now+timedelta(hours=24,minutes=2)
        items=await db.rows("SELECT b.*,s.name service,st.name staff FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff st ON st.id=b.staff_id WHERE b.source='telegram' AND b.status='confirmed' AND b.reminder_sent=0 AND b.starts_at BETWEEN ? AND ?",((now+timedelta(hours=23,minutes=58)).isoformat(timespec='minutes'),edge.isoformat(timespec='minutes')))
        for item in items:
            with suppress(Exception):
                await bot.send_message(item['telegram_id'],f"Напоминаю: завтра в {item['starts_at'][11:16]} у вас {item['service']}, мастер {item['staff']}. Всё в силе?")
                await db.execute("UPDATE bookings SET reminder_sent=1 WHERE id=?",(item['id'],))

@asynccontextmanager
async def lifespan(app:FastAPI):
    global bot,polling_task,reminder_task
    await db.init_db()
    if settings.bot_token:
        bot=Bot(settings.bot_token,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        polling_task=asyncio.create_task(build_dispatcher().start_polling(bot))
        reminder_task=asyncio.create_task(reminders())
    yield
    for task in (polling_task,reminder_task):
        if task:
            task.cancel()
    if bot:
        await bot.session.close()

app=FastAPI(title="Тихо Booking Demo",lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT/"static"), name="static")

class Toggle(BaseModel): enabled: bool
class Setting(BaseModel): value: str
class WebBooking(BaseModel):
    service_id: int
    staff_id: int
    day: str
    time: str
    customer_name: str = Field(min_length=2,max_length=80)
    phone: str = Field(min_length=10,max_length=30)

@app.get("/")
async def index(): return FileResponse(ROOT/"static"/"index.html")

@app.get("/book")
@app.get("/book/")
async def public_booking(): return FileResponse(ROOT/"static"/"book.html")

@app.get("/manifest.webmanifest")
async def manifest(): return FileResponse(ROOT/"static"/"manifest.webmanifest",media_type="application/manifest+json")

@app.get("/sw.js")
async def service_worker(): return FileResponse(ROOT/"static"/"sw.js",media_type="application/javascript",headers={"Service-Worker-Allowed":"/"})

@app.get("/icon.svg")
async def icon(): return FileResponse(ROOT/"static"/"icon.svg",media_type="image/svg+xml")

@app.get("/api/public/bootstrap")
async def public_bootstrap():
    return {"services":await db.services(),"staff":await db.staff(),"today":date.today().isoformat(),"max_day":(date.today()+timedelta(days=30)).isoformat(),"business":{"name":"Тихо","address":"ул. Ленина, 10","phone":"+7 999 000-00-00"}}

@app.get("/api/public/slots")
async def public_slots(staff_id:int,day:str):
    try:
        selected=date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400,"Некорректная дата")
    if selected < date.today() or selected > date.today()+timedelta(days=30):
        raise HTTPException(400,"Дата вне диапазона записи")
    if not await db.row("SELECT id FROM staff WHERE id=? AND enabled=1",(staff_id,)):
        raise HTTPException(404,"Мастер не найден")
    return {"slots":await db.free_slots(staff_id,day)}

@app.post("/api/public/bookings",status_code=201)
async def create_public_booking(payload:WebBooking):
    service=await db.row("SELECT * FROM services WHERE id=? AND enabled=1",(payload.service_id,))
    staff_member=await db.row("SELECT * FROM staff WHERE id=? AND enabled=1",(payload.staff_id,))
    if not service or not staff_member:
        raise HTTPException(404,"Услуга или мастер недоступны")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",payload.day) or not re.fullmatch(r"\d{2}:\d{2}",payload.time):
        raise HTTPException(400,"Некорректные дата или время")
    starts_at=f"{payload.day}T{payload.time}"
    if payload.time not in await db.free_slots(payload.staff_id,payload.day):
        raise HTTPException(409,"Это окно уже заняли")
    digits="".join(character for character in payload.phone if character.isdigit())
    if len(digits)<10:
        raise HTTPException(422,"Проверьте номер телефона")
    token=secrets.token_urlsafe(24)
    try:
        booking_id=await db.execute("INSERT INTO bookings(telegram_id,customer_name,phone,service_id,staff_id,starts_at,source,public_token) VALUES(0,?,?,?,?,?,'web',?)",(payload.customer_name.strip(),payload.phone.strip(),payload.service_id,payload.staff_id,starts_at,token))
    except sqlite3.IntegrityError:
        raise HTTPException(409,"Это окно уже заняли")
    return {"id":booking_id,"token":token,"service":service['name'],"staff":staff_member['name'],"starts_at":starts_at,"price":service['price']}

@app.get("/api/public/bookings/{token}")
async def get_public_booking(token:str):
    item=await db.row("SELECT b.id,b.customer_name,b.phone,b.starts_at,b.status,s.name service,s.price,st.name staff FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff st ON st.id=b.staff_id WHERE b.public_token=?",(token,))
    if not item:
        raise HTTPException(404,"Запись не найдена")
    return item

@app.delete("/api/public/bookings/{token}")
async def cancel_public_booking(token:str):
    item=await db.row("SELECT id,status FROM bookings WHERE public_token=?",(token,))
    if not item:
        raise HTTPException(404,"Запись не найдена")
    if item['status']=='confirmed':
        await db.execute("UPDATE bookings SET status='cancelled' WHERE id=?",(item['id'],))
    return {"ok":True}

@app.get("/api/dashboard")
async def dashboard():
    today=datetime.now().date().isoformat()
    bookings=await db.rows("SELECT b.id,b.customer_name,b.phone,b.starts_at,b.status,s.name service,s.price,st.name staff FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff st ON st.id=b.staff_id WHERE b.starts_at LIKE ? ORDER BY b.starts_at",(f"{today}%",))
    return {"date":today,"bookings":bookings,"count":sum(item['status']=='confirmed' for item in bookings),"revenue":sum(item['price'] for item in bookings if item['status']=='confirmed'),"automation":78}

@app.get("/api/bookings")
async def bookings(): return await db.rows("SELECT b.id,b.customer_name,b.phone,b.starts_at,b.status,s.name service,s.price,st.name staff FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff st ON st.id=b.staff_id ORDER BY b.starts_at DESC LIMIT 100")

@app.get("/api/services")
async def get_services(): return await db.services(False)

@app.patch("/api/services/{service_id}")
async def toggle_service(service_id:int,payload:Toggle):
    if not await db.row("SELECT id FROM services WHERE id=?",(service_id,)):
        raise HTTPException(404,"Service not found")
    await db.execute("UPDATE services SET enabled=? WHERE id=?",(int(payload.enabled),service_id))
    return {"ok":True}

@app.get("/api/settings")
async def get_settings(): return {item['key']:item['value'] for item in await db.rows("SELECT * FROM settings")}

@app.put("/api/settings/{key}")
async def save_setting(key:str,payload:Setting):
    if key not in {'hold_minutes','minimum_notice_hours','reminder_hours'}:
        raise HTTPException(400,"Unknown setting")
    await db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,payload.value))
    return {"ok":True}

@app.get("/health")
async def health(): return {"ok":True,"bot":bool(settings.bot_token),"demo_mode":settings.demo_mode,"pwa":True}
