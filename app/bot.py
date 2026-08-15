from datetime import date, timedelta, datetime
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo
from aiogram.exceptions import TelegramBadRequest
from .config import settings
from . import db
from .security import normalize_phone

router=Router()
class Booking(StatesGroup): service=State(); staff=State(); day=State(); slot=State(); name=State(); phone=State(); confirm=State()
def ik(rows): return InlineKeyboardMarkup(inline_keyboard=rows)
def menu():
    rows=[[KeyboardButton(text="Записаться"),KeyboardButton(text="Мои записи")],[KeyboardButton(text="Услуги и цены"),KeyboardButton(text="Позвать администратора")]]
    if settings.webapp_url.startswith("https://"): rows.append([KeyboardButton(text="Кабинет владельца",web_app=WebAppInfo(url=settings.webapp_url))])
    return ReplyKeyboardMarkup(keyboard=rows,resize_keyboard=True)
async def edit(call,text,markup=None):
    try: await call.message.edit_text(text,reply_markup=markup)
    except TelegramBadRequest: await call.message.answer(text,reply_markup=markup)
    await call.answer()

@router.message(CommandStart())
async def start(message:Message,state:FSMContext):
    await state.clear(); parts=(message.text or '').split(maxsplit=1)
    if len(parts)==2 and parts[1].startswith('verify_'):
        token=parts[1][7:]
        if await db.attach_verification(token,message.from_user.id):
            keyboard=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Подтвердить мой номер",request_contact=True)]],resize_keyboard=True,one_time_keyboard=True)
            return await message.answer("Чтобы подтвердить номер для онлайн-записи, нажмите кнопку ниже. Telegram передаст только ваш номер.",reply_markup=keyboard)
        return await message.answer("Ссылка подтверждения устарела. Вернитесь на страницу записи и запросите новую.",reply_markup=menu())
    await message.answer("Привет! Я помощник салона «Тихо». Запишу на удобное время без звонков.",reply_markup=menu())

@router.message(F.contact)
async def verification_contact(message:Message):
    pending=await db.pending_verification(message.from_user.id)
    if not pending: return
    if message.contact.user_id and message.contact.user_id!=message.from_user.id:
        return await message.answer("Нужно отправить свой контакт кнопкой, чужой номер не подойдёт.")
    try: phone=normalize_phone(message.contact.phone_number)
    except ValueError: return await message.answer("Telegram передал номер неподдерживаемого формата.")
    result=await db.verify_contact(message.from_user.id,phone)
    if result=='verified': await message.answer("Номер подтверждён. Возвращайтесь на страницу записи, она продолжит автоматически.",reply_markup=ReplyKeyboardRemove())
    elif result=='mismatch': await message.answer("Номер Telegram не совпадает с номером на странице. Вернитесь и укажите тот же номер.",reply_markup=ReplyKeyboardRemove())

@router.message(F.text=="Записаться")
async def begin(m:Message,state:FSMContext):
    await state.clear(); items=await db.services(); await state.set_state(Booking.service)
    await m.answer("Что хотите сделать?",reply_markup=ik([[InlineKeyboardButton(text=f"{x['name']} · {x['price'] or 'бесплатно'}{' ₽' if x['price'] else ''}",callback_data=f"svc:{x['id']}")] for x in items]))
@router.callback_query(Booking.service,F.data.startswith("svc:"))
async def choose_service(c,state):
    service=await db.row("SELECT * FROM services WHERE id=? AND enabled=1",(int(c.data.split(':')[1]),))
    if not service:return await edit(c,"Услуга недоступна.")
    await state.update_data(service=service); await state.set_state(Booking.staff); people=await db.staff(); await edit(c,f"Вы выбрали: <b>{service['name']}</b>\nК какому мастеру?",ik([[InlineKeyboardButton(text=x['name'],callback_data=f"staff:{x['id']}")] for x in people]))
@router.callback_query(Booking.staff,F.data.startswith("staff:"))
async def choose_staff(c,state):
    person=await db.row("SELECT * FROM staff WHERE id=? AND enabled=1",(int(c.data.split(':')[1]),)); await state.update_data(staff=person); await state.set_state(Booking.day); days=[date.today()+timedelta(days=i) for i in range(1,8)]; names=['Пн','Вт','Ср','Чт','Пт','Сб','Вс']; await edit(c,"Выберите день:",ik([[InlineKeyboardButton(text=f"{names[d.weekday()]}, {d.strftime('%d.%m')}",callback_data=f"day:{d.isoformat()}")] for d in days]))
@router.callback_query(Booking.day,F.data.startswith("day:"))
async def choose_day(c,state):
    day=c.data.split(':',1)[1]; data=await state.get_data(); slots=await db.free_slots(data['staff']['id'],day)
    if not slots:return await edit(c,"На этот день свободных окон нет.")
    await state.update_data(day=day); await state.set_state(Booking.slot); await edit(c,"Свободные окна:",ik([[InlineKeyboardButton(text=t,callback_data=f"slot:{t}") for t in slots[i:i+3]] for i in range(0,len(slots),3)]))
@router.callback_query(Booking.slot,F.data.startswith("slot:"))
async def choose_slot(c,state): await state.update_data(slot=c.data.split(':')[1]); await state.set_state(Booking.name); await edit(c,"Как вас зовут?")
@router.message(Booking.name)
async def get_name(m,state):
    if len((m.text or '').strip())<2:return await m.answer("Напишите имя хотя бы из двух букв.")
    await state.update_data(name=m.text.strip()); await state.set_state(Booking.phone); await m.answer("Оставьте номер:",reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Поделиться номером",request_contact=True)]],resize_keyboard=True,one_time_keyboard=True))
@router.message(Booking.phone)
async def get_phone(m,state):
    phone=m.contact.phone_number if m.contact else (m.text or '').strip(); await state.update_data(phone=phone); data=await state.get_data(); await state.set_state(Booking.confirm); s,p=data['service'],data['staff']; await m.answer(f"Проверьте запись:\n\n<b>{s['name']}</b>\n{data['day']} в {data['slot']}\nМастер {p['name']}\nИмя: {data['name']}",reply_markup=ik([[InlineKeyboardButton(text="Подтвердить",callback_data="confirm:yes")]]))
@router.callback_query(Booking.confirm,F.data=="confirm:yes")
async def confirm(c,state):
    data=await state.get_data(); starts=f"{data['day']}T{data['slot']}"
    try: bid=await db.execute("INSERT INTO bookings(telegram_id,customer_name,phone,service_id,staff_id,starts_at,phone_verified) VALUES(?,?,?,?,?,?,1)",(c.from_user.id,data['name'],data['phone'],data['service']['id'],data['staff']['id'],starts))
    except Exception: await state.clear(); return await edit(c,"Окно уже заняли.")
    await state.clear(); await edit(c,f"Готово! Запись №{bid} подтверждена.")
@router.message(F.text=="Мои записи")
async def mine(m):
    items=await db.rows("SELECT b.*,s.name service,st.name staff FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff st ON st.id=b.staff_id WHERE telegram_id=? AND status='confirmed' AND starts_at>=? ORDER BY starts_at",(m.from_user.id,datetime.now().isoformat(timespec='minutes')))
    if not items:return await m.answer("Активных записей нет.")
    for x in items:await m.answer(f"<b>{x['service']}</b>\n{x['starts_at'].replace('T',' в ')} · {x['staff']}")
@router.message(F.text=="Услуги и цены")
async def prices(m):
    items=await db.services(); await m.answer("\n".join(f"<b>{x['name']}</b> · {x['duration']} мин · {x['price'] or 'бесплатно'}{' ₽' if x['price'] else ''}" for x in items))
@router.message(F.text=="Позвать администратора")
async def human(m,bot:Bot):
    await m.answer("Передал запрос администратору.")
    if settings.admin_id:await bot.send_message(settings.admin_id,f"Нужен администратор: @{m.from_user.username or 'без username'}")
@router.message()
async def fallback(m): await m.answer("Выберите действие кнопкой ниже.",reply_markup=menu())
def build_dispatcher(): dp=Dispatcher(); dp.include_router(router); return dp
