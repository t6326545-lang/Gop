import aiosqlite
import asyncio
import os
import logging
import requests
import random
from flask import Flask
from threading import Thread
from datetime import datetime, timedelta
from telethon import TelegramClient, events, Button
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.functions.channels import GetMessagesRequest
from telethon.tl.functions.messages import GetMessagesViewsRequest
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.messages import GetDialogsRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import InputPeerChannel, InputPeerChat
from telethon.errors import (
    MessageNotModifiedError, 
    ChannelPrivateError, 
    PeerIdInvalidError, 
    SessionPasswordNeededError,
    UserAlreadyParticipantError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError
)

# Configuration
API_ID = int(os.getenv("API_ID", "33112812"))
API_HASH = os.getenv("API_HASH", "e12bb0150c7e5475e2460fdce18bfe82")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8800745759:AAEw-aPjINQMMkqW92WX3ApI8SIrXiJoNpQ")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8211510972"))

# Railway Volume Support (Ensures data persistence)
DATA_DIR = os.getenv("DATA_DIR", "/data") 
DATABASE_NAME = os.path.join(DATA_DIR, "bot_database.db")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
MENU_IMAGE_PATH = os.path.join(DATA_DIR, "menu_image.jpg")
DEFAULT_MENU_IMAGE = "https://kommodo.ai/i/Td5qP4VC7pxGcTyrXeEy"

os.makedirs(SESSIONS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO)

# ==================== DATABASE ====================
async def init_db():
    async with aiosqlite.connect(DATABASE_NAME) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, subscription_expiry TEXT, is_admin INTEGER DEFAULT 0)")
        await db.execute("CREATE TABLE IF NOT EXISTS user_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, phone TEXT, session_name TEXT, is_active INTEGER DEFAULT 1)")
        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute("INSERT OR IGNORE INTO users (user_id, is_admin) VALUES (?, 1)", (ADMIN_ID,))
        await db.commit()

async def get_setting(key, default=None):
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else default

async def set_setting(key, value):
    async with aiosqlite.connect(DATABASE_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

async def is_authorized(user_id):
    if user_id == ADMIN_ID: return True
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT is_admin, subscription_expiry FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                is_admin = row[0]
                subscription_expiry = row[1]
                if is_admin == 1: return True
                if subscription_expiry:
                    try:
                        expiry = datetime.strptime(subscription_expiry, '%Y-%m-%d %H:%M:%S')
                        return expiry > datetime.now()
                    except: return False
            return False

async def add_subscription(user_id, days):
    expiry_str = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,)) as cursor:
            existing_user = await cursor.fetchone()
            current_is_admin = existing_user[0] if existing_user else 0
        await db.execute("INSERT OR REPLACE INTO users (user_id, subscription_expiry, is_admin) VALUES (?, ?, ?)", (user_id, expiry_str, current_is_admin))
        await db.commit()
    return expiry_str

async def count_user_sessions(owner_id):
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM user_sessions WHERE owner_id = ? AND is_active = 1", (owner_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def count_all_sessions():
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM user_sessions WHERE is_active = 1") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def add_session(owner_id, phone, session_name):
    async with aiosqlite.connect(DATABASE_NAME) as db:
        await db.execute("INSERT INTO user_sessions (owner_id, phone, session_name) VALUES (?, ?, ?)", (owner_id, phone, session_name))
        await db.commit()

async def get_user_sessions(owner_id):
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT * FROM user_sessions WHERE owner_id = ? AND is_active = 1", (owner_id,)) as cursor:
            return await cursor.fetchall()

async def get_all_active_sessions():
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute("SELECT * FROM user_sessions WHERE is_active = 1") as cursor:
            return await cursor.fetchall()

# ==================== SESSION MANAGER ====================
class SessionManager:
    def __init__(self):
        self.clients = {}
        self.active_clients = {}
        self.is_global_online = False 
        self.stop_tasks = {}

    async def start_login(self, user_id, phone):
        session_path = os.path.join(SESSIONS_DIR, f"user_{user_id}_{phone}")
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            try:
                sent_code = await client.send_code_request(phone)
                self.clients[user_id] = {'client': client, 'phone': phone, 'phone_code_hash': sent_code.phone_code_hash}
                return "otp_required"
            except Exception as e:
                await client.disconnect()
                return str(e)
        else:
            session_name = f"user_{user_id}_{phone}"
            await add_session(user_id, phone, session_name)
            self.active_clients[session_name] = client
            return "success"

    async def complete_login(self, user_id, otp_or_pass):
        if user_id not in self.clients: return "no_pending_login"
        data = self.clients[user_id]
        try:
            if data.get('waiting_password'):
                await data['client'].sign_in(password=otp_or_pass)
            else:
                try:
                    await data['client'].sign_in(data['phone'], otp_or_pass, phone_code_hash=data['phone_code_hash'])
                except SessionPasswordNeededError:
                    data['waiting_password'] = True
                    return "password_required"
            
            session_name = f"user_{user_id}_{data['phone']}"
            await add_session(user_id, data['phone'], session_name)
            self.active_clients[session_name] = data['client']
            del self.clients[user_id]
            return "success"
        except Exception as e: return str(e)

    async def get_client(self, session_name):
        if session_name in self.active_clients:
            client = self.active_clients[session_name]
            if not client.is_connected(): 
                try: await asyncio.wait_for(client.connect(), timeout=10)
                except: return None
            return client
        session_path = os.path.join(SESSIONS_DIR, session_name)
        if os.path.exists(session_path + ".session"):
            try:
                client = TelegramClient(session_path, API_ID, API_HASH)
                await asyncio.wait_for(client.connect(), timeout=10)
                if not await client.is_user_authorized():
                    await client.disconnect()
                    return None
                self.active_clients[session_name] = client
                return client
            except: return None
        return None

    async def join_group(self, session_name, link):
        client = await self.get_client(session_name)
        if not client: return False, "Login expired"
        try:
            link = link.strip().rstrip('/')
            if "t.me/+" in link or "joinchat" in link:
                invite_hash = link.split('/')[-1].replace('+', '')
                await asyncio.wait_for(client(ImportChatInviteRequest(invite_hash)), timeout=20)
            else:
                username = link.split('/')[-1]
                await asyncio.wait_for(client(JoinChannelRequest(username)), timeout=20)
            return True, "Success"
        except UserAlreadyParticipantError as e:
            if "You have successfully requested to join this chat or channel" in str(e):
                return True, "Join Request Sent"
            return True, "Already in"
        except FloodWaitError as e: return False, f"Flood {e.seconds}s"
        except Exception as e: return False, str(e)

    async def send_view(self, session_name, post_link):
        client = await self.get_client(session_name)
        if not client: return False, "Session not found"
        try:
            link = post_link.replace('https://', '').replace('http://', '').replace('t.me/', '').rstrip('/')
            parts = link.split('/')
            if len(parts) < 2: return False, "Invalid link"
            msg_id = int(parts[-1])
            if parts[0] == 'c':
                peer_id = int(f"-100{parts[1]}")
            else:
                entity = await client.get_entity(parts[0])
                peer_id = entity.id
            await asyncio.wait_for(client(GetMessagesViewsRequest(peer=peer_id, id=[msg_id], increment=True)), timeout=15)
            return True, "Success"
        except Exception as e: return False, str(e)

    async def send_views_to_post(self, user_id, post_link, delay=1, event=None):
        sessions = await get_all_active_sessions()
        if not sessions: return 0, "❌ No IDs found!"
        
        sessions = list(sessions)
        random.shuffle(sessions)
        
        successful_views = 0
        failed_views = 0
        self.stop_tasks[user_id] = False
        for i, session in enumerate(sessions):
            if self.stop_tasks.get(user_id, False): break
            success, reason = await self.send_view(session[3], post_link)
            if success: successful_views += 1
            else: failed_views += 1
            if event and i % 2 == 0:
                try:
                    await event.edit(f"⏳ Views: {i+1}/{len(sessions)}\n✅ OK: {successful_views} | ❌ Fail: {failed_views}", buttons=[Button.inline("⏹️ Stop", data="stop_task")])
                except Exception as e:
                    logging.error(f"Failed to edit progress message: {e}")
            await asyncio.sleep(delay)
        return successful_views, f"✅ Views Done!\n✅ Success: {successful_views}\n❌ Failed: {failed_views}"

    async def leave_all_channels(self, user_id):
        sessions = await get_all_active_sessions()
        if not sessions: return 0
        count = 0
        for sess in sessions:
            client = await self.get_client(sess[3])
            if not client: continue
            try:
                async for dialog in client.iter_dialogs():
                    if dialog.is_channel or dialog.is_group:
                        await client(LeaveChannelRequest(dialog.entity))
                        count += 1
                        await asyncio.sleep(0.5)
            except: continue
        return count

    async def keep_online_loop(self):
        while True:
            try:
                is_online = await get_setting("is_global_online", "False") == "True"
                if is_online:
                    sessions = await get_all_active_sessions()
                    for sess in sessions:
                        client = await self.get_client(sess[3])
                        if client and client.is_connected():
                            try: await client(UpdateStatusRequest(offline=False))
                            except: pass
                
                now = datetime.now()
                if now.hour == 2 and now.minute == 0:
                    async with aiosqlite.connect(DATABASE_NAME) as db:
                        async with db.execute("SELECT DISTINCT owner_id FROM user_sessions") as cursor:
                            users = await cursor.fetchall()
                            for (u_id,) in users: await self.leave_all_channels(u_id)
                    await asyncio.sleep(60)
            except Exception as e: logging.error(f"Error in background loop: {e}")
            await asyncio.sleep(30)

session_mgr = SessionManager()

# --- Improved Reconnection for Bot Client ---
bot = TelegramClient(os.path.join(DATA_DIR, 'bot_session'), API_ID, API_HASH, 
                     connection_retries=None, # Infinite retries
                     auto_reconnect=True)

MAIN_MENU_TEXT = """
📈 **Owner** - @{owner_username}
👤 **Your Logged Id** - {user_logged_id}
🌍 **Total Logged Id** - {total_logged_id}
⚙️ **Status**: {status}
🆔 **Your ID**: `{user_id}`
"""

def get_main_buttons():
    return [
        [Button.inline("🫂 Login New Id", data="login_new"), Button.inline("🔄 Refresh", data="refresh")],
        [Button.inline("📝 Req Link", data="req_link"), Button.inline("📤 Leave Link", data="leave_link")],
        [Button.inline("🟢 Set Online", data="set_online"), Button.inline("⚫ Set Offline", data="set_offline")],
        [Button.inline("👁️ Send Views", data="send_views")]
    ]

user_states = {}

async def ensure_bot_connected():
    """Ensure bot is connected before any operation."""
    if not bot.is_connected():
        try:
            await bot.connect()
            logging.info("Bot reconnected successfully.")
        except Exception as e:
            logging.error(f"Failed to reconnect bot: {e}")
            return False
    return True

@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    if not await ensure_bot_connected(): return
    user_id = event.sender_id
    if not await is_authorized(user_id):
        await event.respond(f"❌ No access! ID: `{user_id}`")
        return
    user_count = await count_user_sessions(user_id)
    total_count = await count_all_sessions()
    me = await bot.get_me()
    is_online = await get_setting("is_global_online", "False") == "True"
    status = "🟢 Online" if is_online else "⚫ Offline"
    caption = MAIN_MENU_TEXT.format(owner_username=me.username, user_logged_id=user_count, total_logged_id=total_count, status=status, user_id=user_id)
    try:
        if os.path.exists(MENU_IMAGE_PATH): await bot.send_file(event.chat_id, MENU_IMAGE_PATH, caption=caption, buttons=get_main_buttons())
        else: await event.respond(caption, buttons=get_main_buttons())
    except Exception as e:
        logging.error(f"Start handler error: {e}")
        await event.respond(caption, buttons=get_main_buttons())

@bot.on(events.NewMessage)
async def message_handler(event):
    if not await ensure_bot_connected(): return
    user_id = event.sender_id
    if await is_authorized(user_id) and event.photo:
        await event.respond("⏳ Updating menu image...")
        if os.path.exists(MENU_IMAGE_PATH): os.remove(MENU_IMAGE_PATH)
        await event.download_media(file=MENU_IMAGE_PATH)
        await set_setting("menu_image", MENU_IMAGE_PATH)
        await event.respond("✅ Updated!")
        return

    if not event.text or event.text.startswith('/') or not await is_authorized(user_id): return
    state = user_states.get(user_id)
    if not state: return

    if state == "waiting_phone":
        phone = event.text.strip()
        user_states[user_id] = {"state": "waiting_otp", "phone": phone}
        res = await session_mgr.start_login(user_id, phone)
        if res == "otp_required": await event.respond("📩 Send OTP:")
        else: await event.respond(f"❌ {res}"); user_states.pop(user_id)
    
    elif isinstance(state, dict) and state.get("state") == "waiting_otp":
        res = await session_mgr.complete_login(user_id, event.text.strip())
        if res == "success": await event.respond("✅ Success!"); user_states.pop(user_id)
        elif res == "password_required": await event.respond("🔐 Send Password:")
        else: await event.respond(f"❌ {res}"); user_states.pop(user_id)

    elif state == "waiting_link":
        user_states[user_id] = {"state": "waiting_quantity", "link": event.text.strip()}
        await event.respond("🔢 Quantity:")

    elif isinstance(state, dict) and state.get("state") == "waiting_quantity":
        try:
            user_states[user_id]["quantity"] = int(event.text.strip())
            user_states[user_id]["state"] = "waiting_delay"
            await event.respond("⏱️ Delay:")
        except: await event.respond("❌ Invalid!")

    elif isinstance(state, dict) and state.get("state") == "waiting_delay" and "link" in state:
        try:
            delay = float(event.text.strip())
            link, qty = state["link"], state["quantity"]
            progress_msg = await event.respond(f"🚀 Starting {qty} IDs...", buttons=[Button.inline("⏹️ Stop", data="stop_task")])
            sessions = await get_all_active_sessions()
            
            sessions = list(sessions)
            random.shuffle(sessions)
            
            session_mgr.stop_tasks[user_id] = False
            joined = 0
            failed_reasons = []
            for i, sess in enumerate(sessions[:qty]):
                if session_mgr.stop_tasks.get(user_id, False): break
                try:
                    await progress_msg.edit(f"⏳ ID {i+1}/{qty}: Processing...", buttons=[Button.inline("⏹️ Stop", data="stop_task")])
                except: pass
                success, reason = await session_mgr.join_group(sess[3], link)
                if success: joined += 1
                else: failed_reasons.append(reason)
                await asyncio.sleep(delay)
            
            final_msg = f"✅ Done! Joined: {joined}/{qty}\n"
            if joined == 0 and failed_reasons:
                unique_reasons = list(set(failed_reasons))
                final_msg += f"❌ Errors: {', '.join(unique_reasons)}\n⚠️ Please login again using 'Login New Id' if files are missing."
            await event.respond(final_msg)
        except Exception as e:
            logging.error(f"Join task error: {e}")
            await event.respond("❌ Error!")
        user_states.pop(user_id)

    elif state == "waiting_post_link":
        user_states[user_id] = {"state": "waiting_views_delay", "post_link": event.text.strip()}
        await event.respond("⏱️ Delay:")

    elif isinstance(state, dict) and state.get("state") == "waiting_views_delay":
        try:
            delay = float(event.text.strip())
            post_link = state["post_link"]
            progress_msg = await event.respond(f"⏳ Sending views...", buttons=[Button.inline("⏹️ Stop", data="stop_task")])
            _, msg = await session_mgr.send_views_to_post(user_id, post_link, delay, progress_msg)
            await event.respond(msg)
        except Exception as e:
            logging.error(f"Views task error: {e}")
            await event.respond("❌ Error!")
        user_states.pop(user_id)

@bot.on(events.CallbackQuery())
async def callback_handler(event):
    if not await ensure_bot_connected(): return
    user_id = event.sender_id
    if not await is_authorized(user_id): return
    data = event.data.decode()
    if data == "refresh":
        user_count = await count_user_sessions(user_id)
        total_count = await count_all_sessions()
        me = await bot.get_me()
        is_online = await get_setting("is_global_online", "False") == "True"
        status = "🟢 Online" if is_online else "⚫ Offline"
        try: await event.edit(MAIN_MENU_TEXT.format(owner_username=me.username, user_logged_id=user_count, total_logged_id=total_count, status=status, user_id=user_id), buttons=get_main_buttons())
        except: pass
    elif data == "login_new":
        user_states[user_id] = "waiting_phone"
        await event.respond("📱 Phone number:")
    elif data == "req_link":
        user_states[user_id] = "waiting_link"
        await event.respond("🔗 Link:")
    elif data == "send_views":
        user_states[user_id] = "waiting_post_link"
        await event.respond("🔗 Post Link:")
    elif data == "set_online":
        await set_setting("is_global_online", "True")
        await event.answer("🟢 Global Online Enabled!")
    elif data == "set_offline":
        await set_setting("is_global_online", "False")
        await event.answer("⚫ Global Offline Enabled.")
    elif data == "stop_task":
        session_mgr.stop_tasks[user_id] = True
        await event.answer("⏹️ Stopping...")

@bot.on(events.NewMessage(pattern='/add'))
async def add_user_handler(event):
    if not await ensure_bot_connected(): return
    # --- Restricted to Admin Only ---
    if event.sender_id != ADMIN_ID:
        await event.respond("❌ Only Admin can use this command!")
        return
    # --------------------------------
    try:
        parts = event.text.split()
        expiry = await add_subscription(int(parts[1]), int(parts[2]))
        await event.respond(f"✅ User `{parts[1]}` added! Expiry: {expiry}")
    except: await event.respond("❌ `/add <id> <days>`")

app = Flask('')
@app.route('/')
def home(): return "Running!"

async def main():
    await init_db()
    try:
        await bot.start(bot_token=BOT_TOKEN)
        print("Bot Started!")
        Thread(target=lambda: app.run(host='0.0.0.0', port=8080)).start()
        asyncio.create_task(session_mgr.keep_online_loop())
        await bot.run_until_disconnected()
    except Exception as e:
        logging.error(f"Main loop error: {e}")
        await asyncio.sleep(5)
        await main() 

if __name__ == '__main__':
    asyncio.run(main())
