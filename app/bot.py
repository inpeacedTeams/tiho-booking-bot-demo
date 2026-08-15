from datetime import date,timedelta,datetime
from aiogram import Bot,Dispatcher,Router,F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State,StatesGroup
from aiogram.types import Message,CallbackQuery,InlineKeyboardButton,InlineKeyboardMarkup,KeyboardButton,ReplyKeyboardMarkup,ReplyKeyboardRemove,WebAppInfo
from aiogram.exceptions import TelegramBadRequest
from .config import settings
from . import db
from .security import normalize_phone
router=Router()
class Booking(StatesGroup):service=State();staff=State();day=State();slot=State();name=State();phone=State();confirm=State()
def ik(rows):return InlineKeyboardMarkup(inline_keyboard=rows)
def menu():
    rows=[[KeyboardButton(text="Записаться"),KeyboardButton(text="Мои записи")],[KeyboardButton(text="Услуги и цены"),KeyboardButton(text="Позвать администратора")]]
    if settings.webapp_url.startswith("https://"):rows.append([KeyboardButton(text="Кабинет владельца",web_app=WebAppInfo(url=settings.webapp_url))])
    return ReplyKeyboardMarkup(keyboard=rows,resize_keyboard=True)
async def edit(call,text,markup=None):
    try:await call.message.edit_text(text,reply_markup=markup)
    except TelegramBadRequest:await call.message.answer(text,reply_markup=markup)
    await call.answer()
@router.message(CommandStart())
async def start(m:Message,state:FSMContext):
    await state.clear();parts=(m.text or '').split(maxsplit=1)
    if len(parts)==2 and parts[1].startswith('verify_'):
        token=parts[1][7:]
        if await db.attach_verification(token,m.from_user.id):return await m.answer("Нажмите кнопку ниже. Telegram передаст только ваш номер.",reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Подтвердить мой номер",request_contact=True)]],resize_keyboard=True,one_time_keyboard=True))
        return await m.answer("Ссылка устарела. Запросите новую на странице записи.",reply_markup=menu())
    await m.answer("Привет! Я помощник салона «Тихо».",reply_markup=menu())
@router.callback_query(F.data.startswith("manual_ok:"))
async def manual_ok(c:CallbackQuery):
    if settings.admin_id and c.from_user.id!=settings.admin_id:return await c.answer("Нет доступа",show_alert=True)
    token=c.data.split(':',1)[1];changed=await db.review_manual_verification(token,True,c.from_user.id);await edit(c,"Номер подтверждён администратором." if changed else "Запрос уже обработан или устарел.")
@router.callback_query(F.data.startswith("manual_no:"))
async def manual_no(c:CallbackQuery):
    if settings.admin_id and c.from_user.id!=settings.admin_id:return await c.answer("Нет доступа",show_alert=True)
    token=c.data.split(':',1)[1];changed=await db.review_manual_verification(token,False,c.from_user.id);await edit(c,"Запрос отклонён." if changed else "Запрос уже обработан или устарел.")
@router.message(F.text=="Записаться")
async def begin(m,state):
    await state.clear();items=await db.services();await state.set_state(Booking.service);await m.answer("Что хотите сделать?",reply_markup=ik([[InlineKeyboardButton(text=f"{x['name']} · {x['price'] or 'бесплатно'}{' ₽' if x['price'] else ''}",callback_data=f"svc:{x['id']}")] for x in items]))
@router.callback_query(Booking.service,F.data.startswith("svc:"))
async def service(c,state):
    item=await db.row("SELECT * FROM services WHERE id=? AND enabled=1",(int(c.data.split(':')[1]),));await state.update_data(service=item);await state.set_state(Booking.staff);people=await db.staff();await edit(c,"Выберите мастера:",ik([[InlineKeyboardButton(text=x['name'],callback_data=f"staff:{x['id']}")] for x in people]))
@router.callback_query(Booking.staff,F.data.startswith("staff:"))
async def staff(c,state):
    person=await db.row("SELECT * FROM staff WHERE id=?",(int(c.data.split(':')[1]),));await state.update_data(staff=person);await state.set_state(Booking.day);days=[date.today()+timedelta(days=i) for i in range(1,8)];await edit(c,"Выберите день:",ik([[InlineKeyboardButton(text=d.strftime('%d.%m'),callback_data=f"day:{d.isoformat()}")] for d in days]))
@router.callback_query(Booking.day,F.data.startswith("day:"))
async def day(c,state):
    value=c.data.split(':',1)[1];data=await state.get_data();slots=await db.free_slots(data['staff']['id'],value)
    if not slots:return await edit(c,"Свободных окон нет.")
    await state.update_data(day=value);await state.set_state(Booking.slot);await edit(c,"Свободные окна:",ik([[InlineKeyboardButton(text=t,callback_data=f"slot:{t}") for t in slots]]))
@router.callback_query(Booking.slot,F.data.startswith("slot:"))
async def slot(c,state):await state.update_data(slot=c.data.split(':')[1]);await state.set_state(Booking.name);await edit(c,"Как вас зовут?")
@router.message(Booking.name)
async def get_name(m,state):await state.update_data(name=(m.text or '').strip());await state.set_state(Booking.phone);await m.answer("Оставьте номер:",reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Поделиться номером",request_contact=True)]],resize_keyboard=True,one_time_keyboard=True))
@router.message(Booking.phone)
async def get_phone(m,state):
    phone=m.contact.phone_number if m.contact else(m.text or '').strip();await state.update_data(phone=phone);data=await state.get_data();await state.set_state(Booking.confirm);await m.answer(f"{data['service']['name']}\n{data['day']} в {data['slot']}\nМастер {data['staff']['name']}",reply_markup=ik([[InlineKeyboardButton(text="Подтвердить",callback_data="confirm:yes")]]))
@router.callback_query(Booking.confirm,F.data=="confirm:yes")
async def confirm(c,state):
    data=await state.get_data()
    try:bid=await db.execute("INSERT INTO bookings(telegram_id,customer_name,phone,service_id,staff_id,starts_at,phone_verified,verification_method) VALUES(?,?,?,?,?,?,1,'telegram')",(c.from_user.id,data['name'],data['phone'],data['service']['id'],data['staff']['id'],f"{data['day']}T{data['slot']}"))
    except Exception:return await edit(c,"Окно уже заняли.")
    await state.clear();await edit(c,f"Готово! Запись №{bid} подтверждена.")
@router.message(F.contact)
async def verification_contact(m:Message):
    pending=await db.pending_verification(m.from_user.id)
    if not pending:return
    if m.contact.user_id and m.contact.user_id!=m.from_user.id:return await m.answer("Нужно отправить свой контакт.")
    try:phone=normalize_phone(m.contact.phone_number)
    except ValueError:return await m.answer("Номер неподдерживаемого формата.")
    result=await db.verify_contact(m.from_user.id,phone)
    if result=='verified':await m.answer("Номер подтверждён. Возвращайтесь на страницу записи.",reply_markup=ReplyKeyboardRemove())
    elif result=='mismatch':await m.answer("Номер не совпадает с указанным на странице.",reply_markup=ReplyKeyboardRemove())
@router.message(F.text=="Мои записи")
async def mine(m):
    items=await db.rows("SELECT b.*,s.name service,st.name staff FROM bookings b JOIN services s ON s.id=b.service_id JOIN staff st ON st.id=b.staff_id WHERE telegram_id=? AND status='confirmed' AND starts_at>=?",(m.from_user.id,datetime.now().isoformat(timespec='minutes')))
    if not items:return await m.answer("Активных записей нет.")
    for x in items:await m.answer(f"<b>{x['service']}</b>\n{x['starts_at'].replace('T',' в ')} · {x['staff']}")
@router.message(F.text=="Услуги и цены")
async def prices(m):
    items=await db.services();await m.answer("\n".join(f"<b>{x['name']}</b> · {x['price']} ₽" for x in items))
@router.message()
async def fallback(m):await m.answer("Выберите действие кнопкой ниже.",reply_markup=menu())
def build_dispatcher():dp=Dispatcher();dp.include_router(router);return dp
