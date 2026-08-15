import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from .config import settings
from . import db
from .bot import build_dispatcher

bot: Bot | None=None
polling_task: asyncio.Task | None=None
reminder_task: asyncio.Task | None=None

async def reminders():
    while True:
        await asyncio.sleep(60)
        if not bot: continue
        now=datetime.now(); edge=now+timedelta(hours=24,minutes=2)
        items=await db.rows("SELECT b.*,s.name service,st.name staff FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff st ON st.id=b.staff_id WHERE b.status='confirmed' AND b.reminder_sent=0 AND b.starts_at BETWEEN ? AND ?",((now+timedelta(hours=23,minutes=58)).isoformat(timespec='minutes'),edge.isoformat(timespec='minutes')))
        for x in items:
            with suppress(Exception):
                await bot.send_message(x['telegram_id'],f"Напоминаю: завтра в {x['starts_at'][11:16]} у вас {x['service']}, мастер {x['staff']}. Всё в силе?")
                await db.execute("UPDATE bookings SET reminder_sent=1 WHERE id=?",(x['id'],))

@asynccontextmanager
async def lifespan(app:FastAPI):
    global bot,polling_task,reminder_task
    await db.init_db()
    if settings.bot_token:
        bot=Bot(settings.bot_token,default=DefaultBotProperties(parse_mode=ParseMode.HTML)); polling_task=asyncio.create_task(build_dispatcher().start_polling(bot)); reminder_task=asyncio.create_task(reminders())
    yield
    for task in (polling_task,reminder_task):
        if task: task.cancel()
    if bot: await bot.session.close()

app=FastAPI(title="Тихо Booking Demo",lifespan=lifespan)

class Toggle(BaseModel): enabled: bool
class Setting(BaseModel): value: str

@app.get("/")
async def index(): return FileResponse(Path(__file__).parent.parent/"static"/"index.html")

@app.get("/api/dashboard")
async def dashboard():
    today=datetime.now().date().isoformat()
    bookings=await db.rows("SELECT b.id,b.customer_name,b.phone,b.starts_at,b.status,s.name service,s.price,st.name staff FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff st ON st.id=b.staff_id WHERE b.starts_at LIKE ? ORDER BY b.starts_at",(f"{today}%",))
    return {"date":today,"bookings":bookings,"count":sum(x['status']=='confirmed' for x in bookings),"revenue":sum(x['price'] for x in bookings if x['status']=='confirmed'),"automation":78}

@app.get("/api/bookings")
async def bookings(): return await db.rows("SELECT b.id,b.customer_name,b.phone,b.starts_at,b.status,s.name service,s.price,st.name staff FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff st ON st.id=b.staff_id ORDER BY b.starts_at DESC LIMIT 100")

@app.get("/api/services")
async def get_services(): return await db.services(False)

@app.patch("/api/services/{service_id}")
async def toggle_service(service_id:int,payload:Toggle):
    if not await db.row("SELECT id FROM services WHERE id=?",(service_id,)): raise HTTPException(404,"Service not found")
    await db.execute("UPDATE services SET enabled=? WHERE id=?",(int(payload.enabled),service_id)); return {"ok":True}

@app.get("/api/settings")
async def get_settings(): return {x['key']:x['value'] for x in await db.rows("SELECT * FROM settings")}

@app.put("/api/settings/{key}")
async def save_setting(key:str,payload:Setting):
    if key not in {'hold_minutes','minimum_notice_hours','reminder_hours'}: raise HTTPException(400,"Unknown setting")
    await db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,payload.value)); return {"ok":True}

@app.get("/health")
async def health(): return {"ok":True,"bot":bool(settings.bot_token),"demo_mode":settings.demo_mode}
