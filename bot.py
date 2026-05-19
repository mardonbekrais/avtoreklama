import asyncio
import logging
import sqlite3
import json
import os
import random
import tempfile
import io
import qrcode
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
import re
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from telethon import TelegramClient
from telethon.sessions import StringSession
import telethon.errors

# BOT SOZLAMALARI
TOKEN = "8796202835:AAHVW4KUWIzPTvJv1pOeC-PEb33kmVOkQ-s"  # Bot tokeningiz
MY_API_ID = 34440062  # my.telegram.org saytidan olingan API ID
MY_API_HASH = "5e8c8b717354f310ccb4ce26ee152201"  # my.telegram.org saytidan olingan API HASH
ADMIN_IDS = [5606450682] # Adminlarning Telegram ID raqamlari (o'zingiznikini qo'shing)

bot = Bot(token=TOKEN)
dp = Dispatcher()
active_clients = {} # Login jarayonidagi klientlarni vaqtincha saqlash uchun
sending_tasks = {} # User_id: task ko'rinishidagi lug'at

def get_user_logger(user_id):
    logger = logging.getLogger(f"user_{user_id}")
    if not logger.handlers:
        os.makedirs("logs", exist_ok=True)
        file_handler = logging.FileHandler(f"logs/{user_id}.log", encoding='utf-8')
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.setLevel(logging.INFO)
        # Global loglarga xalaqit bermasligi uchun
        logger.propagate = False
    return logger

class Database:
    def __init__(self, db_name="bot_data.db"):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.create_tables()

    def create_tables(self):
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS profiles 
                            (phone TEXT, session TEXT, api_id INTEGER, api_hash TEXT, user_id INTEGER, PRIMARY KEY (phone, user_id))""")
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS templates 
                            (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, text TEXT, user_id INTEGER)""")
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS groups 
                            (group_id TEXT, name TEXT, selected INTEGER DEFAULT 0, user_id INTEGER, PRIMARY KEY (group_id, user_id))""")
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS settings 
                            (user_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (user_id, key))""")
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS users 
                            (user_id INTEGER PRIMARY KEY, full_name TEXT, username TEXT, 
                             plan TEXT DEFAULT 'free', is_blocked INTEGER DEFAULT 0, joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        self.conn.commit()

    # Profiles
    def add_profile(self, phone, session, api_id, api_hash, user_id):
        self.cursor.execute("INSERT OR REPLACE INTO profiles (phone, session, api_id, api_hash, user_id) VALUES (?, ?, ?, ?, ?)", (phone, session, api_id, api_hash, user_id))
        self.conn.commit()

    def get_profiles(self, user_id):
        self.cursor.execute("SELECT phone, session, api_id, api_hash FROM profiles WHERE user_id = ?", (user_id,))
        return [{"phone": r[0], "session": r[1], "api_id": r[2], "api_hash": r[3]} for r in self.cursor.fetchall()]

    def delete_profile(self, phone, user_id):
        self.cursor.execute("DELETE FROM profiles WHERE phone = ? AND user_id = ?", (phone, user_id))
        self.conn.commit()

    # Templates
    def add_template(self, name, text, user_id):
        self.cursor.execute("INSERT INTO templates (name, text, user_id) VALUES (?, ?, ?)", (name, text, user_id))
        self.conn.commit()

    def get_templates(self, user_id):
        self.cursor.execute("SELECT id, name, text FROM templates WHERE user_id = ?", (user_id,))
        return [{"id": r[0], "name": r[1], "text": r[2]} for r in self.cursor.fetchall()]

    def delete_template(self, tmpl_id):
        self.cursor.execute("DELETE FROM templates WHERE id = ?", (tmpl_id,))
        self.conn.commit()

    def update_template(self, tmpl_id, name, text):
        self.cursor.execute("UPDATE templates SET name = ?, text = ? WHERE id = ?", (name, text, tmpl_id))
        self.conn.commit()

    # Groups
    def save_groups(self, groups_dict):
        for gid, name in groups_dict.items():
            self.cursor.execute("INSERT OR REPLACE INTO groups (group_id, name, selected) VALUES (?, ?, (SELECT selected FROM groups WHERE group_id = ?))", (gid, name, gid))
        self.conn.commit()

    def get_all_groups(self):
        self.cursor.execute("SELECT group_id, name, selected FROM groups")
        return {r[0]: {"name": r[1], "selected": bool(r[2])} for r in self.cursor.fetchall()}

    def toggle_group(self, group_id):
        self.cursor.execute("UPDATE groups SET selected = 1 - selected WHERE group_id = ?", (group_id,))
        self.conn.commit()

    def get_selected_groups(self):
        self.cursor.execute("SELECT group_id FROM groups WHERE selected = 1")
        return [r[0] for r in self.cursor.fetchall()]

    # Settings
    def set_setting(self, key, value):
        self.cursor.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, str(value)))
        self.conn.commit()

    def get_setting(self, key, default=None):
        self.cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        res = self.cursor.fetchone()
        return res[0] if res else default

    # Users management
    def add_user(self, user_id, full_name, username):
        self.cursor.execute("INSERT OR IGNORE INTO users (user_id, full_name, username) VALUES (?, ?, ?)", 
                            (user_id, full_name, username))
        self.conn.commit()

    def get_user(self, user_id):
        self.cursor.execute("SELECT user_id, full_name, username, plan, is_blocked FROM users WHERE user_id = ?", (user_id,))
        r = self.cursor.fetchone()
        return {"id": r[0], "name": r[1], "username": r[2], "plan": r[3], "is_blocked": r[4]} if r else None

    def get_stats(self):
        self.cursor.execute("SELECT COUNT(*) FROM users")
        total = self.cursor.fetchone()[0]
        self.cursor.execute("SELECT COUNT(*) FROM users WHERE plan = 'premium'")
        premium = self.cursor.fetchone()[0]
        self.cursor.execute("SELECT COUNT(*) FROM profiles")
        accounts = self.cursor.fetchone()[0]
        return total, premium, accounts

    def get_all_users(self):
        self.cursor.execute("SELECT user_id, full_name, plan FROM users")
        return self.cursor.fetchall()

    def update_user_status(self, user_id, field, value):
        # field can be 'plan' or 'is_blocked'
        self.cursor.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
        self.conn.commit()

db = Database()

class ProfileStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code_part1 = State()
    waiting_for_code_part2 = State()
    waiting_for_2fa = State()
    waiting_for_template_name = State()
    waiting_for_template_text = State()
    waiting_for_manual_text = State()
    waiting_for_edit_tmpl_name = State()
    waiting_for_edit_tmpl_text = State()
    waiting_for_qr = State()
    waiting_for_broadcast = State()
    waiting_for_user_search = State()

def get_send_msg_menu_content(user_id):
    status = "🟢 ACTIVE" if user_id in sending_tasks else "🔴 STOPPED"
    selected_groups = db.get_selected_groups(user_id)
    interval = int(db.get_setting(user_id, "interval", "300")) // 60
    templates = db.get_templates(user_id)
    selected_idx = int(db.get_setting(user_id, "selected_template_idx", "-2"))
    
    text = (
        "<b>🚀 XABAR YUBORISH MENYUSI</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• Holat: <b>{status}</b>\n"
        f"• Tanlangan guruhlar: <b>{len(selected_groups)} ta</b>\n"
        f"• Tsikl: <b>{interval} min</b>\n\n"
        "Yuborish uchun shablon tanlang yoki yangi matn yozing:"
    )
    builder = InlineKeyboardBuilder()
    manual_indicator = "🟢 " if selected_idx == -1 else ""
    builder.row(types.InlineKeyboardButton(text=f"{manual_indicator}📝 Qo'lda yozish", callback_data="write_manual"))
    for i, t in enumerate(templates):
        indicator = "🟢 " if selected_idx == i else ""
        builder.row(types.InlineKeyboardButton(text=f"{indicator}📄 {t['name']}", callback_data=f"sel_tmpl_{i}"))
    
    btn_text = "🛑 TO'XTATISH" if user_id in sending_tasks else "▶️ BOSHLASH"
    builder.row(types.InlineKeyboardButton(text=btn_text, callback_data="toggle_send"))
    builder.row(types.InlineKeyboardButton(text="⬅️ ORQAGA", callback_data="main_menu"))
    return text, builder.as_markup()

def get_main_menu():
    """Asosiy menyu keyboardini yaratish (Grid style)"""
    builder = InlineKeyboardBuilder()
    
    # Xabar yuborish eng tepada katta bo'lib turishi uchun
    builder.row(
        types.InlineKeyboardButton(text="🚀 XABAR YUBORISH", callback_data="send_msg_menu")
    )
    builder.row(
        types.InlineKeyboardButton(text="👤 Profillar", callback_data="profiles"),
        types.InlineKeyboardButton(text="🛡 Guruhlar", callback_data="groups")
    )
    builder.row(
        types.InlineKeyboardButton(text="⏳ Tsikl oralig'i", callback_data="cycle"),
        types.InlineKeyboardButton(text="💎 Kabinet", callback_data="cabinet")
    )
    builder.row(
        types.InlineKeyboardButton(text="📝 Shablonlar", callback_data="templates")
    )
    # Adminlar uchun maxsus tugma
    # if user_id in ADMIN_IDS: ... (Inline keyboard user_id ni olmaydi, callbackda tekshiramiz)

    return builder.as_markup()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Start komandasi berilganda menyuni chiqarish"""
    user_id = message.from_user.id
    logger = get_user_logger(user_id)
    logger.info(f"Botga start berildi: {message.from_user.full_name} (@{message.from_user.username})")
    db.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    user = db.get_user(message.from_user.id)
    if user['is_blocked']:
        await message.answer("❌ Siz tizimdan bloklangansiz.")
        return

    header_text = (
        "<b>🟢 CORE SYSTEM v3.0 | PREMIUM PANEL</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👤 <b>Foydalanuvchi:</b> <code>" + message.from_user.full_name + "</code>\n"
        "⚡️ <b>Tizim Holati:</b> <code>ONLINE</code>\n"
        "🛡 <b>Dizayn:</b> <code>Cyberpunk Green</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Tizimni boshqarish uchun quyidagi menyudan foydalaning:</i>"
    )
    
    # Botga rasm yoki banner qo'shsangiz dizayn yanada professional chiqadi
    await message.answer(
        text=header_text,
        reply_markup=get_main_menu(),
        parse_mode=ParseMode.HTML
    )

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    total, premium, accounts = db.get_stats()
    text = (
        "<b>🛠 ADMIN PANEL</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Jami foydalanuvchilar: <b>{total} ta</b>\n"
        f"💎 Premium foydalanuvchilar: <b>{premium} ta</b>\n"
        f"📱 Ulangan akkauntlar: <b>{accounts} ta</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Boshqarish uchun tugmani tanlang:"
    )
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="admin_users"))
    builder.row(types.InlineKeyboardButton(text="📢 Reklama yuborish", callback_data="admin_broadcast"))
    builder.row(types.InlineKeyboardButton(text="⬅️ Chiqish", callback_data="main_menu"))
    
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)

# Universal callback handler barcha bo'limlar uchun
@dp.callback_query(F.data.in_({"profiles", "groups", "cycle", "cabinet", "templates", "send_msg_menu"}))
async def handle_menus(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user['is_blocked']:
        await callback.answer("❌ Bloklangansiz!", show_alert=True)
        return

    data = callback.data
    builder = InlineKeyboardBuilder()
    
    if data == "send_msg_menu":
        text, markup = get_send_msg_menu_content(callback.from_user.id)
        await callback.message.edit_text(text=text, reply_markup=markup, parse_mode=ParseMode.HTML)
        await callback.answer()
        return

    if data == "profiles":
        profiles = db.get_profiles(callback.from_user.id)
        text = (
            "<b>👤 TELEGRAM PROFILLARINI BOSHQARISH</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"• Ulangan profillar: 🟢 <b>{len(profiles)} ta faol</b>\n"
            "• Himoya darajasi: 🛡 <b>Yuqori (Personal App)</b>\n\n"
            "<i>O'chirish uchun profilni tanlang yoki yangi profil qo'shing:</i>"
        )
        for p in profiles:
            builder.row(types.InlineKeyboardButton(text=f"❌ {p['phone']}", callback_data=f"delete_prof_{p['phone']}"))
        builder.row(types.InlineKeyboardButton(text="➕ Profil qo'shish", callback_data="add_acc"))
        builder.row(types.InlineKeyboardButton(text="🔳 QR kod orqali ulanish", callback_data="add_acc_qr"))
        
    elif data == "groups":
        profiles = db.get_profiles(callback.from_user.id)
        if not profiles:
            text = "<b>⚠️ Avval profil ulang!</b>"
        else:
            all_groups = db.get_all_groups(callback.from_user.id)
            selected_count = len(db.get_selected_groups(callback.from_user.id))
            text = (
                "<b>🛡 GURUHLARNI NAZORAT QILISH</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Guruhni tanlang (🟢 - yuboriladi, 🔴 - yuborilmaydi):\n\n"
            )
            for gid, info in all_groups.items():
                status = "🟢" if info['selected'] else "🔴"
                builder.row(types.InlineKeyboardButton(text=f"{status} {info['name']}", callback_data=f"toggle_grp_{gid}"))
            
            if not all_groups:
                text += "<i>Guruhlar topilmadi. Ro'yxatni yangilang.</i>"
            
            builder.row(types.InlineKeyboardButton(text="🔄 Ro'yxatni yangilash", callback_data="refresh_groups"))

    elif data == "cycle":
        interval = int(db.get_setting(callback.from_user.id, "interval", 300)) // 60
        text = (
            "<b>⏳ TSIKL ORALIG'INI TANLANG</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Xabarlarni qayta yuborish vaqtini belgilang:\n\n"
            f"<i>Hozirgi interval:</i> 🟢 <b>{interval} minut</b>"
        )
        # Vaqt oralig'i tugmalari (Grid 3-3-1)
        builder.row(
            types.InlineKeyboardButton(text="1 min", callback_data="time_1m"),
            types.InlineKeyboardButton(text="2 min", callback_data="time_2m"),
            types.InlineKeyboardButton(text="3 min", callback_data="time_3m")
        )
        builder.row(
            types.InlineKeyboardButton(text="5 min", callback_data="time_5m"),
            types.InlineKeyboardButton(text="7 min", callback_data="time_7m"),
            types.InlineKeyboardButton(text="10 min", callback_data="time_10m")
        )
        builder.row(
            types.InlineKeyboardButton(text="1 soat", callback_data="time_1h")
        )

    elif data == "cabinet":
        user = db.get_user(callback.from_user.id)
        plan_status = "💎 PREMIUM" if user['plan'] == 'premium' else "🆓 TEKIN"
        text = (
            "<b>👤 SHAXSIY KABINET</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"• ID: <code>{user['id']}</code>\n"
            f"• Ism: <b>{user['name']}</b>\n"
            f"• Tarif: <b>{plan_status}</b>\n\n"
            "<b>Premium imkoniyatlari:</b>\n"
            "✅ Cheksiz profil qo'shish\n"
            "✅ Reklama guruhlarida ustunlik\n"
            "✅ 24/7 VIP Qo'llab-quvvatlash\n\n"
            "<i>Premium faollashtirish uchun adminga murojaat qiling.</i>"
        )
        if user['plan'] == 'free':
            builder.row(types.InlineKeyboardButton(text="🚀 Premiumga o'tish", url="https://t.me/admin_username"))

    elif data == "templates":
        templates = db.get_templates(callback.from_user.id)
        text = (
            "<b>📝 SHABLONLARNI BOSHQARISH</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
        )
        for t in templates:
            text += f"📄 <b>{t['name']}</b>\n"
            builder.row(
                types.InlineKeyboardButton(text="✏️ Tahrir", callback_data=f"edit_tmpl_{t['id']}"),
                types.InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"delete_tmpl_{t['id']}")
            )
        
        if not templates:
            text += "<i>Hali shablonlar yaratilmagan.</i>"
            
        builder.row(types.InlineKeyboardButton(text="➕ Yangi shablon", callback_data="add_template"))

    builder.row(types.InlineKeyboardButton(text="⬅️ ORQAGA", callback_data="main_menu"))
    
    await callback.message.edit_text(
        text=text,
        reply_markup=builder.as_markup(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

# --- ADMIN FUNKSIYALARI ---

@dp.callback_query(F.data == "admin_users")
async def admin_users_list(callback: types.CallbackQuery):
    users = db.get_all_users()
    text = "<b>👥 FOYDALANUVCHILAR RO'YXATI</b>\n\n"
    builder = InlineKeyboardBuilder()
    
    # Faqat oxirgi 10 ta userni ko'rsatamiz (limit uchun)
    for uid, name, plan in users[-10:]:
        p_icon = "💎" if plan == 'premium' else "👤"
        builder.row(types.InlineKeyboardButton(text=f"{p_icon} {name[:15]}", callback_data=f"manage_user_{uid}"))
    
    builder.row(types.InlineKeyboardButton(text="🔍 ID orqali qidirish", callback_data="search_user"))
    builder.row(types.InlineKeyboardButton(text="⬅️ ORQAGA", callback_data="admin_back"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "search_user")
async def admin_search_user_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS: return
    await state.set_state(ProfileStates.waiting_for_user_search)
    await callback.message.answer("🔍 Qidirilayotgan foydalanuvchi ID raqamini kiriting:")
    await callback.answer()

@dp.message(ProfileStates.waiting_for_user_search)
async def process_user_search(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    user_id_str = message.text.strip()
    
    if not user_id_str.isdigit():
        await message.answer("❌ ID faqat raqamlardan iborat bo'lishi kerak. Qayta urining:")
        return
        
    uid = int(user_id_str)
    user = db.get_user(uid)
    
    if not user:
        await message.answer("❌ Bunday ID ga ega foydalanuvchi topilmadi.", reply_markup=get_main_menu())
        await state.clear()
        return
        
    status = "🔴 Bloklangan" if user['is_blocked'] else "🟢 Faol"
    plan = "💎 Premium" if user['plan'] == 'premium' else "🆓 Tekin"
    
    text = (
        f"<b>USER TOPILDI:</b>\n"
        f"Ism: <b>{user['name']}</b>\n"
        f"ID: <code>{user['id']}</code>\n"
        f"Username: @{user['username']}\n"
        f"Holat: {status}\n"
        f"Tarif: {plan}"
    )
    
    builder = InlineKeyboardBuilder()
    new_plan = "free" if user['plan'] == 'premium' else "premium"
    btn_plan = "🆓 Rejani 'Tekin' qilish" if user['plan'] == 'premium' else "💎 Premium berish"
    builder.row(types.InlineKeyboardButton(text=btn_plan, callback_data=f"set_plan_{uid}_{new_plan}"))
    
    new_block = 0 if user['is_blocked'] else 1
    btn_block = "✅ Blokdan ochish" if user['is_blocked'] else "🚫 Bloklash"
    builder.row(types.InlineKeyboardButton(text=btn_block, callback_data=f"set_block_{uid}_{new_block}"))
    builder.row(types.InlineKeyboardButton(text="⬅️ ORQAGA", callback_data="admin_users"))
    
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)
    await state.clear()

@dp.callback_query(F.data.startswith("manage_user_"))
async def manage_user(callback: types.CallbackQuery):
    uid = int(callback.data.replace("manage_user_", ""))
    user = db.get_user(uid)
    
    status = "🔴 Bloklangan" if user['is_blocked'] else "🟢 Faol"
    plan = "💎 Premium" if user['plan'] == 'premium' else "🆓 Tekin"
    
    text = (
        f"<b>USER: {user['name']}</b>\n"
        f"ID: <code>{user['id']}</code>\n"
        f"Username: @{user['username']}\n"
        f"Holat: {status}\n"
        f"Tarif: {plan}"
    )
    
    builder = InlineKeyboardBuilder()
    
    # Plan toggle
    new_plan = "free" if user['plan'] == 'premium' else "premium"
    btn_plan = "🆓 Rejani 'Tekin' qilish" if user['plan'] == 'premium' else "💎 Premium berish"
    builder.row(types.InlineKeyboardButton(text=btn_plan, callback_data=f"set_plan_{uid}_{new_plan}"))
    
    # Block toggle
    new_block = 0 if user['is_blocked'] else 1
    btn_block = "✅ Blokdan ochish" if user['is_blocked'] else "🚫 Bloklash"
    builder.row(types.InlineKeyboardButton(text=btn_block, callback_data=f"set_block_{uid}_{new_block}"))
    
    builder.row(types.InlineKeyboardButton(text="⬅️ ORQAGA", callback_data="admin_users"))
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)

@dp.callback_query(F.data.startswith("set_plan_"))
async def admin_set_plan(callback: types.CallbackQuery):
    _, _, uid, plan = callback.data.split("_")
    db.update_user_status(int(uid), "plan", plan)
    await callback.answer(f"Tarif o'zgartirildi: {plan}")
    await manage_user(callback)

@dp.callback_query(F.data.startswith("set_block_"))
async def admin_set_block(callback: types.CallbackQuery):
    _, _, uid, val = callback.data.split("_")
    db.update_user_status(int(uid), "is_blocked", int(val))
    await callback.answer("Holat o'zgartirildi")
    await manage_user(callback)

@dp.callback_query(F.data == "admin_broadcast")
async def broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ProfileStates.waiting_for_broadcast)
    await callback.message.answer("📢 Barcha foydalanuvchilarga yuboriladigan xabarni kiriting:")
    await callback.answer()

@dp.message(ProfileStates.waiting_for_broadcast)
async def broadcast_process(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    
    users = db.get_all_users()
    count = 0
    msg = await message.answer(f"⏳ Yuborilmoqda: 0/{len(users)}")
    
    for user_id, _, _ in users:
        try:
            await message.copy_to(user_id)
            count += 1
            if count % 10 == 0:
                await msg.edit_text(f"⏳ Yuborilmoqda: {count}/{len(users)}")
        except:
            pass
        await asyncio.sleep(0.05)
        
    await msg.edit_text(f"✅ Xabar {count} ta foydalanuvchiga muvaffaqiyatli yuborildi.")
    await state.clear()

@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery):
    # Admin commandini qayta chaqirish simulyatsiyasi
    await callback.message.delete()
    await cmd_admin(callback.message)

@dp.callback_query(F.data.startswith("delete_prof_"))
async def delete_profile_handler(callback: types.CallbackQuery):
    phone = callback.data.replace("delete_prof_", "")
    logger = get_user_logger(callback.from_user.id)
    logger.info(f"Profil o'chirildi: {phone}")
    db.delete_profile(phone, callback.from_user.id)
    await callback.answer(f"✅ {phone} profili o'chirildi", show_alert=True)
    
    # Profil menyusini qayta chizish
    profiles = db.get_profiles(callback.from_user.id)
    builder = InlineKeyboardBuilder()
    text = (
        "<b>👤 TELEGRAM PROFILLARINI BOSHQARISH</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• Ulangan profillar: 🟢 <b>{len(profiles)} ta faol</b>\n"
        "• Himoya darajasi: 🛡 <b>Yuqori (Personal App)</b>\n\n"
        "<i>O'chirish uchun profilni tanlang yoki yangi profil qo'shing:</i>"
    )
    for p in profiles:
        builder.row(types.InlineKeyboardButton(text=f"❌ {p['phone']}", callback_data=f"delete_prof_{p['phone']}"))
    builder.row(types.InlineKeyboardButton(text="➕ Profil qo'shish", callback_data="add_acc"))
    builder.row(types.InlineKeyboardButton(text="⬅️ ORQAGA", callback_data="main_menu"))
    
    await callback.message.edit_text(text=text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)

# Profil qo'shish jarayoni
@dp.callback_query(F.data == "add_acc")
async def start_add_profile(callback: types.CallbackQuery, state: FSMContext):
    user = db.get_user(callback.from_user.id)
    profiles = db.get_profiles(callback.from_user.id)
    
    if user['plan'] == 'free' and len(profiles) >= 1:
        await callback.answer("⚠️ Tekin tarifda faqat 1 ta profil qo'shish mumkin! Premiumga o'ting.", show_alert=True)
        return

    # Keshni tozalash: eski holat va ulanishlarni o'chirish
    await state.clear()
    user_id = callback.from_user.id
    if user_id in active_clients:
        try:
            await active_clients[user_id].disconnect()
        except: pass
        del active_clients[user_id]
        
    await callback.answer()
    await state.set_state(ProfileStates.waiting_for_phone)
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_login"))
    
    await callback.message.edit_text(
        "<b>QADAM: Telefon raqami</b>\n\n"
        "Iltimos, akkaunt telefon raqamini xalqaro formatda yuboring (Masalan: <code>+998901234567</code>):",
        parse_mode=ParseMode.HTML,
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "add_acc_qr")
async def start_qr_login(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    
    status_msg = await callback.message.edit_text("⏳ QR kod generatsiya qilinmoqda...")
    
    client = TelegramClient(StringSession(), MY_API_ID, MY_API_HASH)
    await client.connect()
    active_clients[user_id] = client
    
    try:
        qr_login = await client.qr_login()
        
        # QR kod rasmini yaratish
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(qr_login.url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Rasmni xotirada saqlash
        bio = io.BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)
        
        qr_file = types.BufferedInputFile(bio.read(), filename="qr.png")
        
        await callback.message.delete() # Eski xabarni o'chirish
        sent_photo = await bot.send_photo(
            chat_id=callback.message.chat.id,
            photo=qr_file,
            caption=(
                "<b>🔳 QR KOD ORQALI KIRISH</b>\n\n"
                "1. Telegram sozlamalariga kiring (Settings)\n"
                "2. 'Qurilmalar' (Devices) bo'limini tanlang\n"
                "3. 'Qurilmani ulash' (Link Desktop Device) tugmasini bosing va ushbu kodni skanerlang.\n\n"
                "<i>Kod 60 soniya davomida faol bo'ladi.</i>"
            ),
            parse_mode=ParseMode.HTML
        )

        user = None
        timeout = 60
        step = 10

        try:
            # Foydalanuvchi skanerlashini kutish
            for remaining in range(timeout, 0, -step):
                try:
                    user = await qr_login.wait(timeout=step)
                    break
                except asyncio.TimeoutError:
                    if remaining <= step:
                        raise
                    
                    # Taymerni yangilash
                    try:
                        await bot.edit_message_caption(
                            chat_id=callback.message.chat.id,
                            message_id=sent_photo.message_id,
                            caption=(
                                "<b>🔳 QR KOD ORQALI KIRISH</b>\n\n"
                                "1. Telegram sozlamalariga kiring (Settings)\n"
                                "2. 'Qurilmalar' (Devices) bo'limini tanlang\n"
                                "3. 'Qurilmani ulash' (Link Desktop Device) tugmasini bosing va ushbu kodni skanerlang.\n\n"
                                f"<i>Kod {remaining - step} soniya davomida faol bo'ladi.</i>"
                            ),
                            parse_mode=ParseMode.HTML
                        )
                    except:
                        pass
            
            phone = user.phone if user.phone else f"QR_{user.id}"
            session_str = client.session.save()
            db.add_profile(phone, session_str, MY_API_ID, MY_API_HASH, callback.from_user.id)
            logger = get_user_logger(callback.from_user.id)
            logger.info(f"Yangi profil qo'shildi (QR): {phone}")
            
            await sent_photo.delete()
            await bot.send_message(callback.message.chat.id, f"✅ <b>Akkaunt muvaffaqiyatli ulandi!</b>\nFoydalanuvchi: {user.first_name}", reply_markup=get_main_menu(), parse_mode=ParseMode.HTML)
            
        except telethon.errors.SessionPasswordNeededError:
            await sent_photo.delete()
            await state.set_state(ProfileStates.waiting_for_2fa)
            await bot.send_message(
                callback.message.chat.id,
                "🔐 <b>2-BOSQICHLI PAROL (2FA)</b>\n\n"
                "Ushbu akkauntda ikki bosqichli tasdiqlash yoqilgan. Iltimos, parolingizni kiriting:",
                parse_mode=ParseMode.HTML
            )
            return # Finally blokida disconnect bo'lmasligi uchun

        except asyncio.TimeoutError:
            await sent_photo.delete()
            await bot.send_message(callback.message.chat.id, "❌ QR kod muddati tugadi. Qaytadan urinib ko'ring.", reply_markup=get_main_menu())
            
    except Exception as e:
        await bot.send_message(callback.message.chat.id, f"❌ Xatolik: {e}", reply_markup=get_main_menu())
    finally:
        # Agar 2FA kutilayotgan bo'lsa, ulanishni uzmaymiz
        if await state.get_state() == ProfileStates.waiting_for_2fa:
            return

        if user_id in active_clients:
            try:
                await active_clients[user_id].disconnect()
            except: pass
            del active_clients[user_id]
        await state.clear()

@dp.callback_query(F.data == "cancel_login")
async def cancel_login_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    if user_id in active_clients:
        try:
            await active_clients[user_id].disconnect()
        except: pass
        del active_clients[user_id]
    await callback.message.edit_text("❌ Jarayon bekor qilindi va kesh tozalandi.", reply_markup=get_main_menu())
    await callback.answer()

@dp.message(ProfileStates.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = re.sub(r'[^\d+]', '', message.text.strip())
    
    if not phone.startswith('+'):
        phone = '+' + phone
    
    # Qat'iy validatsiya: '+' bilan boshlanishi va undan keyin faqat raqamlar bo'lishi kerak
    if not re.fullmatch(r'^\+\d+$', phone):
        await message.answer("❌ Telefon raqami noto'g'ri formatda. Iltimos, raqamni '+' bilan boshlab, faqat raqamlardan iborat qilib kiriting (masalan: +998901234567).")
        return

    # Oldin ochilgan va yopilmay qolgan klient bo'lsa tozalaymiz
    if message.from_user.id in active_clients:
        try:
            await active_clients[message.from_user.id].disconnect()
        except: pass
        del active_clients[message.from_user.id]

    status_msg = await message.answer("⏳ Telegram serverlariga ulanmoqda...")

    client = TelegramClient(StringSession(), MY_API_ID, MY_API_HASH)
    
    try:
        await client.connect()
        sent_code = await client.send_code_request(phone)
        active_clients[message.from_user.id] = client
        logger = get_user_logger(message.from_user.id)
        logger.info(f"Login jarayoni boshlandi: {phone}")
        await state.update_data(phone=phone, phone_code_hash=sent_code.phone_code_hash)
        
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_login"))
        
        await state.set_state(ProfileStates.waiting_for_code_part1)
        await status_msg.edit_text(
            "<b>TASDIQLASH KODI (1-qism)</b>\n\n"
            "Telegramdan kelgan 5 xonali kodning <b>dastlabki 2 ta raqamini</b> yuboring.\n\n"
            "<i>Namuna: Kod <code>12345</code> bo'lsa, <code>12</code> ni yuboring.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=builder.as_markup()
        )
    except telethon.errors.FloodWaitError as e:
        await status_msg.edit_text(f"❌ Juda ko'p urinish! {e.seconds} soniyadan keyin qayta urining.")
        await state.clear()
        await client.disconnect()
    except telethon.errors.PhoneNumberInvalidError:
        await status_msg.edit_text("❌ Telefon raqami noto'g'ri. Iltimos, raqamni tekshirib qaytadan urinib ko'ring.")
        await client.disconnect()
        await state.clear()
    except Exception as e:
        await status_msg.edit_text(f"❌ Xato yuz berdi: {e}")
        await client.disconnect()
        await state.clear()

@dp.message(ProfileStates.waiting_for_code_part1)
async def process_code_part1(message: types.Message, state: FSMContext):
    code_p1 = message.text.strip()

    # Agar user kodni hammasini (5 ta raqam) kiritib yuborsa
    if code_p1.isdigit() and len(code_p1) == 5:
        await message.answer("⚠️ Kodni to'liq kiritish xavfsizlik qoidalariga zid! \n🛑 Tizim 1 minutga bloklandi. Iltimos kuting...")
        await state.clear()
        if message.from_user.id in active_clients:
            try:
                await active_clients[message.from_user.id].disconnect()
            except: pass
            del active_clients[message.from_user.id]
        await asyncio.sleep(60)
        await message.answer("🔄 Bloklash vaqti tugadi. Endi kesh tozalandi, qaytadan urinib ko'rishingiz mumkin.")
        return

    if not code_p1.isdigit() or len(code_p1) != 2:
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_login"))
        await message.answer("❌ Xato! Dastlabki 2 ta raqamni yuboring:", reply_markup=builder.as_markup())
        return
        
    await state.update_data(code_p1=code_p1)
    await state.set_state(ProfileStates.waiting_for_code_part2)
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_login"))
    await message.answer(
        "<b>TASDIQLASH KODI (2-qism)</b>\n\n"
        "Endi kodning <b>qolgan 3 ta raqamini</b> yuboring.\n\n"
        "<i>Namuna: Kod <code>12345</code> bo'lsa, <code>345</code> ni yuboring.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=builder.as_markup()
    )

@dp.message(ProfileStates.waiting_for_code_part2)
async def process_code_part2(message: types.Message, state: FSMContext):
    code_p2 = message.text.strip()

    if not code_p2.isdigit() or len(code_p2) != 3:
        await message.answer("❌ Xato! Qolgan 3 ta raqamni yuboring:")
        return

    user_data = await state.get_data()
    full_code = user_data['code_p1'] + code_p2
    phone = user_data['phone']
    phone_code_hash = user_data['phone_code_hash']
    client = active_clients.get(message.from_user.id)

    if not client:
        await message.answer("❌ Seans muddati o'tgan. Qaytadan urinib ko'ring.")
        return

    try:
        # Kodni tasdiqlab login qilamiz
        await client.sign_in(phone=phone, code=full_code, phone_code_hash=phone_code_hash)
        session_str = client.session.save() # Keyinchalik ishlatish uchun session string
        
        db.add_profile(phone, session_str, MY_API_ID, MY_API_HASH, message.from_user.id)
        logger = get_user_logger(message.from_user.id)
        logger.info(f"Yangi profil muvaffaqiyatli ulandi: {phone}")
        if message.from_user.id in active_clients:
            del active_clients[message.from_user.id]
        
        await message.answer(
            "✅ <b>Akkaunt muvaffaqiyatli ulandi!</b>\n\n"
            f"📱 Raqam: <code>{phone}</code>\n"
            "📡 Holat: 🟢 <b>ONLINE</b>\n\n"
            "Tizim ushbu akkaunt orqali ishlashga tayyor.",
            reply_markup=get_main_menu(),
            parse_mode=ParseMode.HTML
        )
        await client.disconnect()
        await state.clear()
    except telethon.errors.PhoneCodeInvalidError:
        await message.answer("❌ Tasdiqlash kodi noto'g'ri. Iltimos, qaytadan urinib ko'ring (Dastlabki 2 ta raqam):")
        await state.set_state(ProfileStates.waiting_for_code_part1)
    except telethon.errors.PhoneCodeExpiredError:
        await message.answer("❌ Tasdiqlash kodining muddati o'tgan. Iltimos, raqam kiritishdan boshlang.")
        await self._cleanup_connection(message.from_user.id, state)
    except telethon.errors.SessionPasswordNeededError:
        await state.set_state(ProfileStates.waiting_for_2fa)
        await message.answer(
            "🔐 <b>2-BOSQICHLI PAROL (2FA)</b>\n\n"
            "Akkauntingizda ikki bosqichli tasdiqlash yoqilgan. Iltimos, parolingizni kiriting:",
            parse_mode=ParseMode.HTML
        )
        # Clientni yopmaymiz, chunki sign_in(password) uchun u kerak
    except Exception as e:
        # Xatolik matnini tekshirish (fallback)
        err_str = str(e).lower()
        if "phone code" in err_str and "invalid" in err_str:
            await message.answer("❌ Tasdiqlash kodi noto'g'ri. Iltimos, qaytadan urinib ko'ring.\n\n<b>TASDIQLASH KODI (1-qism)</b>\n2 ta raqam yuboring:", parse_mode=ParseMode.HTML)
            await state.set_state(ProfileStates.waiting_for_code_part1)
            return
            
        await message.answer(f"❌ Xato: {e}")
        await self._cleanup_connection(message.from_user.id, state)

async def _cleanup_connection(self, user_id, state):
    """Ulanishni tozalash uchun yordamchi funksiya"""
    if user_id in active_clients:
        try:
            await active_clients[user_id].disconnect()
        except: pass
        del active_clients[user_id]
    await state.clear()

@dp.message(ProfileStates.waiting_for_2fa)
async def process_2fa(message: types.Message, state: FSMContext):
    password = message.text.strip()
    user_data = await state.get_data()
    phone = user_data.get('phone')
    client = active_clients.get(message.from_user.id)

    if not client:
        await message.answer("❌ Seans muddati o'tgan yoki ulanish uzilgan. Qaytadan urinib ko'ring.")
        await state.clear()
        return

    try:
        # 2FA paroli bilan kirish
        await client.sign_in(password=password)
        user = await client.get_me()
        phone = user.phone if user.phone else f"ID_{user.id}"
        session_str = client.session.save()
        
        db.add_profile(phone, session_str, MY_API_ID, MY_API_HASH, message.from_user.id)
        logger = get_user_logger(message.from_user.id)
        logger.info(f"Profil 2FA orqali ulandi: {phone}")
        if message.from_user.id in active_clients:
            del active_clients[message.from_user.id]
            
        await message.answer(
            "✅ <b>Akkaunt muvaffaqiyatli ulandi (2FA orqali)!</b>",
            reply_markup=get_main_menu(),
            parse_mode=ParseMode.HTML
        )
        await client.disconnect()
        await state.clear()
    except telethon.errors.PasswordHashInvalidError:
        await message.answer("❌ 2-bosqichli parol noto'g'ri. Iltimos, qaytadan kiriting:")
    except Exception as e:
        await message.answer(f"❌ Xato: {e}")
        if message.from_user.id in active_clients:
            await client.disconnect()
            del active_clients[message.from_user.id]
        await state.clear()

# Guruhlarni yangilash
@dp.callback_query(F.data == "refresh_groups")
async def refresh_groups_handler(callback: types.CallbackQuery):
    profiles = db.get_profiles(callback.from_user.id)
    if not profiles:
        await callback.answer("❌ Avval kamida bitta profil ulashingiz kerak!", show_alert=True)
        return

    await callback.message.edit_text("🔄 Barcha profillardan guruhlar yig'ilmoqda, iltimos kuting...")

    found_groups = {}
    for profile in profiles:
        try:
            client = TelegramClient(StringSession(profile["session"]), MY_API_ID, MY_API_HASH)
            await client.connect()
            async for dialog in client.iter_dialogs():
                # Faqat guruhlar va superguruhlarni olamiz (kanallarni emas)
                if dialog.is_group or (dialog.is_channel and dialog.entity.megagroup):
                    found_groups[str(dialog.id)] = dialog.name
            await client.disconnect()
        except Exception as e:
            logging.error(f"Profil skanerlashda xato ({profile['phone']}): {e}")

    db.save_groups(found_groups, callback.from_user.id)
    logger = get_user_logger(callback.from_user.id)
    logger.info(f"Guruhlar ro'yxati yangilandi. Jami: {len(found_groups)}")
    await callback.answer("✅ Guruhlar ro'yxati yangilandi!", show_alert=True)
    await handle_menus(callback)

@dp.callback_query(F.data.startswith("toggle_grp_"))
async def toggle_group(callback: types.CallbackQuery):
    gid = callback.data.replace("toggle_grp_", "")
    db.toggle_group(gid, callback.from_user.id)
    await handle_menus(callback)

@dp.callback_query(F.data.startswith("delete_tmpl_"))
async def delete_template_handler(callback: types.CallbackQuery):
    tmpl_id = int(callback.data.replace("delete_tmpl_", ""))
    db.delete_template(tmpl_id)
    await callback.answer("✅ Shablon o'chirildi", show_alert=True)
    
    # Shablonlar menyusini qayta chizish
    templates = db.get_templates(callback.from_user.id)
    builder = InlineKeyboardBuilder()
    text = "<b>📝 SHABLONLARNI BOSHQARISH</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    for t in templates:
        text += f"📄 <b>{t['name']}</b>\n"
        builder.row(
            types.InlineKeyboardButton(text="✏️ Tahrir", callback_data=f"edit_tmpl_{t['id']}"),
            types.InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"delete_tmpl_{t['id']}")
        )
    if not templates: text += "<i>Hali shablonlar yaratilmagan.</i>"
    builder.row(types.InlineKeyboardButton(text="➕ Yangi shablon", callback_data="add_template"))
    builder.row(types.InlineKeyboardButton(text="⬅️ ORQAGA", callback_data="main_menu"))
    
    await callback.message.edit_text(text=text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)

@dp.callback_query(F.data.startswith("edit_tmpl_"))
async def edit_template_start(callback: types.CallbackQuery, state: FSMContext):
    tmpl_id = int(callback.data.replace("edit_tmpl_", ""))
    await state.update_data(edit_tmpl_id=tmpl_id)
    await state.set_state(ProfileStates.waiting_for_edit_tmpl_name)
    await callback.message.answer("📝 Shablon uchun <b>yangi nom</b> kiriting:", parse_mode=ParseMode.HTML)
    await callback.answer()

@dp.message(ProfileStates.waiting_for_edit_tmpl_name)
async def edit_tmpl_name(message: types.Message, state: FSMContext):
    await state.update_data(edit_tmpl_name=message.text)
    await state.set_state(ProfileStates.waiting_for_edit_tmpl_text)
    await message.answer("📝 Endi shablon uchun <b>yangi matnni</b> kiriting:", parse_mode=ParseMode.HTML)

@dp.message(ProfileStates.waiting_for_edit_tmpl_text)
async def edit_tmpl_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    db.update_template(data["edit_tmpl_id"], data["edit_tmpl_name"], message.text)
    await state.clear()
    await message.answer(
        f"✅ Shablon '{data['edit_tmpl_name']}' yangilandi!",
        reply_markup=get_main_menu(),
        parse_mode=ParseMode.HTML
    )

# Shablon yaratish jarayoni
@dp.callback_query(F.data == "add_template")
async def add_template_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ProfileStates.waiting_for_template_name)
    await callback.message.answer("📝 Shablon uchun <b>nom</b> kiriting (masalan: <i>Reklama 1</i>):", parse_mode=ParseMode.HTML)

@dp.message(ProfileStates.waiting_for_template_name)
async def tmpl_name(message: types.Message, state: FSMContext):
    await state.update_data(tmpl_name=message.text)
    await state.set_state(ProfileStates.waiting_for_template_text)
    await message.answer("📝 Endi shablon <b>matnini</b> kiriting:", parse_mode=ParseMode.HTML)

@dp.message(ProfileStates.waiting_for_template_text)
async def tmpl_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    db.add_template(data["tmpl_name"], message.text, message.from_user.id)
    await state.clear()
    await message.answer(f"✅ Shablon '{data['tmpl_name']}' saqlandi!", reply_markup=get_main_menu())

# Xabar yuborish logikasi
async def auto_send_loop(user_id):
    logger = get_user_logger(user_id)
    logger.info("Avto-yuborish sikli ishga tushirildi.")
    while True:
        active_text = db.get_setting(user_id, "active_text")
        selected_groups = db.get_selected_groups(user_id)
        profiles = db.get_profiles(user_id)
        interval = int(db.get_setting(user_id, "interval", 300))

        if not active_text or not selected_groups or not profiles:
            logger.warning("Yuborish uchun ma'lumotlar yetarli emas (matn, guruhlar yoki profil).")
            break

        admin_id = user_id # Xabarlarni o'ziga yuboradi
        
        # Har bir profil uchun alohida ulanish ochamiz
        for profile in profiles:
            if user_id not in sending_tasks: break
            
            client = TelegramClient(StringSession(profile["session"]), MY_API_ID, MY_API_HASH)
            try:
                await client.connect()
                for gid in selected_groups:
                    if user_id not in sending_tasks: break
                    try:
                        await client.send_message(int(gid), active_text)
                        logger.info(f"Xabar yuborildi: {gid} (Profil: {profile['phone']})")
                        await asyncio.sleep(random.uniform(5, 10)) # Spamdan himoya uchun intervalni biroz oshirdik
                    except telethon.errors.PeerFloodError:
                        logger.error(f"Spam cheklovi (FloodError): {profile['phone']}")
                        if admin_id:
                            await bot.send_message(admin_id, f"<b>⚠️ DIQQAT: {profile['phone']} spamga tushdi!</b>", parse_mode="HTML")
                        break # Bu profilni to'xtatib keyingisiga o'tamiz
                    except Exception:
                        continue # Agar bu profil bu guruhda bo'lmasa yoki boshqa xato bo'lsa keyingisiga o'tamiz
                await client.disconnect()
            except Exception as e:
                logging.error(f"Yuborishda xatolik ({profile['phone']}): {e}")

        # Agar yuborish to'xtatilgan bo'lsa kutishni to'xtatish
        for _ in range(interval):
            if user_id not in sending_tasks: break
            await asyncio.sleep(1)

@dp.callback_query(F.data == "toggle_send")
async def toggle_send_process(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    logger = get_user_logger(user_id)
    if user_id in sending_tasks:
        sending_tasks[user_id].cancel()
        del sending_tasks[user_id]
        db.set_setting(user_id, "active_text", "")
        db.set_setting(user_id, "selected_template_idx", "-2")
        logger.info("Avto-yuborish to'xtatildi.")
        await callback.answer("🛑 Yuborish to'xtatildi!", show_alert=True)
    else:
        active_text = db.get_setting(user_id, "active_text")
        selected_groups = db.get_selected_groups(user_id)
        if not active_text:
            await callback.answer("⚠️ Avval shablon tanlang yoki matn yozing!", show_alert=True)
            return
        if not selected_groups:
            await callback.answer("⚠️ Kamida bitta guruhni tanlang!", show_alert=True)
            return
            
        sending_tasks[user_id] = asyncio.create_task(auto_send_loop(user_id))
        logger.info("Avto-yuborish boshlandi.")
        await callback.answer("▶️ Avto-yuborish boshlandi!", show_alert=True)
    
    text, markup = get_send_msg_menu_content(user_id)
    await callback.message.edit_text(text=text, reply_markup=markup, parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "write_manual")
async def manual_msg_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ProfileStates.waiting_for_manual_text)
    await callback.message.answer("⌨️ Guruhlarga yuboriladigan matnni kiriting:", parse_mode=ParseMode.HTML)

@dp.message(ProfileStates.waiting_for_manual_text)
async def manual_msg_recv(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    logger = get_user_logger(user_id)
    db.set_setting(user_id, "active_text", message.text)
    db.set_setting(user_id, "selected_template_idx", "-1")
    await state.clear()
    
    selected_groups = db.get_selected_groups(user_id)
    if selected_groups and user_id not in sending_tasks:
        sending_tasks[user_id] = asyncio.create_task(auto_send_loop(user_id))
        logger.info("Qo'lda yozilgan matn qabul qilindi, yuborish boshlandi.")
        await message.answer("✅ Matn qabul qilindi va avtomatik yuborish boshlandi!")
    
    text, markup = get_send_msg_menu_content(user_id)
    await message.answer(text=text, reply_markup=markup, parse_mode=ParseMode.HTML)

@dp.callback_query(F.data.startswith("sel_tmpl_"))
async def select_template(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    logger = get_user_logger(user_id)
    idx = int(callback.data.replace("sel_tmpl_", ""))
    templates = db.get_templates(user_id)
    template = templates[idx]
    
    db.set_setting(user_id, "active_text", template["text"])
    db.set_setting(user_id, "selected_template_idx", str(idx))
    
    selected_groups = db.get_selected_groups(user_id)
    if selected_groups and user_id not in sending_tasks:
        sending_tasks[user_id] = asyncio.create_task(auto_send_loop(user_id))
        logger.info(f"Shablon tanlandi: {template['name']}, yuborish boshlandi.")
        await callback.answer(f"✅ '{template['name']}' tanlandi va avtomatik yuborish boshlandi!", show_alert=True)
    else:
        await callback.answer(f"✅ '{template['name']}' tanlandi!")

    text, markup = get_send_msg_menu_content()
    await callback.message.edit_text(text=text, reply_markup=markup, parse_mode=ParseMode.HTML)

# 12 soatlik spam nazorati vazifasi
async def spam_monitor_loop():
    while True:
        await asyncio.sleep(12 * 3600) # 12 soat kutish
        profiles = db.get_profiles()
        admin_id = db.get_setting("admin_id")
        if not profiles or not admin_id:
            continue
            
        report = "<b>📊 12 SOATLIK AKKAUNTLAR HOLATI</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for profile in profiles:
            client = TelegramClient(StringSession(profile["session"]), MY_API_ID, MY_API_HASH)
            try:
                await client.connect()
                me = await client.get_me()
                status = "🟢 100% (Sog'lom)" if not me.restricted else "🔴 0% (Spamda)"
                report += f"👤 {profile['phone']}: {status}\n"
                await client.disconnect()
            except Exception:
                report += f"👤 {profile['phone']}: ⚠️ Xatolik\n"
        
        report += "━━━━━━━━━━━━━━━━━━━━\n<i>Keyingi tekshiruv 12 soatdan keyin.</i>"
        await bot.send_message(admin_id, report, parse_mode="HTML")

# Vaqt tanlash callbacklari uchun oddiy handler
@dp.callback_query(F.data.startswith("time_"))
async def process_time_selection(callback: types.CallbackQuery):
    t_str = callback.data.split("_")[1]
    interval = 300
    if "m" in t_str:
        interval = int(t_str.replace("m", "")) * 60
    else:
        interval = 3600
    
    db.set_setting(callback.from_user.id, "interval", interval)
    selected_time = t_str.replace("m", " minut").replace("h", " soat")
    await callback.answer(f"✅ Tsikl oralig'i {selected_time}ga o'rnatildi", show_alert=True)
    await back_to_main(callback)

@dp.callback_query(F.data == "main_menu")
async def back_to_main(callback: types.CallbackQuery):
    """Asosiy menyuga qaytish"""
    await callback.message.edit_text(
        text=(
            "<b>🟢 CORE SYSTEM v3.0 | PREMIUM PANEL</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Kerakli bo'limni tanlang:</i>"
        ),
        reply_markup=get_main_menu(),
        parse_mode=ParseMode.HTML
    )
    await callback.answer()

async def handle_ping(request):
    return web.Response(text="Bot is alive!")

async def main():
    logging.basicConfig(level=logging.INFO)
    print("Bot ishga tushdi...")
    
    # Render uchun veb-server (Keep-alive)
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Render avtomatik PORT beradi, agar bo'lmasa 8080 ishlatamiz
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    asyncio.create_task(site.start())
    
    asyncio.create_task(spam_monitor_loop()) # Spam monitorni ishga tushirish
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot to'xtatildi.")
