import os
import logging
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message
import aiosqlite
from instagrapi import Client
from instagrapi.exceptions import ClientError, ChallengeRequired
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ==========================================
# CONFIGURATION & LOGGING
# ==========================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing in .env file.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

DB_NAME = "instagram_bot.db"
SESSION_DIR = "sessions"
MEDIA_DIR = "media"

os.makedirs(SESSION_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

# ==========================================
# DATABASE SETUP
# ==========================================
async def init_db() -> None:
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    sessionid TEXT NOT NULL,
                    session_file TEXT,
                    fixed_caption TEXT,
                    fixed_poster TEXT,
                    target_list TEXT
                )
            """)
            await db.commit()
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise

# ==========================================
# FSM STATES
# ==========================================
class BotStates(StatesGroup):
    # Account Management
    WaitingForUsername = State()
    WaitingForSessionID = State()
    
    # Account Actions
    WaitingForBio = State()
    WaitingForTarget = State()
    WaitingForFixedCaption = State()
    WaitingForFixedPoster = State()
    
    # Post Creation
    WaitingForVideo = State()
    WaitingForScheduleTime = State()

# ==========================================
# INSTAGRAM CLIENT HELPER (ASYNC WRAPPER)
# ==========================================
def instagram_login(username: str, sessionid: str, session_file: str) -> Client:
    cl = Client()
    cl.delay_range = [1, 3] # Human-like delay
    
    try:
        # 1. Check if session file exists first (Uploaded via Termius)
        if os.path.exists(session_file):
            logger.info(f"Session file found for {username}. Loading settings...")
            cl.load_settings(session_file)
            
            try:
                cl.get_timeline_feed()
                logger.info("✅ Login verified via existing session file!")
                return cl
            except Exception as e:
                logger.warning(f"Session file invalid, checking sessionid... : {e}")
        
        # 2. Fallback to sessionid if no file found and sessionid is not dummy
        if sessionid and sessionid != "12345":
            logger.info(f"Trying to login via sessionid for {username}")
            cl.login_by_sessionid(sessionid)
            cl.dump_settings(session_file)
            return cl
        else:
            raise ValueError("Session file nahi mili! Kripya PC se json file upload karein.")
            
    except ChallengeRequired as e:
        logger.error(f"Challenge Required for {username}: {e}")
        raise ValueError(
            "⚠️ **Security Challenge:** Instagram ne block kar diya hai.\n\n"
            "**Fix:** Apne PC par login karke session.json banayein aur VPS ke 'sessions' folder mein upload karein."
        )
    except Exception as e:
        logger.error(f"Instagrapi Login Error for {username}: {e}")
        raise ValueError(f"❌ Login Error: {e}")

async def get_insta_client(username: str, sessionid: str, session_file: str) -> Client:
    return await asyncio.to_thread(instagram_login, username, sessionid, session_file)

# ==========================================
# KEYBOARDS
# ==========================================
def main_menu_keyboard(accounts: list) -> InlineKeyboardMarkup:
    buttons = []
    for acc in accounts:
        buttons.append([InlineKeyboardButton(text=f"👤 {acc[1]}", callback_data=f"acc_{acc[0]}")])
    buttons.append([InlineKeyboardButton(text="➕ Add New Account", callback_data="add_account")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def account_actions_keyboard(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Edit Bio", callback_data=f"bio_{account_id}"),
         InlineKeyboardButton(text="🎯 Manage Targets", callback_data=f"target_{account_id}")],
        [InlineKeyboardButton(text="⚙️ Set Fixed Caption", callback_data=f"setcap_{account_id}"),
         InlineKeyboardButton(text="🖼️ Set Fixed Poster", callback_data=f"setposter_{account_id}")],
        [InlineKeyboardButton(text="📤 Create Post (Reel)", callback_data=f"post_{account_id}"),
         InlineKeyboardButton(text="📅 Schedule Post", callback_data=f"schedule_{account_id}")],
        [InlineKeyboardButton(text="🔙 Back to Main Menu", callback_data="back_main")]
    ])

# ==========================================
# HANDLERS: START & ACCOUNT LIST
# ==========================================
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT id, username FROM accounts") as cursor:
                accounts = await cursor.fetchall()
        
        await message.answer(
            "👋 Welcome to the Advanced Multi-Account Instagram Manager!\n"
            "Select an account or add a new one:",
            reply_markup=main_menu_keyboard(accounts)
        )
    except Exception as e:
        logger.error(f"Error in start command: {e}")
        await message.answer(f"❌ System Error: {e}")

@dp.callback_query(F.data == "back_main")
async def back_to_main(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT id, username FROM accounts") as cursor:
                accounts = await cursor.fetchall()
        await call.message.edit_text(
            "Select an account or add a new one:",
            reply_markup=main_menu_keyboard(accounts)
        )
    except Exception as e:
        logger.error(f"Error returning to main: {e}")
        await call.message.answer(f"❌ System Error: {e}")

# ==========================================
# HANDLERS: ADD ACCOUNT
# ==========================================
@dp.callback_query(F.data == "add_account")
async def add_account_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.answer("Enter Instagram Username:")
    await state.set_state(BotStates.WaitingForUsername)
    await call.answer()

@dp.message(BotStates.WaitingForUsername)
async def add_account_username(message: Message, state: FSMContext) -> None:
    await state.update_data(username=message.text.strip())
    await message.answer("Enter Instagram Session ID (cookie):\n*(Agar aapne session file VPS par daal di hai, toh yahan sirf 12345 likh kar bhej dein)*")
    await state.set_state(BotStates.WaitingForSessionID)

@dp.message(BotStates.WaitingForSessionID)
async def add_account_sessionid(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    username = data['username']
    sessionid = message.text.strip()
    session_file = os.path.join(SESSION_DIR, f"{username}_session.json")
    
    msg = await message.answer("⏳ Validating session ID and saving...")
    
    try:
        # Test login & generate session
        await get_insta_client(username, sessionid, session_file)
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT INTO accounts (username, sessionid, session_file) VALUES (?, ?, ?)",
                (username, sessionid, session_file)
            )
            await db.commit()
            
        await msg.edit_text(f"✅ Account @{username} successfully linked!")
        await state.clear()
        
    except ValueError as ve:
        # This catches our custom formatted error messages
        await msg.edit_text(str(ve))
        await state.clear()
    except Exception as e:
        logger.error(f"Login failed for {username}: {e}")
        await msg.edit_text(f"❌ Failed to login.\nError: {str(e)}")
        await state.clear()

# ==========================================
# HANDLERS: ACCOUNT MENU
# ==========================================
@dp.callback_query(F.data.startswith("acc_"))
async def account_dashboard(call: CallbackQuery, state: FSMContext) -> None:
    account_id = int(call.data.split("_")[1])
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT username FROM accounts WHERE id = ?", (account_id,)) as cursor:
                row = await cursor.fetchone()
                
        if not row:
            await call.answer("Account not found!", show_alert=True)
            return

        username = row[0]
        await state.update_data(current_account_id=account_id)
        
        await call.message.edit_text(
            f"🛠️ **Dashboard for @{username}**\n\nWhat would you like to do?",
            parse_mode="Markdown",
            reply_markup=account_actions_keyboard(account_id)
        )
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        await call.message.answer(f"❌ Error: {e}")

# ==========================================
# HANDLERS: EDIT BIO
# ==========================================
@dp.callback_query(F.data.startswith("bio_"))
async def edit_bio_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.answer("Send the new bio text for this account:")
    await state.set_state(BotStates.WaitingForBio)
    await call.answer()

@dp.message(BotStates.WaitingForBio)
async def edit_bio_process(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    account_id = data.get("current_account_id")
    new_bio = message.text

    msg = await message.answer("⏳ Updating bio...")
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT username, sessionid, session_file FROM accounts WHERE id = ?", (account_id,)) as cursor:
                acc = await cursor.fetchone()
        
        cl = await get_insta_client(acc[0], acc[1], acc[2])
        await asyncio.to_thread(cl.account_edit, biography=new_bio)
        
        await msg.edit_text("✅ Bio successfully updated!")
    except ValueError as ve:
        await msg.edit_text(str(ve))
    except Exception as e:
        logger.error(f"Bio update failed: {e}")
        await msg.edit_text(f"❌ Failed to update bio: {e}")
    finally:
        await state.clear()

# ==========================================
# HANDLERS: SETTINGS (CAPTION & POSTER)
# ==========================================
@dp.callback_query(F.data.startswith("setcap_"))
async def set_caption_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.answer("Send the fixed caption that will be used for all future reels on this account:")
    await state.set_state(BotStates.WaitingForFixedCaption)
    await call.answer()

@dp.message(BotStates.WaitingForFixedCaption)
async def set_caption_process(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    account_id = data.get("current_account_id")
    
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE accounts SET fixed_caption = ? WHERE id = ?", (message.text, account_id))
            await db.commit()
        await message.answer("✅ Fixed caption updated successfully!")
    except Exception as e:
        logger.error(f"Setting caption failed: {e}")
        await message.answer(f"❌ Error setting caption: {e}")
    finally:
        await state.clear()

@dp.callback_query(F.data.startswith("setposter_"))
async def set_poster_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.answer("Send the image (as Photo) that will be used as the fixed poster/cover for reels:")
    await state.set_state(BotStates.WaitingForFixedPoster)
    await call.answer()

@dp.message(BotStates.WaitingForFixedPoster, F.photo)
async def set_poster_process(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    account_id = data.get("current_account_id")
    
    photo = message.photo[-1]
    file_path = os.path.join(MEDIA_DIR, f"poster_{account_id}.jpg")
    
    try:
        await bot.download(photo, destination=file_path)
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE accounts SET fixed_poster = ? WHERE id = ?", (file_path, account_id))
            await db.commit()
        await message.answer("✅ Fixed poster saved successfully!")
    except Exception as e:
        logger.error(f"Setting poster failed: {e}")
        await message.answer(f"❌ Error saving poster: {e}")
    finally:
        await state.clear()

# ==========================================
# HANDLERS: TARGET MANAGEMENT
# ==========================================
@dp.callback_query(F.data.startswith("target_"))
async def target_start(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.answer("Send comma-separated target usernames (e.g., target1, target2):")
    await state.set_state(BotStates.WaitingForTarget)
    await call.answer()

@dp.message(BotStates.WaitingForTarget)
async def target_process(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    account_id = data.get("current_account_id")
    targets = message.text.strip()
    
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE accounts SET target_list = ? WHERE id = ?", (targets, account_id))
            await db.commit()
        await message.answer(f"✅ Targets updated: {targets}")
    except Exception as e:
        logger.error(f"Target update failed: {e}")
        await message.answer(f"❌ Error updating targets: {e}")
    finally:
        await state.clear()

# ==========================================
# CORE LOGIC: POSTING REEL
# ==========================================
async def upload_reel_task(user_id: int, account_id: int, video_path: str):
    try:
        # Notify user task started
        await bot.send_message(user_id, f"⏳ Starting Reel upload process for account ID: {account_id}...")
        
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT username, sessionid, session_file, fixed_caption, fixed_poster FROM accounts WHERE id = ?", (account_id,)) as cursor:
                acc = await cursor.fetchone()
        
        username, sessionid, session_file, fixed_caption, fixed_poster = acc
        caption = fixed_caption if fixed_caption else ""
        
        cl = await get_insta_client(username, sessionid, session_file)
        
        # Upload process
        await asyncio.to_thread(
            cl.clip_upload,
            path=video_path,
            caption=caption,
            thumbnail=fixed_poster if fixed_poster and os.path.exists(fixed_poster) else None
        )
        
        await bot.send_message(user_id, f"✅ Reel uploaded successfully to @{username}!")
    except ValueError as ve:
        await bot.send_message(user_id, str(ve))
    except Exception as e:
        logger.error(f"Reel upload failed for account {account_id}: {e}")
        await bot.send_message(user_id, f"❌ Reel upload failed for @{username}:\n{e}")
    finally:
        # Cleanup video file
        if os.path.exists(video_path):
            os.remove(video_path)

# ==========================================
# HANDLERS: CREATE / SCHEDULE POST
# ==========================================
@dp.callback_query(F.data.startswith("post_") | F.data.startswith("schedule_"))
async def create_post_start(call: CallbackQuery, state: FSMContext) -> None:
    action, account_id = call.data.split("_")
    is_schedule = (action == "schedule")
    
    await state.update_data(current_account_id=account_id, is_schedule=is_schedule)
    await call.message.answer("📹 Send the Video (.mp4) for the Reel:")
    await state.set_state(BotStates.WaitingForVideo)
    await call.answer()

@dp.message(BotStates.WaitingForVideo, F.video)
async def process_video(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    is_schedule = data.get("is_schedule")
    
    video = message.video
    video_path = os.path.join(MEDIA_DIR, f"video_{message.message_id}.mp4")
    
    msg = await message.answer("⏳ Downloading video...")
    try:
        await bot.download(video, destination=video_path)
        await state.update_data(saved_video_path=video_path)
        
        if is_schedule:
            await msg.edit_text("🕒 Send the schedule time in seconds from now (e.g., 3600 for 1 hour):")
            await state.set_state(BotStates.WaitingForScheduleTime)
        else:
            await msg.edit_text("🚀 Uploading Reel to Instagram now...")
            account_id = data.get("current_account_id")
            
            # Fire and forget the upload task
            asyncio.create_task(upload_reel_task(message.from_user.id, account_id, video_path))
            await state.clear()
            
    except Exception as e:
        logger.error(f"Video download failed: {e}")
        await msg.edit_text(f"❌ Error processing video: {e}")
        await state.clear()

@dp.message(BotStates.WaitingForScheduleTime)
async def process_schedule(message: Message, state: FSMContext) -> None:
    try:
        delay_seconds = int(message.text.strip())
        data = await state.get_data()
        account_id = data.get("current_account_id")
        video_path = data.get("saved_video_path")
        
        run_date = datetime.now()
        
        import datetime as dt
        schedule_time = datetime.now() + dt.timedelta(seconds=delay_seconds)

        scheduler.add_job(
            upload_reel_task,
            'date',
            run_date=schedule_time,
            args=[message.from_user.id, account_id, video_path]
        )
        
        await message.answer(f"✅ Reel scheduled to be posted in {delay_seconds} seconds!")
    except ValueError:
        await message.answer("❌ Invalid input. Please enter numbers only (seconds).")
    except Exception as e:
        logger.error(f"Scheduling failed: {e}")
        await message.answer(f"❌ Error scheduling: {e}")
    finally:
        await state.clear()

# ==========================================
# MAIN EXECUTION
# ==========================================
async def main() -> None:
    try:
        await init_db()
        scheduler.start()
        logger.info("Bot is starting...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Fatal error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
