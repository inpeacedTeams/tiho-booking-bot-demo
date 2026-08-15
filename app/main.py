import asyncio,re,secrets
from contextlib import asynccontextmanager,suppress
from datetime import date,datetime,timedelta
from pathlib import Path
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardButton,InlineKeyboardMarkup
from .config import settings
from . import db
from .bot import build_dispatcher
from .security import client_ip,hash_ip,normalize_phone,validate_name,validate_origin
ROOT=Path(__file__).parent.parent;bot=None;polling_task=None;reminder_task=None;bot_username=None
async def reminders():
    while True:
        await asyncio.sleep(60)
        if not bot:continue
        now=datetime.now();items=await db.rows("SELECT b.*,s.name service FROM bookings b JOIN services s ON s.id=b.service_id WHERE b.telegram_id>0 AND b.status='confirmed' AND b.reminder_sent=0 AND b.starts_at BETWEEN ? AND ?",((now+timedelta(hours=23,minutes=58)).isoformat(timespec='minutes'),(now+timedelta(hours=24,minutes=2)).isoformat(timespec='minutes')))
        for x in items:
            with suppress(Exception):await bot.send_message(x['telegram_id'],f"Напоминаю: завтра в {x['starts_at'][11:16]} у вас {x['service']}.");await db.execute("UPDATE bookings SET reminder_sent=1 WHERE id=?",(x['id'],))
@asynccontextmanager
async def lifespan(app):
    global bot,polling_task,reminder_task,bot_username
    await db.init_db()
    if settings.bot_token:
        bot=Bot(settings.bot_token,default=DefaultBotProperties(parse_mode=ParseMode.HTML));bot_username=(await bot.get_me()).username;polling_task=asyncio.create_task(build_dispatcher().start_polling(bot));reminder_task=asyncio.create_task(reminders())
    yield
    for task in(polling_task,reminder_task):
        if task:task.cancel()
    if bot:await bot.session.close()
app=FastAPI(title="Тихо Booking",lifespan=lifespan);app.mount("/static",StaticFiles(directory=ROOT/"static"),name="static")
class Toggle(BaseModel):enabled:bool
class Setting(BaseModel):value:str
class Attendance(BaseModel):status:str
class VerifyStart(BaseModel):phone:str
class WebBooking(BaseModel):service_id:int;staff_id:int;day:str;time:str;customer_name:str=Field(min_length=2,max_length=80);phone:str=Field(min_length=10,max_length=30);verification_token:str=Field(min_length=20,max_length=100)
@app.get("/")
async def index():return FileResponse(ROOT/"static"/"index.html")
@app.get("/book")
@app.get("/book/")
async def book():return FileResponse(ROOT/"static"/"book.html")
@app.get("/manifest.webmanifest")
async def manifest():return FileResponse(ROOT/"static"/"manifest.webmanifest",media_type="application/manifest+json")
@app.get("/sw.js")
async def sw():return FileResponse(ROOT/"static"/"sw.js",media_type="application/javascript",headers={"Service-Worker-Allowed":"/"})
@app.get("/icon.svg")
async def icon():return FileResponse(ROOT/"static"/"icon.svg",media_type="image/svg+xml")
@app.get("/api/public/bootstrap")
async def bootstrap():return {"services":await db.services(),"staff":await db.staff(),"today":date.today().isoformat(),"verification":{"telegram":bool(bot_username),"manual":bool(bot and settings.admin_id)}}
@app.post("/api/public/verification/telegram")
async def verify_telegram(payload:VerifyStart,request:Request):
    if not bot_username:raise HTTPException(503,"Telegram-проверка недоступна")
    try:validate_origin(request);phone=normalize_phone(payload.phone)
    except ValueError as e:raise HTTPException(422,str(e))
    token=secrets.token_urlsafe(24);await db.create_verification(token,phone,'telegram');return {"token":token,"url":f"https://t.me/{bot_username}?start=verify_{token}"}
@app.post("/api/public/verification/manual")
async def verify_manual(payload:VerifyStart,request:Request):
    if not bot or not settings.admin_id:raise HTTPException(503,"Подтверждение звонком не настроено")
    try:validate_origin(request);phone=normalize_phone(payload.phone)
    except ValueError as e:raise HTTPException(422,str(e))
    ip=hash_ip(client_ip(request));recent=await db.row("SELECT COUNT(*) n FROM booking_attempts WHERE ip_hash=? AND created_at>=datetime('now','-1 hour')",(ip,))
    if recent and recent['n']>=8:raise HTTPException(429,"Слишком много попыток")
    token=secrets.token_urlsafe(18);await db.create_verification(token,phone,'manual')
    keyboard=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подтвердить",callback_data=f"manual_ok:{token}"),InlineKeyboardButton(text="Отклонить",callback_data=f"manual_no:{token}")]])
    await bot.send_message(settings.admin_id,f"Запрос подтверждения номера для записи:\n<b>{phone}</b>\nПозвоните клиенту, затем выберите действие.",reply_markup=keyboard)
    return {"token":token,"status":"pending"}
@app.get("/api/public/verification/{token}")
async def verify_status(token:str):return await db.verification_status(token)
@app.get("/api/public/slots")
async def slots(staff_id:int,day:str):
    try:selected=date.fromisoformat(day)
    except ValueError:raise HTTPException(400,"Некорректная дата")
    if selected<date.today() or selected>date.today()+timedelta(days=30):raise HTTPException(400,"Дата вне диапазона")
    return {"slots":await db.free_slots(staff_id,day)}
@app.post("/api/public/bookings",status_code=201)
async def create_booking(payload:WebBooking,request:Request):
    try:validate_origin(request);phone=normalize_phone(payload.phone);name=validate_name(payload.customer_name)
    except ValueError as e:raise HTTPException(422,str(e))
    service=await db.row("SELECT * FROM services WHERE id=? AND enabled=1",(payload.service_id,));staff=await db.row("SELECT * FROM staff WHERE id=? AND enabled=1",(payload.staff_id,))
    if not service or not staff:raise HTTPException(404,"Услуга или мастер недоступны")
    starts=f"{payload.day}T{payload.time}"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",starts) or datetime.fromisoformat(starts)<=datetime.now():raise HTTPException(400,"Некорректное время")
    token=secrets.token_urlsafe(24)
    try:booking_id,risk,method=await db.secure_web_booking(ip_hash=hash_ip(client_ip(request)),phone=phone,name=name,service_id=payload.service_id,staff_id=payload.staff_id,starts_at=starts,token=token,verification_token=payload.verification_token)
    except PermissionError as e:raise HTTPException(429,str(e))
    except FileExistsError as e:raise HTTPException(409,str(e))
    return {"id":booking_id,"token":token,"service":service['name'],"staff":staff['name'],"starts_at":starts,"price":service['price'],"verification_method":method}
@app.get("/api/public/bookings/{token}")
async def get_booking(token:str):
    item=await db.row("SELECT b.id,b.customer_name,b.phone,b.starts_at,b.status,s.name service,s.price,st.name staff FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff st ON st.id=b.staff_id WHERE b.public_token=?",(token,))
    if not item:raise HTTPException(404,"Запись не найдена")
    return item
@app.delete("/api/public/bookings/{token}")
async def cancel_booking(token:str):
    item=await db.row("SELECT id,status FROM bookings WHERE public_token=?",(token,))
    if not item:raise HTTPException(404,"Запись не найдена")
    if item['status']=='confirmed':await db.mark_attendance(item['id'],'cancelled')
    return {"ok":True}
@app.get("/api/dashboard")
async def dashboard():
    today=date.today().isoformat();items=await db.rows("SELECT b.id,b.customer_name,b.phone,b.starts_at,b.status,b.risk_score,s.name service,s.price,st.name staff FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff st ON st.id=b.staff_id WHERE b.starts_at LIKE ? ORDER BY b.starts_at",(f"{today}%",));return {"date":today,"bookings":items,"count":sum(x['status']=='confirmed' for x in items),"revenue":sum(x['price'] for x in items if x['status']=='confirmed'),"automation":78}
@app.get("/api/bookings")
async def bookings():return await db.rows("SELECT b.id,b.customer_name,b.phone,b.starts_at,b.status,b.risk_score,s.name service,s.price,st.name staff FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff st ON st.id=b.staff_id ORDER BY b.starts_at DESC LIMIT 100")
@app.patch("/api/bookings/{booking_id}/attendance")
async def attendance(booking_id:int,payload:Attendance):await db.mark_attendance(booking_id,payload.status);return {"ok":True}
@app.get("/api/services")
async def services():return await db.services(False)
@app.patch("/api/services/{service_id}")
async def toggle(service_id:int,payload:Toggle):await db.execute("UPDATE services SET enabled=? WHERE id=?",(int(payload.enabled),service_id));return {"ok":True}
@app.get("/api/settings")
async def get_settings():return {x['key']:x['value'] for x in await db.rows("SELECT * FROM settings")}
@app.put("/api/settings/{key}")
async def save_setting(key:str,payload:Setting):await db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,payload.value));return {"ok":True}
@app.get("/health")
async def health():return {"ok":True,"bot":bool(bot),"pwa":True,"anti_fraud":True,"verification":{"telegram":bool(bot_username),"manual":bool(bot and settings.admin_id)}}
