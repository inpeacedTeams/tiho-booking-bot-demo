from datetime import date, timedelta, datetime
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
from aiogram.exceptions import TelegramBadRequest
from .config import settings
from . import db

router=Router()

class Booking(StatesGroup):
    service=State(); staff=State(); day=State(); slot=State(); name=State(); phone=State(); confirm=State()

def ik(rows): return InlineKeyboardMarkup(inline_keyboard=rows)
def menu():
    rows=[[KeyboardButton(text="Записаться"),KeyboardButton(text="Мои записи")],[KeyboardButton(text="Услуги и цены"),KeyboardButton(text="Позвать администратора")]]
    if settings.webapp_url.startswith("https://"): rows.append([KeyboardButton(text="Кабинет владельца",web_app=WebAppInfo(url=settings.webapp_url))])
    return ReplyKeyboardMarkup(keyboard=rows,resize_keyboard=True)

async def edit(call:CallbackQuery,text:str,markup=None):
    try: await call.message.edit_text(text,reply_markup=markup)
    except TelegramBadRequest: await call.message.answer(text,reply_markup=markup)
    await call.answer()

@router.message(CommandStart())
async def start(m:Message,state:FSMContext):
    await state.clear(); await m.answer("Привет! Я помощник салона «Тихо». Запишу на удобное время без звонков.",reply_markup=menu())

@router.message(F.text=="Записаться")
async def begin(m:Message,state:FSMContext):
    await state.clear(); items=await db.services()
    await state.set_state(Booking.service)
    await m.answer("Что хотите сделать?",reply_markup=ik([[InlineKeyboardButton(text=f"{x['name']} · {x['price'] or 'бесплатно'}{' ₽' if x['price'] else ''}",callback_data=f"svc:{x['id']}")] for x in items]))

@router.callback_query(Booking.service,F.data.startswith("svc:"))
async def choose_service(c:CallbackQuery,state:FSMContext):
    sid=int(c.data.split(':')[1]); service=await db.row("SELECT * FROM services WHERE id=? AND enabled=1",(sid,))
    if not service: return await edit(c,"Услуга временно недоступна. Начните запись заново.")
    await state.update_data(service=service); await state.set_state(Booking.staff)
    people=await db.staff(); await edit(c,f"Вы выбрали: <b>{service['name']}</b>\nК какому мастеру?",ik([[InlineKeyboardButton(text=x['name'],callback_data=f"staff:{x['id']}")] for x in people]+[[InlineKeyboardButton(text="Любой мастер",callback_data="staff:any")]]))

@router.callback_query(Booking.staff,F.data.startswith("staff:"))
async def choose_staff(c:CallbackQuery,state:FSMContext):
    raw=c.data.split(':')[1]; people=await db.staff(); person=people[0] if raw=='any' else next((x for x in people if x['id']==int(raw)),None)
    if not person: return await edit(c,"Мастер недоступен. Начните запись заново.")
    await state.update_data(staff=person); await state.set_state(Booking.day)
    days=[date.today()+timedelta(days=i) for i in range(1,8)]
    names=['Пн','Вт','Ср','Чт','Пт','Сб','Вс']
    buttons=[[InlineKeyboardButton(text=f"{names[d.weekday()]}, {d.strftime('%d.%m')}",callback_data=f"day:{d.isoformat()}")] for d in days]
    await edit(c,f"Мастер: <b>{person['name']}</b>\nВыберите день:",ik(buttons))

@router.callback_query(Booking.day,F.data.startswith("day:"))
async def choose_day(c:CallbackQuery,state:FSMContext):
    day=c.data.split(':',1)[1]; data=await state.get_data(); slots=await db.free_slots(data['staff']['id'],day)
    if not slots: return await edit(c,"На этот день свободных окон нет.",ik([[InlineKeyboardButton(text="Выбрать другой день",callback_data="back:day")]]))
    await state.update_data(day=day); await state.set_state(Booking.slot)
    await edit(c,"Свободные окна:",ik([[InlineKeyboardButton(text=t,callback_data=f"slot:{t}") for t in slots[i:i+3]] for i in range(0,len(slots),3)]))

@router.callback_query(F.data=="back:day")
async def back_day(c:CallbackQuery,state:FSMContext):
    await state.set_state(Booking.staff); c.data="staff:any"; await choose_staff(c,state)

@router.callback_query(Booking.slot,F.data.startswith("slot:"))
async def choose_slot(c:CallbackQuery,state:FSMContext):
    await state.update_data(slot=c.data.split(':')[1]); await state.set_state(Booking.name); await edit(c,"Как вас зовут?")

@router.message(Booking.name)
async def get_name(m:Message,state:FSMContext):
    if len((m.text or '').strip())<2: return await m.answer("Напишите имя хотя бы из двух букв.")
    await state.update_data(name=m.text.strip()); await state.set_state(Booking.phone)
    await m.answer("Оставьте номер телефона:",reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Поделиться номером",request_contact=True)]],resize_keyboard=True,one_time_keyboard=True))

@router.message(Booking.phone)
async def get_phone(m:Message,state:FSMContext):
    phone=m.contact.phone_number if m.contact else (m.text or '').strip()
    digits=''.join(x for x in phone if x.isdigit())
    if len(digits)<10: return await m.answer("Не похоже на номер. Пришлите его в формате +7 999 123-45-67.")
    await state.update_data(phone=phone); data=await state.get_data(); await state.set_state(Booking.confirm)
    s,p=data['service'],data['staff']; summary=f"Проверьте запись:\n\n<b>{s['name']}</b>\n{data['day']} в {data['slot']}\nМастер {p['name']}\nСтоимость: {s['price'] or 'бесплатно'}{' ₽' if s['price'] else ''}\nИмя: {data['name']}"
    await m.answer(summary,reply_markup=ik([[InlineKeyboardButton(text="Подтвердить",callback_data="confirm:yes")],[InlineKeyboardButton(text="Начать заново",callback_data="confirm:no")]]))

@router.callback_query(Booking.confirm,F.data=="confirm:yes")
async def confirm(c:CallbackQuery,state:FSMContext):
    data=await state.get_data(); starts=f"{data['day']}T{data['slot']}"
    try: bid=await db.execute("INSERT INTO bookings(telegram_id,customer_name,phone,service_id,staff_id,starts_at) VALUES(?,?,?,?,?,?)",(c.from_user.id,data['name'],data['phone'],data['service']['id'],data['staff']['id'],starts))
    except Exception: await state.clear(); return await edit(c,"Это окно только что заняли. Нажмите «Записаться» и выберите другое.")
    await state.clear(); await edit(c,f"Готово! Запись №{bid} подтверждена. За сутки я напомню.",ik([[InlineKeyboardButton(text="Отменить запись",callback_data=f"cancel:{bid}")]]))

@router.callback_query(F.data=="confirm:no")
async def restart(c:CallbackQuery,state:FSMContext): await state.clear(); await edit(c,"Хорошо. Нажмите «Записаться», когда будете готовы.")

@router.message(F.text=="Мои записи")
async def mine(m:Message):
    items=await db.rows("SELECT b.*,s.name service,st.name staff FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff st ON st.id=b.staff_id WHERE telegram_id=? AND status='confirmed' AND starts_at>=? ORDER BY starts_at",(m.from_user.id,datetime.now().isoformat(timespec='minutes')))
    if not items: return await m.answer("Активных записей нет.")
    for x in items: await m.answer(f"<b>{x['service']}</b>\n{x['starts_at'].replace('T',' в ')} · {x['staff']}",reply_markup=ik([[InlineKeyboardButton(text="Отменить",callback_data=f"cancel:{x['id']}")]]))

@router.callback_query(F.data.startswith("cancel:"))
async def cancel(c:CallbackQuery):
    bid=int(c.data.split(':')[1]); item=await db.row("SELECT * FROM bookings WHERE id=? AND telegram_id=?",(bid,c.from_user.id))
    if not item: return await c.answer("Запись не найдена",show_alert=True)
    await db.execute("UPDATE bookings SET status='cancelled' WHERE id=?",(bid,)); await edit(c,"Запись отменена. Слот снова доступен.")

@router.message(F.text=="Услуги и цены")
async def prices(m:Message):
    items=await db.services(); await m.answer("\n".join(f"<b>{x['name']}</b> · {x['duration']} мин · {x['price'] or 'бесплатно'}{' ₽' if x['price'] else ''}" for x in items))

@router.message(F.text=="Позвать администратора")
async def human(m:Message,bot:Bot):
    await m.answer("Передал запрос администратору. Он увидит ваш Telegram и продолжит диалог здесь.")
    if settings.admin_id: await bot.send_message(settings.admin_id,f"Нужен администратор: @{m.from_user.username or 'без username'}, id {m.from_user.id}")

@router.message()
async def fallback(m:Message): await m.answer("Я умею записывать, показывать цены и отменять записи. Выберите действие кнопкой ниже.",reply_markup=menu())

def build_dispatcher():
    dp=Dispatcher(); dp.include_router(router); return dp
