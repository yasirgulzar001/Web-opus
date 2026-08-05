#!/usr/bin/env python3
"""
Telegram Bot for Syntx.ai Claude Chat with Admin Control + Referral System
Owner: @NEVER_DIE8
1 referral = 30 min premium for referrer, 30 min trial for new user
"""

import asyncio
import logging
import re
import os
import sys
import time
import tempfile
import sqlite3
import random
import string
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import cloudscraper
import requests
from telegram import Update, constants
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
BOT_TOKEN = "8799719369:AAGvETel87nRB6hqNPyUWMc"  # Hardcoded as requested
ADMIN_IDS = {6535041385}                     # Admin Telegram user ID(s)
BOT_USERNAME = "PROBIxAichatbot"              # ⚠️ Replace with your actual bot username (without @)

REFERRER_REWARD_MINUTES = 30      # what the referrer earns per referral
REFERREE_TRIAL_MINUTES = 30       # what the new user gets as trial

EMAIL_API = "https://zecora0.serv00.net/Gmail.php"
SYNTX_AUTH_SEND_OTP = "https://api.syntx.ai/api/v1/auth/email/send-otp"
SYNTX_AUTH_VERIFY_OTP = "https://api.syntx.ai/api/v1/auth/email/verify-otp"
SYNTX_CHATS = "https://api.syntx.ai/api/v1/chats"
SYNTX_UPLOAD = "https://api.syntx.ai/api/v1/chats/upload-files"

HTTP_TIMEOUT = 15
OTP_TIMEOUT = 120
REPLY_TIMEOUT = 600  # 10 minutes for long responses
POLL_INTERVAL = 2
MAX_MESSAGES = 0

DB_PATH = "referral_claims.db"  # SQLite database file for permanent referral tracking

MODELS = [
    ("claude-sonnet-5", "Claude Sonnet 5"),
    ("claude-opus-4-8", "Claude 4.8 Opus"),
    ("claude-opus-4-7", "Claude 4.7 Opus"),
    ("claude-sonnet-4-6", "Claude 4.6 Sonnet"),
    ("claude-opus-4-6", "Claude 4.6 Opus"),
    ("claude-sonnet-4-5-20250929", "Claude 4.5 Sonnet"),
    ("claude-opus-4-5-20251101", "Claude 4.5 Opus"),
    ("claude-opus-4-1-20250805", "Claude 4.1 Opus"),
    ("claude-opus-4-20250514", "Claude Opus 4"),
    ("claude-sonnet-4-20250514", "Claude 4 Sonnet"),
]

# Mapping code block languages to file extensions
LANG_EXTENSIONS = {
    "python": "py", "py": "py", "python3": "py",
    "javascript": "js", "js": "js", "node": "js",
    "html": "html", "xml": "xml",
    "css": "css", "scss": "scss",
    "json": "json",
    "bash": "sh", "shell": "sh", "sh": "sh", "zsh": "sh",
    "c": "c", "cpp": "cpp", "c++": "cpp", "objective-c": "m",
    "java": "java", "kotlin": "kt",
    "ruby": "rb", "rb": "rb",
    "go": "go", "golang": "go",
    "php": "php",
    "sql": "sql",
    "yaml": "yml", "yml": "yml",
    "typescript": "ts", "ts": "ts",
    "rust": "rs", "rs": "rs",
    "swift": "swift",
    "perl": "pl", "pl": "pl",
    "r": "r",
    "dart": "dart",
    "lua": "lua",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Global state
# ------------------------------------------------------------
ALLOWED_USERS: Dict[int, Optional[datetime]] = {uid: None for uid in ADMIN_IDS}
USER_SESSIONS: Dict[int, Dict[str, Any]] = {}
REFERRAL_COUNT: Dict[int, int] = {}
ALLAC_USERS: set = set()  # track users granted unlimited by /allac

# Initialize scraper globally
scraper = cloudscraper.create_scraper()

UNAUTHORIZED_MSG = (
    "⛔ <b>Access Disabled</b>\n\n"
    "Buy Premium: <a href='https://t.me/NEVER_DIE8'>@NEVER_DIE8</a>\n"
    "/referral – Get your referral link &amp; stats"
)

# ------------------------------------------------------------
# Database Functions (SQLite for permanent referral tracking & redeem codes)
# ------------------------------------------------------------
def init_db():
    """Initialize the SQLite database with tables for referrals and redeem codes."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Referral claims table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS referral_claims (
                user_id INTEGER PRIMARY KEY,
                referrer_id INTEGER,
                claim_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Redeem codes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS redeem_codes (
                code TEXT PRIMARY KEY,
                days INTEGER NOT NULL,
                is_used INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")

def claim_referral(user_id: int, referrer_id: int) -> bool:
    """
    Atomically tries to insert a referral claim.
    Returns True if successful, False if the user has already claimed.
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        # INSERT OR IGNORE prevents errors if the PRIMARY KEY (user_id) already exists
        cursor.execute(
            "INSERT OR IGNORE INTO referral_claims (user_id, referrer_id) VALUES (?, ?)", 
            (user_id, referrer_id)
        )
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success
    except sqlite3.Error as e:
        logger.error(f"Database error in claim_referral for user {user_id}: {e}")
        return False

def generate_redeem_codes(amount: int, days: int) -> list:
    """Generate and store `amount` unique redeem codes each giving `days` premium days."""
    codes = []
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    for _ in range(amount):
        # Generate a unique 10-char alphanumeric code
        while True:
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
            cursor.execute("SELECT code FROM redeem_codes WHERE code=?", (code,))
            if not cursor.fetchone():
                break
        cursor.execute("INSERT INTO redeem_codes (code, days) VALUES (?, ?)", (code, days))
        codes.append(code)
    conn.commit()
    conn.close()
    return codes

def redeem_code(code: str) -> Optional[int]:
    """
    Redeem a code: mark it as used and return the number of days.
    Returns None if the code is invalid or already used.
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT days, is_used FROM redeem_codes WHERE code=?", (code,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return None
        days, is_used = row
        if is_used:
            conn.close()
            return None
        # Mark as used
        cursor.execute("UPDATE redeem_codes SET is_used=1 WHERE code=?", (code,))
        conn.commit()
        conn.close()
        return days
    except sqlite3.Error as e:
        logger.error(f"Database error in redeem_code: {e}")
        return None

# ------------------------------------------------------------
# Helper: add or extend premium for a user
# ------------------------------------------------------------
def add_or_extend_premium(user_id: int, minutes: int):
    """Give a user `minutes` of premium, adding them if new or extending."""
    now = datetime.now()
    if user_id not in ALLOWED_USERS:
        ALLOWED_USERS[user_id] = now + timedelta(minutes=minutes)
    else:
        current = ALLOWED_USERS[user_id]
        if current is None:
            return  # Permanent – no need to extend
        new_expiry = max(now, current) + timedelta(minutes=minutes)
        ALLOWED_USERS[user_id] = new_expiry

def is_allowed(user_id: int) -> bool:
    """Return True if user is explicitly allowed and not expired."""
    if user_id not in ALLOWED_USERS:
        return False
    expiry = ALLOWED_USERS[user_id]
    if expiry is None:
        return True
    if expiry > datetime.now():
        return True
    
    # Expired - clean up state
    ALLOWED_USERS.pop(user_id, None)
    USER_SESSIONS.pop(user_id, None)
    return False

async def send_long_message(update: Update, text: str, parse_mode: str = None):
    """Sends long messages, falling back to plain text if Markdown parsing fails."""
    max_len = 4000
    for i in range(0, len(text), max_len):
        chunk = text[i:i+max_len]
        try:
            await update.message.reply_text(chunk, parse_mode=parse_mode)
        except Exception:
            # Fallback to plain text if markdown is invalid
            try:
                await update.message.reply_text(chunk)
            except Exception as e:
                logger.error(f"Failed to send message chunk: {e}")

def get_extension(lang: str) -> str:
    """Map a language string from markdown to a file extension."""
    lang = lang.lower().strip()
    return LANG_EXTENSIONS.get(lang, "txt")

async def send_response_with_code_files(update: Update, text: str, model_name: str):
    """Extracts code blocks from the response, sends them as files, and sends the rest as text."""
    code_blocks = []
    
    def replacer(match):
        lang = match.group(1) or "txt"
        code = match.group(2)
        ext = get_extension(lang)
        code_blocks.append((ext, code))
        return "📁 _Code block extracted and sent as a file below._"
    
    # Regex to match ```lang\n ... ```
    cleaned_text = re.sub(r"```([a-zA-Z0-9_+-]*)\n?(.*?)```", replacer, text, flags=re.DOTALL)
    
    # Prepend model name
    final_text = f"**{model_name}:** {cleaned_text}".strip()
    
    # Send the text part
    await send_long_message(update, final_text, parse_mode=constants.ParseMode.MARKDOWN)
    
    # Send the code files
    for i, (ext, code) in enumerate(code_blocks, 1):
        tmp_path = None
        try:
            # Create temp file to hold the code
            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}", mode="w", encoding="utf-8") as tmp:
                tmp.write(code)
                tmp_path = tmp.name
            
            filename = f"code_{i}.{ext}" if len(code_blocks) > 1 else f"code.{ext}"
            
            with open(tmp_path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=filename,
                    caption=f"📂 Extracted code snippet ({ext})"
                )
        except Exception as e:
            logger.error(f"Failed to send code file: {e}")
            # Fallback: send code as text if file sending fails
            await send_long_message(update, f"```\n{code}\n```", parse_mode=constants.ParseMode.MARKDOWN)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

async def animate_thinking(bot, chat_id: int, message_id: int):
    """Continuously edits a message to show a waving thinking animation."""
    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    i = 0
    while True:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"🧠 {frames[i % len(frames)]} Thinking..."
            )
        except Exception:
            # Ignore "message not modified" errors to prevent crashes
            pass
        i += 1
        await asyncio.sleep(1.5)

# ------------------------------------------------------------
# Email & OTP
# ------------------------------------------------------------
async def create_email() -> tuple[str, str]:
    def _sync():
        resp = scraper.get(f"{EMAIL_API}?action=create", timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            raise Exception(f"Status {resp.status_code}")
        data = resp.json()
        if "error" in data or not data.get("email"):
            raise Exception("Invalid response")
        return data["email"], data["id"]
    
    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        logger.error(f"Email creation failed: {e}")
        raise

async def fetch_otp(email: str, mailbox_id: str) -> Optional[str]:
    start = time.time()
    last_msg_id = None
    
    while time.time() - start < OTP_TIMEOUT:
        def _sync():
            return scraper.get(
                f"{EMAIL_API}?action=get_messages&mailbox_id={mailbox_id}&email={email}",
                timeout=HTTP_TIMEOUT,
            )
        
        try:
            resp = await asyncio.to_thread(_sync)
            if resp.status_code == 200:
                messages = resp.json()
                if messages and isinstance(messages, list):
                    first = messages[0]
                    msg_id = first.get("id")
                    if msg_id != last_msg_id:
                        last_msg_id = msg_id
                        body = first.get("html", "") or first.get("text", "") or first.get("body", "")
                        match = re.search(r"\b(\d{6})\b", body)
                        if match:
                            return match.group(1)
        except Exception as e:
            logger.warning(f"OTP poll error: {e}")
            
        await asyncio.sleep(POLL_INTERVAL)
    return None

# ------------------------------------------------------------
# Syntx.ai API
# ------------------------------------------------------------
async def send_otp(email: str) -> bool:
    def _sync():
        resp = scraper.post(
            SYNTX_AUTH_SEND_OTP,
            json={"email": email, "ref_uuid": None, "utm": ""},
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36"},
            timeout=HTTP_TIMEOUT,
        )
        return resp.status_code == 200 and resp.json().get("success")
    
    try:
        return await asyncio.to_thread(_sync)
    except Exception:
        return False

async def verify_otp(email: str, otp: str) -> Optional[str]:
    def _sync():
        resp = scraper.post(
            SYNTX_AUTH_VERIFY_OTP,
            json={"email": email, "otp_code": otp, "ref_uuid": None, "utm": ""},
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200 and resp.json().get("success"):
            return resp.json().get("token")
        return None
    
    try:
        return await asyncio.to_thread(_sync)
    except Exception:
        return None

async def create_chat(token: str) -> Optional[str]:
    def _sync():
        resp = scraper.post(
            SYNTX_CHATS,
            json={"title": "Claude Chat", "scope": "text"},
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36",
            },
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 201:
            return resp.json().get("uuid")
        return None
    
    try:
        return await asyncio.to_thread(_sync)
    except Exception:
        return None

async def upload_image(token: str, chat_uuid: str, file_path: str) -> Optional[str]:
    def _sync():
        with open(file_path, "rb") as f:
            files = {"files": (os.path.basename(file_path), f, "application/octet-stream")}
            data = {"check_duplicates": "true", "chat_uuid": chat_uuid}
            resp = requests.post(
                SYNTX_UPLOAD,
                data=data,
                files=files,
                headers={"Authorization": f"Bearer {token}"},
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code == 200 and resp.json().get("successful", 0) > 0:
                return resp.json()["files"][0]["url"]
            return None
    
    try:
        return await asyncio.to_thread(_sync)
    except Exception as e:
        logger.error(f"Image upload failed: {e}")
        return None

async def send_message(token: str, chat_uuid: str, objects: list) -> Optional[int]:
    def _sync():
        resp = scraper.post(
            f"{SYNTX_CHATS}/{chat_uuid}/messages?ai_name=claude",
            json={"objects": objects},
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36",
            },
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json().get("id")
        return None
    
    try:
        return await asyncio.to_thread(_sync)
    except Exception:
        return None

async def fetch_reply(token: str, chat_uuid: str, after_id: int, timeout: int = REPLY_TIMEOUT) -> Optional[str]:
    start = time.time()
    while time.time() - start < timeout:
        def _sync():
            return scraper.get(
                f"{SYNTX_CHATS}/{chat_uuid}/messages?page_size=20",
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36",
                },
                timeout=HTTP_TIMEOUT,
            )
        
        try:
            resp = await asyncio.to_thread(_sync)
            if resp.status_code == 200:
                for msg in resp.json().get("messages", []):
                    if msg.get("author_id") == -1 and msg.get("id", 0) > after_id:
                        msg_objs = msg.get("message_object", [])
                        if msg_objs and isinstance(msg_objs, list):
                            obj = msg_objs[0]
                            if obj.get("object_type") == "text" and obj.get("completed"):
                                return obj.get("object_text")
        except Exception as e:
            logger.warning(f"Fetch reply poll error: {e}")
            
        await asyncio.sleep(POLL_INTERVAL)
    return None

# ------------------------------------------------------------
# Session management
# ------------------------------------------------------------
async def new_session(chat_id: int) -> Optional[str]:
    try:
        email, mailbox_id = await create_email()
        if not await send_otp(email):
            return "❌ Failed to send OTP."
        
        otp = await fetch_otp(email, mailbox_id)
        if not otp:
            return "❌ OTP not received in time."
            
        token = await verify_otp(email, otp)
        if not token:
            return "❌ OTP verification failed."
            
        chat_uuid = await create_chat(token)
        if not chat_uuid:
            return "❌ Failed to create chat."
            
        USER_SESSIONS[chat_id] = {
            "email": email,
            "token": token,
            "chat_uuid": chat_uuid,
            "model_id": "claude-sonnet-5",
            "model_name": "Claude Sonnet 5",
            "message_count": 0,
            "active": True,
        }
        return None
    except Exception as e:
        logger.error(f"new_session error: {e}", exc_info=True)
        return f"❌ Internal error: {e}"

# ------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start with optional referral code."""
    user = update.effective_user
    try:
        # If user already has an active session, just notify them
        if is_allowed(user.id) and user.id in USER_SESSIONS:
            await update.message.reply_text(
                "✅ You already have an active session!\n"
                "Send a message, photo, or file.\n"
                "Type /help for commands."
            )
            return

        args = context.args
        is_referral_link = args and args[0].startswith("ref_")
        just_claimed = False
        
        if is_referral_link:
            try:
                referrer_id = int(args[0][4:])
                if referrer_id == user.id:
                    await update.message.reply_text("❌ You cannot refer yourself.")
                else:
                    # ATOMIC DB CHECK: Returns True if first time claiming, False if already claimed
                    claimed_successfully = await asyncio.to_thread(claim_referral, user.id, referrer_id)
                    
                    if not claimed_successfully:
                        await update.message.reply_text("❌ You have already claimed a referral reward previously.")
                    else:
                        # CLAIM WAS SUCCESSFUL
                        just_claimed = True
                        
                        # 1. Grant trial to the new user
                        add_or_extend_premium(user.id, REFERREE_TRIAL_MINUTES)
                        
                        # 2. Reward the referrer
                        add_or_extend_premium(referrer_id, REFERRER_REWARD_MINUTES)
                        REFERRAL_COUNT[referrer_id] = REFERRAL_COUNT.get(referrer_id, 0) + 1

                        # 3. Notify referrer
                        try:
                            ref_expiry = ALLOWED_USERS.get(referrer_id)
                            exp_str = ref_expiry.strftime('%Y-%m-%d %H:%M UTC') if ref_expiry else 'Permanent'
                            await context.bot.send_message(
                                chat_id=referrer_id,
                                text=(
                                    f"🎉 New referral! You earned {REFERRER_REWARD_MINUTES} min premium.\n"
                                    f"Your premium now expires: {exp_str}"
                                ),
                            )
                        except Exception:
                            pass 
            except (ValueError, IndexError):
                await update.message.reply_text("❌ Invalid referral code format.")

        # Check if user is allowed to use the bot
        if not is_allowed(user.id):
            await update.message.reply_text(UNAUTHORIZED_MSG, parse_mode=constants.ParseMode.HTML)
            return

        err = await new_session(user.id)
        if err:
            await update.message.reply_text(err)
            return
            
        if just_claimed:
            await update.message.reply_text(
                f"✅ Welcome! You received a {REFERREE_TRIAL_MINUTES}-minute trial.\n"
                "Send a message, photo, or file to begin."
            )
        else:
            await update.message.reply_text(
                "✅ Session started!\n"
                "Send a message, photo, or file to begin."
            )
    except Exception as e:
        logger.error(f"start handler exception: {e}", exc_info=True)
        await update.message.reply_text(UNAUTHORIZED_MSG, parse_mode=constants.ParseMode.HTML)

async def referral_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Give referral link to ANYONE."""
    user = update.effective_user

    link = f"https://t.me/{BOT_USERNAME}?start=ref_{user.id}"
    count = REFERRAL_COUNT.get(user.id, 0)

    if user.id in ALLOWED_USERS:
        expiry = ALLOWED_USERS[user.id]
        if expiry is None:
            exp_str = "Permanent"
        else:
            exp_str = expiry.strftime("%Y-%m-%d %H:%M UTC")
    else:
        exp_str = "❌ Not premium yet"

    msg = (
        f"🔗 <b>Your Referral Link</b>\n"
        f"<code>{link}</code>\n\n"
        f"👥 Successful referrals: {count}\n"
        f"🕒 Your premium expiry: {exp_str}\n\n"
        f"📌 <b>1 referral = 30 min premium</b>\n"
        f"➕ You get <b>{REFERRER_REWARD_MINUTES} min</b> added per friend.\n"
        f"🎁 New users get <b>{REFERREE_TRIAL_MINUTES} min</b> trial.\n"
        f"🔁 No limit – refer as many as you want!"
    )
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True)

async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id) or user.id not in USER_SESSIONS:
        await update.message.reply_text(UNAUTHORIZED_MSG, parse_mode=constants.ParseMode.HTML)
        return

    if not context.args:
        models_text = "\n".join(f"{i+1}. {name}" for i, (_, name) in enumerate(MODELS))
        await update.message.reply_text(
            f"Available models:\n{models_text}\n\nUse /model <number> to select."
        )
        return
        
    try:
        idx = int(context.args[0]) - 1
        if idx < 0 or idx >= len(MODELS):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid model number.")
        return

    model_id, model_name = MODELS[idx]
    USER_SESSIONS[user.id]["model_id"] = model_id
    USER_SESSIONS[user.id]["model_name"] = model_name
    await update.message.reply_text(f"✅ Model set to: {model_name}")

async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text(UNAUTHORIZED_MSG, parse_mode=constants.ParseMode.HTML)
        return
        
    USER_SESSIONS.pop(user.id, None)
    
    err = await new_session(user.id)
    if err:
        await update.message.reply_text(err)
        return
        
    await update.message.reply_text("✅ Session reset successfully!\nSend a message, photo, or file to begin.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 <b>Claude Chat Bot Commands</b>\n\n"
        "/start – Start a new session\n"
        "/model [number] – Choose Claude model\n"
        "/new – Reset session (fresh credentials)\n"
        "/referral – Get your referral link &amp; stats\n"
        "/redeem <code> – Redeem a premium code\n"
        "/help – Show this help\n\n"
        "<b>OWNER: @NEVER_DIE8</b> – contact for premium"
    )
    await update.message.reply_text(text, parse_mode=constants.ParseMode.HTML)

# ------------------------------------------------------------
# Admin commands
# ------------------------------------------------------------
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /adduser <user_id>")
        return
    try:
        uid = int(context.args[0])
        ALLOWED_USERS[uid] = None
        await update.message.reply_text(f"✅ User {uid} added permanently.")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")

async def add_user_timed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /userid <user_id> <hours>")
        return
    try:
        uid = int(context.args[0])
        hours = float(context.args[1])
        if hours <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID or hours.")
        return

    expiry = datetime.now() + timedelta(hours=hours)
    ALLOWED_USERS[uid] = expiry
    await update.message.reply_text(
        f"✅ User {uid} added for {hours} hour(s). Expires at {expiry.strftime('%Y-%m-%d %H:%M:%S')} UTC."
    )

async def add_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /addid <user_id> <hours> – same as /userid but dedicated."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addid <user_id> <hours>")
        return
    try:
        uid = int(context.args[0])
        hours = float(context.args[1])
        if hours <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID or hours.")
        return

    expiry = datetime.now() + timedelta(hours=hours)
    ALLOWED_USERS[uid] = expiry
    await update.message.reply_text(
        f"✅ User {uid} added for {hours} hour(s). Expires at {expiry.strftime('%Y-%m-%d %H:%M:%S')} UTC."
    )

async def gen_codes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /gen <amount> <days> – generates redeem codes."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /gen <amount_of_codes> <expiry_in_days>")
        return
    try:
        amount = int(context.args[0])
        days = int(context.args[1])
        if amount <= 0 or amount > 50:
            await update.message.reply_text("❌ Amount must be between 1 and 50.")
            return
        if days <= 0 or days > 365:
            await update.message.reply_text("❌ Days must be between 1 and 365.")
            return
    except ValueError:
        await update.message.reply_text("❌ Invalid numbers.")
        return

    codes = await asyncio.to_thread(generate_redeem_codes, amount, days)
    codes_text = "\n".join(f"<code>{c}</code>" for c in codes)
    await update.message.reply_text(
        f"✅ Generated {amount} code(s) for {days} day(s) each:\n{codes_text}",
        parse_mode=constants.ParseMode.HTML
    )

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeuser <user_id>")
        return
    try:
        uid = int(context.args[0])
        if uid in ADMIN_IDS:
            await update.message.reply_text("❌ Cannot remove an admin.")
            return
        ALLOWED_USERS.pop(uid, None)
        USER_SESSIONS.pop(uid, None)
        ALLAC_USERS.discard(uid)
        await update.message.reply_text(f"✅ User {uid} removed.")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    text = " ".join(context.args)
    sent = 0
    for uid in list(ALLOWED_USERS.keys()):
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 Broadcast:\n{text}")
            sent += 1
        except Exception:
            pass 
    await update.message.reply_text(f"✅ Broadcast sent to {sent} users.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    now = datetime.now()
    perm = sum(1 for e in ALLOWED_USERS.values() if e is None)
    temp = sum(1 for e in ALLOWED_USERS.values() if e is not None and e > now)
    expired = sum(1 for e in ALLOWED_USERS.values() if e is not None and e <= now)
    await update.message.reply_text(
        f"📊 Active sessions: {len(USER_SESSIONS)}\n"
        f"👥 Whitelist:\n"
        f"  - Permanent: {perm}\n"
        f"  - Timed (active): {temp}\n"
        f"  - Timed (expired): {expired}\n"
        f"🔗 Total referrals: {sum(REFERRAL_COUNT.values())}"
    )

async def admin_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /reset <user_id>")
        return
    try:
        uid = int(context.args[0])
        if uid in USER_SESSIONS:
            del USER_SESSIONS[uid]
            await update.message.reply_text(f"✅ Session of user {uid} reset.")
        else:
            await update.message.reply_text("❌ No active session.")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")

# ------------------------------------------------------------
# New admin commands: /allac and /off
# ------------------------------------------------------------
async def allac_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activate unlimited premium for all currently known users."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    global ALLAC_USERS
    count = 0
    for uid in list(ALLOWED_USERS.keys()):
        ALLOWED_USERS[uid] = None  # set to unlimited
        ALLAC_USERS.add(uid)
        count += 1
    await update.message.reply_text(f"✅ All {count} users have been granted unlimited premium.")

async def off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Revoke unlimited premium from users who received it via /allac."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    global ALLAC_USERS
    removed = 0
    for uid in list(ALLAC_USERS):
        ALLOWED_USERS.pop(uid, None)
        USER_SESSIONS.pop(uid, None)
        removed += 1
    ALLAC_USERS.clear()
    await update.message.reply_text(f"✅ Unlimited premium revoked for {removed} users. They no longer have access.")

# ------------------------------------------------------------
# New admin command: /list
# ------------------------------------------------------------
def build_user_list(max_users: Optional[int] = None) -> str:
    """Build a formatted text list of all users and their premium status."""
    lines = ["User ID | Premium Status | Expiry | Referrals"]
    now = datetime.now()
    
    # Sort users by ID or premium status; let's just sort by user ID
    sorted_users = sorted(ALLOWED_USERS.items(), key=lambda x: x[0])
    
    # Limit if max_users is specified
    if max_users is not None:
        sorted_users = sorted_users[:max_users]
    
    for uid, expiry in sorted_users:
        referrals = REFERRAL_COUNT.get(uid, 0)
        if expiry is None:
            status = "Unlimited"
            expiry_str = "Permanent"
        elif expiry > now:
            status = "Active"
            expiry_str = expiry.strftime("%Y-%m-%d %H:%M")
        else:
            status = "Expired"
            expiry_str = expiry.strftime("%Y-%m-%d %H:%M")  # still show expired time
        lines.append(f"{uid} | {status} | {expiry_str} | {referrals}")
    
    return "\n".join(lines)

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /list <all/amount> <chat/.txt>"""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /list <all|amount> <chat|.txt>")
        return
    
    first_arg = context.args[0].lower()
    second_arg = context.args[1].lower()
    
    # Determine max_users
    max_users = None
    if first_arg == "all":
        max_users = None
    else:
        try:
            max_users = int(first_arg)
            if max_users <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ First argument must be 'all' or a positive number.")
            return
    
    # Determine output format
    if second_arg not in ("chat", ".txt"):
        await update.message.reply_text("❌ Second argument must be 'chat' or '.txt'.")
        return
    
    user_list_text = build_user_list(max_users)
    
    if second_arg == "chat":
        await send_long_message(update, user_list_text)
    else:  # .txt
        # Write to a temp file and send as document
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8") as tmp:
                tmp.write(user_list_text)
                tmp_path = tmp.name
            
            await update.message.reply_document(
                document=open(tmp_path, "rb"),
                filename="user_list.txt",
                caption=f"📋 User list ({len(ALLOWED_USERS)} total users)"
            )
        except Exception as e:
            logger.error(f"Failed to send user list file: {e}")
            await update.message.reply_text("❌ Failed to create list file.")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

# ------------------------------------------------------------
# User redeem command
# ------------------------------------------------------------
async def redeem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User command: /redeem <code> – claim a premium code."""
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Usage: /redeem <code>")
        return

    code = context.args[0].strip().upper()
    days = await asyncio.to_thread(redeem_code, code)
    if days is None:
        await update.message.reply_text("❌ Invalid or already used code.")
        return

    # Convert days to minutes and add premium
    minutes = days * 24 * 60
    add_or_extend_premium(user.id, minutes)

    expiry = ALLOWED_USERS.get(user.id)
    exp_str = expiry.strftime('%Y-%m-%d %H:%M UTC') if expiry and isinstance(expiry, datetime) else "Permanent"
    await update.message.reply_text(
        f"✅ Code redeemed! You received {days} day(s) of premium.\n"
        f"Your premium now expires: {exp_str}"
    )

# ------------------------------------------------------------
# Message handlers
# ------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text(UNAUTHORIZED_MSG, parse_mode=constants.ParseMode.HTML)
        return

    session = USER_SESSIONS.get(user.id)
    if not session or not session.get("active"):
        await update.message.reply_text("❌ No active session. Use /start.")
        return

    text = update.message.text.strip()
    if not text:
        return

    objects = [
        {
            "object_type": "text",
            "object_url": None,
            "object_text": text,
            "model_type": session["model_id"],
        }
    ]

    await context.bot.send_chat_action(chat_id=user.id, action=constants.ChatAction.TYPING)
    msg_id = await send_message(session["token"], session["chat_uuid"], objects)
    if not msg_id:
        await update.message.reply_text("❌ Failed to send message.")
        return

    # Start thinking animation
    status_msg = await update.message.reply_text("🧠 Thinking...")
    anim_task = asyncio.create_task(animate_thinking(context.bot, user.id, status_msg.message_id))

    try:
        reply = await fetch_reply(session["token"], session["chat_uuid"], msg_id)
    finally:
        anim_task.cancel()
        try:
            await status_msg.delete()
        except Exception:
            pass

    if reply:
        await send_response_with_code_files(update, reply, session["model_name"])
        session["message_count"] += 1
    else:
        await update.message.reply_text("❌ No reply received. (Timed out after 10 minutes)")

    if MAX_MESSAGES > 0 and session["message_count"] >= MAX_MESSAGES:
        await update.message.reply_text("⚠️ Message limit reached. Use /new to reset.")
        session["active"] = False

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text(UNAUTHORIZED_MSG, parse_mode=constants.ParseMode.HTML)
        return

    session = USER_SESSIONS.get(user.id)
    if not session or not session.get("active"):
        await update.message.reply_text("❌ No active session. Use /start.")
        return

    photo_file = await update.message.photo[-1].get_file()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            tmp_path = tmp.name
        await photo_file.download_to_drive(tmp_path)

        await context.bot.send_chat_action(chat_id=user.id, action=constants.ChatAction.UPLOAD_PHOTO)
        img_url = await upload_image(session["token"], session["chat_uuid"], tmp_path)
        if not img_url:
            await update.message.reply_text("❌ Image upload failed.")
            return

        caption = update.message.caption or ""
        objects = []
        if caption.strip():
            objects.append({
                "object_type": "text",
                "object_url": None,
                "object_text": caption,
                "model_type": session["model_id"],
            })
        objects.append({
            "object_type": "image",
            "object_url": img_url,
            "object_text": os.path.basename(tmp_path),
            "model_type": session["model_id"],
        })

        msg_id = await send_message(session["token"], session["chat_uuid"], objects)
        if not msg_id:
            await update.message.reply_text("❌ Failed to send message.")
            return

        # Start thinking animation
        status_msg = await update.message.reply_text("🧠 Thinking...")
        anim_task = asyncio.create_task(animate_thinking(context.bot, user.id, status_msg.message_id))

        try:
            reply = await fetch_reply(session["token"], session["chat_uuid"], msg_id)
        finally:
            anim_task.cancel()
            try:
                await status_msg.delete()
            except Exception:
                pass

        if reply:
            await send_response_with_code_files(update, reply, session["model_name"])
            session["message_count"] += 1
        else:
            await update.message.reply_text("❌ No reply received. (Timed out after 10 minutes)")

        if MAX_MESSAGES > 0 and session["message_count"] >= MAX_MESSAGES:
            await update.message.reply_text("⚠️ Message limit reached. Use /new to reset.")
            session["active"] = False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception as e:
                logger.warning(f"Failed to delete temp file: {e}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles .txt, .py, .js etc. Reads content and sends to Claude."""
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text(UNAUTHORIZED_MSG, parse_mode=constants.ParseMode.HTML)
        return

    session = USER_SESSIONS.get(user.id)
    if not session or not session.get("active"):
        await update.message.reply_text("❌ No active session. Use /start.")
        return

    doc = update.message.document
    if not doc:
        return

    # 5MB limit for text files to prevent memory issues
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("❌ File is too large (max 5MB for text files).")
        return

    tmp_path = None
    try:
        file_obj = await doc.get_file()
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
        await file_obj.download_to_drive(tmp_path)

        # Attempt to read as text
        try:
            with open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as e:
            await update.message.reply_text("❌ Failed to read file content. Is it a text file?")
            return

        if not content.strip():
            await update.message.reply_text("❌ The uploaded file is empty.")
            return

        caption = update.message.caption or ""
        # Determine extension to hint Claude
        ext = os.path.splitext(doc.file_name)[1].lstrip(".") if doc.file_name else "txt"
        
        prompt_text = (
            f"{caption}\n\n"
            f"Here is the content of a file named `{doc.file_name}`:\n"
            f"```{ext}\n{content}\n```"
        ) if caption else (
            f"Here is the content of a file named `{doc.file_name}`:\n"
            f"```{ext}\n{content}\n```"
        )

        objects = [{
            "object_type": "text",
            "object_url": None,
            "object_text": prompt_text,
            "model_type": session["model_id"],
        }]

        await context.bot.send_chat_action(chat_id=user.id, action=constants.ChatAction.TYPING)
        msg_id = await send_message(session["token"], session["chat_uuid"], objects)
        if not msg_id:
            await update.message.reply_text("❌ Failed to send message.")
            return

        # Start thinking animation
        status_msg = await update.message.reply_text("🧠 Thinking...")
        anim_task = asyncio.create_task(animate_thinking(context.bot, user.id, status_msg.message_id))

        try:
            reply = await fetch_reply(session["token"], session["chat_uuid"], msg_id)
        finally:
            anim_task.cancel()
            try:
                await status_msg.delete()
            except Exception:
                pass

        if reply:
            await send_response_with_code_files(update, reply, session["model_name"])
            session["message_count"] += 1
        else:
            await update.message.reply_text("❌ No reply received. (Timed out after 10 minutes)")

        if MAX_MESSAGES > 0 and session["message_count"] >= MAX_MESSAGES:
            await update.message.reply_text("⚠️ Message limit reached. Use /new to reset.")
            session["active"] = False
    except Exception as e:
        logger.error(f"Document handler error: {e}", exc_info=True)
        await update.message.reply_text("❌ Failed to process document.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

# ------------------------------------------------------------
# Error handler
# ------------------------------------------------------------
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Exception:", exc_info=context.error)
    if update and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ An internal error occurred. Please try again.",
            )
        except Exception:
            pass 

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main():
    init_db()  # Initialize SQLite database on startup
    
    # concurrent_updates=True allows the bot to handle multiple users in parallel
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("referral", referral_cmd))
    app.add_handler(CommandHandler("model", model_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("redeem", redeem_cmd))

    app.add_handler(CommandHandler("adduser", add_user))
    app.add_handler(CommandHandler("userid", add_user_timed))
    app.add_handler(CommandHandler("addid", add_id_cmd))
    app.add_handler(CommandHandler("gen", gen_codes))
    app.add_handler(CommandHandler("allac", allac_cmd))
    app.add_handler(CommandHandler("off", off_cmd))
    app.add_handler(CommandHandler("list", list_cmd))  # New /list command
    app.add_handler(CommandHandler("removeuser", remove_user))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset", admin_reset))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    app.add_error_handler(error_handler)

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
