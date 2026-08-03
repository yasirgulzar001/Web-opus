#!/usr/bin/env python3
"""
Telegram Bot for Syntx.ai Claude Chat with Admin Control + Referral System

Features:
- Temporary email creation + auto OTP verification.
- Multiple Claude models.
- Text and photo messages (images uploaded to Syntx.ai first).
- Per-user sessions with persistent token and chat UUID.
- Admin commands: /adduser, /removeuser, /broadcast, /stats, /reset, /userid.
- Time-limited user access (via /userid <id> <hours>).
- Referral system: /referral to get your invite link.
  - 1 successful referral = 30 min extra premium for the referrer.
  - New user gets 60 min trial.
- Owner: @NEVER_DIE8
"""

import asyncio
import logging
import re
import os
import sys
import time
import tempfile
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

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
# Configuration (move to a .env file in production)
# ------------------------------------------------------------
BOT_TOKEN = "8710434434:AAHR3EcMzwmGh9dBuj8cO0NXDlPvG_05I8Y"          # Replace with your bot token
ADMIN_IDS = {6535041385}                    # Replace with your Telegram user IDs
BOT_USERNAME = "PROBIX_AIbot"      # Your bot's username (without @)

# Referral rewards (minutes)
REFERRER_REWARD_MINUTES = 30       # Given to the referrer
REFERREE_TRIAL_MINUTES = 60        # Given to the new user

# API endpoints (same as before)
EMAIL_API = "https://zecora0.serv00.net/Gmail.php"
SYNTX_AUTH_SEND_OTP = "https://api.syntx.ai/api/v1/auth/email/send-otp"
SYNTX_AUTH_VERIFY_OTP = "https://api.syntx.ai/api/v1/auth/email/verify-otp"
SYNTX_CHATS = "https://api.syntx.ai/api/v1/chats"
SYNTX_UPLOAD = "https://api.syntx.ai/api/v1/chats/upload-files"

HTTP_TIMEOUT = 15
OTP_TIMEOUT = 120
REPLY_TIMEOUT = 120
POLL_INTERVAL = 2
MAX_MESSAGES = 0

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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Global state (in-memory; add DB for production persistence)
# ------------------------------------------------------------
# Allowed users: user_id -> expiry (None = permanent)
ALLOWED_USERS: Dict[int, Optional[datetime]] = {uid: None for uid in ADMIN_IDS}

# User sessions
USER_SESSIONS: Dict[int, Dict[str, Any]] = {}

# Referral statistics (optional, just for /referral display)
REFERRAL_COUNT: Dict[int, int] = {}

scraper = cloudscraper.create_scraper()

# ------------------------------------------------------------
# Whitelist helpers
# ------------------------------------------------------------
def is_allowed(user_id: int) -> bool:
    """Check if user is allowed and not expired. Removes expired entries."""
    expiry = ALLOWED_USERS.get(user_id)
    if expiry is None:
        return True
    if expiry > datetime.now():
        return True
    # expired
    ALLOWED_USERS.pop(user_id, None)
    USER_SESSIONS.pop(user_id, None)
    return False

def extend_user_time(user_id: int, minutes: int):
    """Add minutes to a user's expiry (only if they have a finite expiry)."""
    if user_id not in ALLOWED_USERS or ALLOWED_USERS[user_id] is None:
        # Permanent user or not in list – do nothing, but we could still add if new
        return
    current_expiry = ALLOWED_USERS[user_id]
    if current_expiry is None:
        return
    new_expiry = max(current_expiry, datetime.now()) + timedelta(minutes=minutes)
    ALLOWED_USERS[user_id] = new_expiry

# ------------------------------------------------------------
# Helper: send long messages
# ------------------------------------------------------------
async def send_long_message(update: Update, text: str, parse_mode: str = None):
    max_len = 4000
    for i in range(0, len(text), max_len):
        chunk = text[i:i+max_len]
        await update.message.reply_text(chunk, parse_mode=parse_mode)

# ------------------------------------------------------------
# Email & OTP (unchanged)
# ------------------------------------------------------------
async def create_email() -> tuple[str, str]:
    try:
        resp = scraper.get(f"{EMAIL_API}?action=create", timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            raise Exception(f"Status {resp.status_code}")
        data = resp.json()
        if "error" in data or not data.get("email"):
            raise Exception("Invalid response")
        return data["email"], data["id"]
    except Exception as e:
        logger.error(f"Email creation failed: {e}")
        raise

async def fetch_otp(email: str, mailbox_id: str) -> Optional[str]:
    start = time.time()
    last_msg_id = None
    while time.time() - start < OTP_TIMEOUT:
        try:
            resp = scraper.get(
                f"{EMAIL_API}?action=get_messages&mailbox_id={mailbox_id}&email={email}",
                timeout=HTTP_TIMEOUT,
            )
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
# Syntx.ai API calls (unchanged)
# ------------------------------------------------------------
async def send_otp(email: str) -> bool:
    try:
        resp = scraper.post(
            SYNTX_AUTH_SEND_OTP,
            json={"email": email, "ref_uuid": None, "utm": ""},
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36"},
            timeout=HTTP_TIMEOUT,
        )
        return resp.status_code == 200 and resp.json().get("success")
    except Exception as e:
        logger.error(f"send_otp error: {e}")
        return False

async def verify_otp(email: str, otp: str) -> Optional[str]:
    try:
        resp = scraper.post(
            SYNTX_AUTH_VERIFY_OTP,
            json={"email": email, "otp_code": otp, "ref_uuid": None, "utm": ""},
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200 and resp.json().get("success"):
            return resp.json().get("token")
    except Exception as e:
        logger.error(f"verify_otp error: {e}")
    return None

async def create_chat(token: str, title: str = "Claude Chat") -> Optional[str]:
    try:
        resp = scraper.post(
            SYNTX_CHATS,
            json={"title": title, "scope": "text"},
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36",
            },
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 201:
            return resp.json().get("uuid")
    except Exception as e:
        logger.error(f"create_chat error: {e}")
    return None

async def upload_image(token: str, chat_uuid: str, file_path: str) -> Optional[str]:
    try:
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
    except Exception as e:
        logger.error(f"Image upload error: {e}")
    return None

async def send_message(token: str, chat_uuid: str, objects: list) -> Optional[int]:
    try:
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
    except Exception as e:
        logger.error(f"send_message error: {e}")
    return None

async def fetch_reply(
    token: str, chat_uuid: str, after_id: int, timeout: int = REPLY_TIMEOUT
) -> Optional[str]:
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = scraper.get(
                f"{SYNTX_CHATS}/{chat_uuid}/messages?page_size=20",
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36",
                },
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code == 200:
                messages = resp.json().get("messages", [])
                for msg in messages:
                    if msg.get("author_id") == -1 and msg.get("id", 0) > after_id:
                        obj = msg.get("message_object", [{}])[0]
                        if obj.get("object_type") == "text" and obj.get("completed"):
                            return obj.get("object_text")
        except Exception as e:
            logger.warning(f"fetch_reply error: {e}")
        await asyncio.sleep(POLL_INTERVAL)
    return None

# ------------------------------------------------------------
# Session management (unchanged)
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
            "last_message_id": 0,
            "active": True,
        }
        return None
    except Exception as e:
        logger.error(f"new_session exception: {e}")
        return f"❌ Internal error: {e}"

# ------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start with optional referral code."""
    user = update.effective_user
    args = context.args  # list of words after /start

    # 1. Check if user is already allowed (even if expired, is_allowed will clean)
    if is_allowed(user.id):
        # Already authorized, just start session (ignore referral if any)
        err = await new_session(user.id)
        if err:
            await update.message.reply_text(err)
            return
        await update.message.reply_text(
            "✅ Session started!\n"
            "Model: Claude Sonnet 5 (change with /model)\n"
            "Send a message or a photo with optional caption.\n"
            "Type /help for commands."
        )
        return

    # 2. User NOT authorized yet – check for referral code
    referral_processed = False
    if args and args[0].startswith("ref_"):
        try:
            referrer_id = int(args[0][4:])  # remove "ref_"
            if referrer_id == user.id:
                await update.message.reply_text("❌ You cannot refer yourself.")
                return
            if referrer_id in ALLOWED_USERS:
                # Valid referrer – give new user trial time
                expiry = datetime.now() + timedelta(minutes=REFERREE_TRIAL_MINUTES)
                ALLOWED_USERS[user.id] = expiry

                # Give referrer bonus
                extend_user_time(referrer_id, REFERRER_REWARD_MINUTES)
                REFERRAL_COUNT[referrer_id] = REFERRAL_COUNT.get(referrer_id, 0) + 1

                # Notify referrer (optional)
                try:
                    await context.bot.send_message(
                        chat_id=referrer_id,
                        text=(
                            f"🎉 New referral! User {user.mention_html()} joined using your link.\n"
                            f"➕ You earned {REFERRER_REWARD_MINUTES} minutes of premium.\n"
                            f"Your new expiry: {ALLOWED_USERS[referrer_id].strftime('%Y-%m-%d %H:%M') if ALLOWED_USERS[referrer_id] else 'Permanent'}"
                        ),
                        parse_mode=constants.ParseMode.HTML,
                    )
                except Exception as e:
                    logger.warning(f"Could not notify referrer {referrer_id}: {e}")

                referral_processed = True
            else:
                await update.message.reply_text("❌ Invalid referral link. The referrer is not a valid user.")
                return
        except (ValueError, IndexError):
            await update.message.reply_text("❌ Invalid referral code format.")
            return

    if not referral_processed:
        # No valid referral – show the beautiful unauthorized message
        await update.message.reply_text(
            "⛔ <b>Access Denied</b>\n\n"
            "🔹 <b>To get premium access:</b>\n"
            "   📩 Contact <a href='https://t.me/NEVER_DIE8'>@NEVER_DIE8</a>\n\n"
            "🔹 <b>Or use a referral link</b> from an existing user.\n"
            "   👥 Get 1 hour free trial + 30 min bonus for your inviter!\n"
            "   🔗 Ask a friend for their invite link, or type /referral if you already have access.",
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    # Referral processed – start session for the new user
    err = await new_session(user.id)
    if err:
        await update.message.reply_text(err)
        return

    await update.message.reply_text(
        f"✅ Welcome! You received a <b>{REFERREE_TRIAL_MINUTES}-minute trial</b>.\n"
        "Model: Claude Sonnet 5 (change with /model)\n"
        "Send a message or a photo with optional caption.\n"
        "Type /help for commands.",
        parse_mode=constants.ParseMode.HTML,
    )

async def referral_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the user's referral link and stats."""
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text(
            "⛔ <b>You need premium to use referrals.</b>\n"
            "Contact @NEVER_DIE8 to get access, or use someone else's referral link.",
            parse_mode=constants.ParseMode.HTML,
        )
        return

    link = f"https://t.me/{BOT_USERNAME}?start=ref_{user.id}"
    count = REFERRAL_COUNT.get(user.id, 0)
    expiry_info = ALLOWED_USERS.get(user.id)
    if expiry_info is None:
        expiry_str = "Permanent"
    else:
        expiry_str = expiry_info.strftime("%Y-%m-%d %H:%M UTC")

    msg = (
        f"🔗 <b>Your Referral Link</b>\n"
        f"<code>{link}</code>\n\n"
        f"👥 Successful referrals: {count}\n"
        f"🕒 Your premium expiry: {expiry_str}\n\n"
        f"📌 Earn <b>{REFERRER_REWARD_MINUTES} min</b> for each friend who joins using your link."
    )
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True)

async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id) or user.id not in USER_SESSIONS:
        await update.message.reply_text("❌ No active session. Use /start first.")
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
    session = USER_SESSIONS[user.id]
    session["model_id"] = model_id
    session["model_name"] = model_name
    await update.message.reply_text(f"✅ Model set to: {model_name}")

async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if user.id in USER_SESSIONS:
        del USER_SESSIONS[user.id]
    await start(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmds = """
🤖 <b>Claude Chat Bot Commands</b>

/start – Start a new session
/model [number] – Choose Claude model
/new – Reset session (fresh credentials)
/referral – Get your referral link & stats
/help – Show this help

<b>Usage:</b>
Send a text message, or a photo with optional caption.
The bot will reply using the selected Claude model.
    """
    await update.message.reply_text(cmds, parse_mode=constants.ParseMode.HTML)

# ------------------------------------------------------------
# Admin commands (unchanged except minor updates)
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
        ALLOWED_USERS[uid] = None   # permanent
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
        await update.message.reply_text(f"✅ User {uid} removed.")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    text = " ".join(context.args)
    sent = 0
    for uid in list(USER_SESSIONS.keys()):
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 Broadcast:\n{text}")
            sent += 1
        except Exception as e:
            logger.warning(f"Broadcast to {uid} failed: {e}")
    await update.message.reply_text(f"✅ Broadcast sent to {sent} users.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return

    now = datetime.now()
    permanent = sum(1 for e in ALLOWED_USERS.values() if e is None)
    timed_active = sum(1 for e in ALLOWED_USERS.values() if e is not None and e > now)
    timed_expired = sum(1 for e in ALLOWED_USERS.values() if e is not None and e <= now)

    await update.message.reply_text(
        f"📊 Active sessions: {len(USER_SESSIONS)}\n"
        f"👥 Whitelist:\n"
        f"  - Permanent: {permanent}\n"
        f"  - Timed (active): {timed_active}\n"
        f"  - Timed (expired, will be removed): {timed_expired}\n"
        f"🔗 Total referrals tracked: {sum(REFERRAL_COUNT.values())}"
    )

async def admin_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
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
            await update.message.reply_text("❌ No active session for that user.")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")

# ------------------------------------------------------------
# Message handlers (unchanged)
# ------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text(
            "⛔ <b>Access Denied</b>\n\n"
            "🔹 <b>To get premium access:</b>\n"
            "   📩 Contact <a href='https://t.me/NEVER_DIE8'>@NEVER_DIE8</a>\n\n"
            "🔹 <b>Or use a referral link</b> from an existing user.\n"
            "   👥 Get 1 hour free trial + 30 min bonus for your inviter!\n"
            "   🔗 Ask a friend for their invite link, or type /referral if you already have access.",
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    session = USER_SESSIONS.get(user.id)
    if not session or not session.get("active"):
        await update.message.reply_text("❌ No active session. Use /start.")
        return

    text = update.message.text
    if not text.strip():
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

    reply = await fetch_reply(session["token"], session["chat_uuid"], msg_id)
    if reply:
        await send_long_message(update, f"**{session['model_name']}:** {reply}",
                                parse_mode=constants.ParseMode.MARKDOWN)
        session["message_count"] += 1
    else:
        await update.message.reply_text("❌ No reply received (timeout).")

    if MAX_MESSAGES > 0 and session["message_count"] >= MAX_MESSAGES:
        await update.message.reply_text("⚠️ Message limit reached. Use /new to reset.")
        session["active"] = False

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text(
            "⛔ <b>Access Denied</b>\n\n"
            "🔹 <b>To get premium access:</b>\n"
            "   📩 Contact <a href='https://t.me/NEVER_DIE8'>@NEVER_DIE8</a>\n\n"
            "🔹 <b>Or use a referral link</b> from an existing user.\n"
            "   👥 Get 1 hour free trial + 30 min bonus for your inviter!\n"
            "   🔗 Ask a friend for their invite link, or type /referral if you already have access.",
            parse_mode=constants.ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    session = USER_SESSIONS.get(user.id)
    if not session or not session.get("active"):
        await update.message.reply_text("❌ No active session. Use /start.")
        return

    photo_file = await update.message.photo[-1].get_file()
    caption = update.message.caption or ""

    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        await photo_file.download_to_memory()
        tmp.write(photo_file.download_as_bytearray())
        tmp_path = tmp.name

    await context.bot.send_chat_action(chat_id=user.id, action=constants.ChatAction.UPLOAD_PHOTO)
    img_url = await upload_image(session["token"], session["chat_uuid"], tmp_path)
    os.unlink(tmp_path)

    if not img_url:
        await update.message.reply_text("❌ Image upload failed.")
        return

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

    reply = await fetch_reply(session["token"], session["chat_uuid"], msg_id)
    if reply:
        await send_long_message(update, f"**{session['model_name']}:** {reply}",
                                parse_mode=constants.ParseMode.MARKDOWN)
        session["message_count"] += 1
    else:
        await update.message.reply_text("❌ No reply received.")

    if MAX_MESSAGES > 0 and session["message_count"] >= MAX_MESSAGES:
        await update.message.reply_text("⚠️ Message limit reached. Use /new to reset.")
        session["active"] = False

# ------------------------------------------------------------
# Error handler
# ------------------------------------------------------------
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Exception while handling update:", exc_info=context.error)
    if update and update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⚠️ An internal error occurred. Please try again.",
        )

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("referral", referral_cmd))
    app.add_handler(CommandHandler("model", model_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    # Admin commands
    app.add_handler(CommandHandler("adduser", add_user))
    app.add_handler(CommandHandler("userid", add_user_timed))
    app.add_handler(CommandHandler("removeuser", remove_user))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset", admin_reset))

    # Message handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Error handler
    app.add_error_handler(error_handler)

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
