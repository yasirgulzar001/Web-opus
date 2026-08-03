#!/usr/bin/env python3
"""
Telegram Bot for Syntx.ai Claude Chat (Bot‑Only Version)
- Text, photos, documents (txt, py, etc.)
- Multiple Claude models
- Time‑based access (/addid <user_id> <hours>)
- Admin control
No external URLs, no HTTPS, no ngrok required.
"""

import asyncio
import logging
import os
import re
import tempfile
import time
from datetime import datetime
from typing import Dict, Optional

import cloudscraper
import requests
from telegram import Update, constants
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ============================================================
# CONFIGURATION – EDIT THESE TWO VALUES
# ============================================================
BOT_TOKEN = "8799719369:AAGvETel8yd-Dijvu47W87nRB6hqNPyUWMc"          # Your bot token from @BotFather
ADMIN_IDS = {6535041385}                   # Your Telegram numeric ID
# ============================================================

# External APIs (unchanged)
EMAIL_API = "https://zecora0.serv00.net/Gmail.php"
SYNTX_AUTH_SEND_OTP = "https://api.syntx.ai/api/v1/auth/email/send-otp"
SYNTX_AUTH_VERIFY_OTP = "https://api.syntx.ai/api/v1/auth/email/verify-otp"
SYNTX_CHATS = "https://api.syntx.ai/api/v1/chats"
SYNTX_UPLOAD = "https://api.syntx.ai/api/v1/chats/upload-files"

HTTP_TIMEOUT = 15
OTP_TIMEOUT = 120
REPLY_TIMEOUT = 120
POLL_INTERVAL = 2
MAX_FILE_SIZE_MB = 20
MAX_TEXT_FILE_SIZE_MB = 5

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

# Text file extensions (read content directly)
TEXT_FILE_EXTENSIONS = {
    '.txt', '.py', '.log', '.md', '.json', '.xml', '.csv', '.yaml', '.ini',
    '.cfg', '.sh', '.bat', '.js', '.ts', '.html', '.css', '.sql', '.r',
    '.java', '.c', '.cpp', '.h', '.rb', '.go', '.rs', '.swift', '.kt',
    '.php', '.pl', '.lua', '.conf', '.toml', '.env', '.gitignore',
    '.dockerfile', '.makefile', '.cmake'
}

# ------------------------------------------------------------
ALLOWED_USERS: set[int] = set(ADMIN_IDS)
USER_EXPIRY: Dict[int, float] = {}
USER_SESSIONS: Dict[int, dict] = {}

scraper = cloudscraper.create_scraper()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------
async def create_email():
    resp = scraper.get(f"{EMAIL_API}?action=create", timeout=HTTP_TIMEOUT)
    if resp.status_code != 200:
        raise Exception("Email creation failed")
    data = resp.json()
    if 'error' in data or not data.get('email'):
        raise Exception("Invalid email response")
    return data['email'], data['id']

async def fetch_otp(email: str, mailbox_id: str) -> Optional[str]:
    start = time.time()
    last_id = None
    while time.time() - start < OTP_TIMEOUT:
        try:
            resp = scraper.get(
                f"{EMAIL_API}?action=get_messages&mailbox_id={mailbox_id}&email={email}",
                timeout=HTTP_TIMEOUT
            )
            if resp.status_code == 200:
                msgs = resp.json()
                if msgs and isinstance(msgs, list):
                    first = msgs[0]
                    if first.get('id') != last_id:
                        last_id = first['id']
                        body = first.get('html', '') or first.get('text', '') or first.get('body', '')
                        match = re.search(r'\b(\d{6})\b', body)
                        if match:
                            return match.group(1)
        except Exception:
            pass
        await asyncio.sleep(POLL_INTERVAL)
    return None

async def send_otp_api(email: str) -> bool:
    resp = scraper.post(
        SYNTX_AUTH_SEND_OTP,
        json={"email": email, "ref_uuid": None, "utm": ""},
        headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36"}
    )
    return resp.status_code == 200 and resp.json().get('success')

async def verify_otp_api(email: str, otp: str) -> Optional[str]:
    resp = scraper.post(
        SYNTX_AUTH_VERIFY_OTP,
        json={"email": email, "otp_code": otp, "ref_uuid": None, "utm": ""},
        headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36"}
    )
    if resp.status_code == 200 and resp.json().get('success'):
        return resp.json().get('token')
    return None

async def create_chat_api(token: str) -> Optional[str]:
    resp = scraper.post(
        SYNTX_CHATS,
        json={"title": "Claude Chat", "scope": "text"},
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"}
    )
    if resp.status_code == 201:
        return resp.json().get('uuid')
    return None

async def upload_file_to_syntx(token: str, chat_uuid: str, file_path: str) -> Optional[str]:
    try:
        with open(file_path, 'rb') as f:
            resp = requests.post(
                SYNTX_UPLOAD,
                data={"check_duplicates": "true", "chat_uuid": chat_uuid},
                files={"files": (os.path.basename(file_path), f, 'application/octet-stream')},
                headers={"Authorization": f"Bearer {token}"},
                timeout=HTTP_TIMEOUT
            )
        if resp.status_code == 200 and resp.json().get('successful', 0) > 0:
            return resp.json()['files'][0]['url']
    except Exception as e:
        logger.error(f"File upload failed: {e}")
    return None

async def send_message_api(token: str, chat_uuid: str, objects: list) -> Optional[int]:
    resp = scraper.post(
        f"{SYNTX_CHATS}/{chat_uuid}/messages?ai_name=claude",
        json={"objects": objects},
        headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"}
    )
    if resp.status_code == 200:
        return resp.json().get('id')
    return None

async def fetch_reply_api(token: str, chat_uuid: str, after_id: int) -> Optional[str]:
    start = time.time()
    while time.time() - start < REPLY_TIMEOUT:
        resp = scraper.get(
            f"{SYNTX_CHATS}/{chat_uuid}/messages?page_size=20",
            headers={"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"}
        )
        if resp.status_code == 200:
            for msg in resp.json().get('messages', []):
                if msg.get('author_id') == -1 and msg.get('id', 0) > after_id:
                    obj = msg.get('message_object', [{}])[0]
                    if obj.get('object_type') == 'text' and obj.get('completed'):
                        return obj.get('object_text')
        await asyncio.sleep(POLL_INTERVAL)
    return None

# ------------------------------------------------------------
# FILE HANDLING
# ------------------------------------------------------------
def is_text_file(filename: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return ext in TEXT_FILE_EXTENSIONS

async def read_file_content(file_path: str) -> Optional[str]:
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except UnicodeDecodeError:
        try:
            with open(file_path, 'r', encoding='latin-1') as f:
                return f.read()
        except Exception:
            return None
    except Exception:
        return None

async def process_document(update: Update, session: dict) -> Optional[list]:
    doc = update.message.document
    file_size_mb = doc.file_size / (1024 * 1024) if doc.file_size else 0
    file_name = doc.file_name or f"file_{int(time.time())}"

    if file_size_mb > MAX_FILE_SIZE_MB:
        await update.message.reply_text(f"❌ File too large ({file_size_mb:.1f} MB). Max {MAX_FILE_SIZE_MB} MB.")
        return None

    file = await doc.get_file()
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file_name)[1]) as tmp:
        await file.download_to_memory()
        tmp.write(file.download_as_bytearray())
        tmp_path = tmp.name

    objects = []

    if is_text_file(file_name) and file_size_mb < MAX_TEXT_FILE_SIZE_MB:
        content = await read_file_content(tmp_path)
        if content is not None:
            caption = update.message.caption or ""
            text_content = f"📄 File: {file_name}\n"
            if caption:
                text_content += f"Caption: {caption}\n\n"
            text_content += f"```\n{content[:50000]}\n```"
            objects.append({
                "object_type": "text",
                "object_url": None,
                "object_text": text_content,
                "model_type": session['model_id']
            })
            os.unlink(tmp_path)
            return objects

    await update.message.reply_text("📎 Uploading file...")
    file_url = await upload_file_to_syntx(session['token'], session['chat_uuid'], tmp_path)
    os.unlink(tmp_path)

    if not file_url:
        await update.message.reply_text("❌ File upload failed.")
        return None

    caption = update.message.caption or ""
    if caption:
        objects.append({
            "object_type": "text",
            "object_url": None,
            "object_text": caption,
            "model_type": session['model_id']
        })

    objects.append({
        "object_type": "file",
        "object_url": file_url,
        "object_text": file_name,
        "model_type": session['model_id']
    })

    return objects

# ------------------------------------------------------------
# ACCESS CONTROL
# ------------------------------------------------------------
async def check_access(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    if user_id not in ALLOWED_USERS:
        return False
    expiry = USER_EXPIRY.get(user_id)
    if expiry and time.time() > expiry:
        ALLOWED_USERS.discard(user_id)
        USER_EXPIRY.pop(user_id, None)
        USER_SESSIONS.pop(user_id, None)
        return False
    return True

async def new_session(uid: int) -> Optional[str]:
    try:
        email, mid = await create_email()
        if not await send_otp_api(email):
            return "❌ Failed to send OTP."
        otp = await fetch_otp(email, mid)
        if not otp:
            return "❌ OTP not received."
        token = await verify_otp_api(email, otp)
        if not token:
            return "❌ OTP verification failed."
        chat_uuid = await create_chat_api(token)
        if not chat_uuid:
            return "❌ Chat creation failed."
        USER_SESSIONS[uid] = {
            'email': email,
            'token': token,
            'chat_uuid': chat_uuid,
            'model_id': 'claude-sonnet-5',
            'model_name': 'Claude Sonnet 5',
            'last_message_id': 0,
            'message_count': 0
        }
        return None
    except Exception as e:
        logger.error(f"new_session error: {e}")
        return f"❌ Error: {e}"

# ------------------------------------------------------------
# BOT HANDLERS
# ------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_access(user.id):
        await update.message.reply_text("⛔ Access denied. Contact admin.")
        return

    err = await new_session(user.id)
    if err:
        await update.message.reply_text(err)
        return

    help_text = (
        "🤖 *Claude Chat Bot Commands*\n\n"
        "/start – Start a new session\n"
        "/model number – Choose Claude model\n"
        "/new – Reset session (fresh credentials)\n"
        "/help – Show this help\n\n"
        "📌 *Usage:*\n"
        "Send a text message, photo, or document.\n"
        "For .txt, .py and other text files, content is read directly.\n"
        "Other files are uploaded and attached to the message."
    )
    await update.message.reply_text("✅ Session started!")
    await update.message.reply_text(help_text, parse_mode=constants.ParseMode.MARKDOWN)

async def model_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_access(user.id) or user.id not in USER_SESSIONS:
        await update.message.reply_text("❌ No active session. Use /start first.")
        return
    if not context.args:
        models_list = "\n".join(f"{i+1}. {name}" for i, (_, name) in enumerate(MODELS))
        await update.message.reply_text(f"Models:\n{models_list}\nUse /model <number>")
        return
    try:
        idx = int(context.args[0]) - 1
        if idx < 0 or idx >= len(MODELS):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Invalid number.")
        return
    mid, mname = MODELS[idx]
    USER_SESSIONS[user.id]['model_id'] = mid
    USER_SESSIONS[user.id]['model_name'] = mname
    await update.message.reply_text(f"✅ Model set to: {mname}")

async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_access(user.id):
        return
    USER_SESSIONS.pop(user.id, None)
    await start(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands: /start, /model, /new, /help.\n"
        "Send a text message, photo, or document."
    )

# Admin commands
async def addid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await update.message.reply_text("❌ Invalid arguments.")
        return
    expiry = time.time() + hours * 3600
    ALLOWED_USERS.add(uid)
    USER_EXPIRY[uid] = expiry
    until = datetime.fromtimestamp(expiry).strftime("%Y-%m-%d %H:%M")
    await update.message.reply_text(f"✅ User {uid} added for {hours} hours (until {until}).")
    try:
        await context.bot.send_message(uid, f"🎉 You have been granted premium access for {hours} hours.")
    except Exception:
        pass

async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeuser <id>")
        return
    try:
        uid = int(context.args[0])
        if uid in ADMIN_IDS:
            await update.message.reply_text("Cannot remove admin.")
            return
        ALLOWED_USERS.discard(uid)
        USER_EXPIRY.pop(uid, None)
        USER_SESSIONS.pop(uid, None)
        await update.message.reply_text(f"✅ User {uid} removed.")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    active = len(USER_SESSIONS)
    now = time.time()
    premium = sum(1 for u in ALLOWED_USERS if u not in ADMIN_IDS and USER_EXPIRY.get(u, 0) > now)
    await update.message.reply_text(f"Active sessions: {active}\nPremium users: {premium}")

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <msg>")
        return
    text = " ".join(context.args)
    sent = 0
    for uid in list(USER_SESSIONS.keys()):
        try:
            await context.bot.send_message(uid, f"📢 {text}")
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"Sent to {sent} users.")

# ------------------------------------------------------------
# MESSAGE HANDLERS (text, photo, document)
# ------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_access(user.id):
        await update.message.reply_text("⛔ Access denied.")
        return
    session = USER_SESSIONS.get(user.id)
    if not session:
        await update.message.reply_text("❌ No active session. Use /start.")
        return
    text = update.message.text.strip()
    if not text:
        return
    objects = [{
        "object_type": "text",
        "object_url": None,
        "object_text": text,
        "model_type": session['model_id']
    }]
    await send_and_reply(update, session, objects, user.id, context)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_access(user.id):
        await update.message.reply_text("⛔ Access denied.")
        return
    session = USER_SESSIONS.get(user.id)
    if not session:
        await update.message.reply_text("❌ No active session. Use /start.")
        return

    photo_file = await update.message.photo[-1].get_file()
    caption = update.message.caption or ""

    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        await photo_file.download_to_memory()
        tmp.write(photo_file.download_as_bytearray())
        tmp_path = tmp.name

    await context.bot.send_chat_action(chat_id=user.id, action=constants.ChatAction.UPLOAD_PHOTO)
    img_url = await upload_file_to_syntx(session['token'], session['chat_uuid'], tmp_path)
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
            "model_type": session['model_id']
        })
    objects.append({
        "object_type": "image",
        "object_url": img_url,
        "object_text": os.path.basename(tmp_path),
        "model_type": session['model_id']
    })

    await send_and_reply(update, session, objects, user.id, context)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_access(user.id):
        await update.message.reply_text("⛔ Access denied.")
        return
    session = USER_SESSIONS.get(user.id)
    if not session:
        await update.message.reply_text("❌ No active session. Use /start.")
        return

    await context.bot.send_chat_action(chat_id=user.id, action=constants.ChatAction.TYPING)
    objects = await process_document(update, session)
    if objects is None:
        return
    await send_and_reply(update, session, objects, user.id, context)

async def send_and_reply(update: Update, session: dict, objects: list, chat_id: int, context):
    msg_id = await send_message_api(session['token'], session['chat_uuid'], objects)
    if not msg_id:
        await update.message.reply_text("❌ Failed to send message.")
        return
    reply = await fetch_reply_api(session['token'], session['chat_uuid'], msg_id)
    if reply:
        for i in range(0, len(reply), 4000):
            await context.bot.send_message(chat_id=chat_id, text=reply[i:i+4000])
        session['message_count'] += 1
    else:
        await context.bot.send_message(chat_id=chat_id, text="❌ No reply received.")

# ------------------------------------------------------------
# BACKGROUND: expire users
# ------------------------------------------------------------
async def expire_loop():
    while True:
        now = time.time()
        to_remove = [uid for uid, exp in USER_EXPIRY.items() if exp <= now]
        for uid in to_remove:
            ALLOWED_USERS.discard(uid)
            USER_EXPIRY.pop(uid, None)
            USER_SESSIONS.pop(uid, None)
            logger.info(f"Access expired for user {uid}")
        await asyncio.sleep(60)

# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("model", model_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    # Admin
    app.add_handler(CommandHandler("addid", addid_cmd))
    app.add_handler(CommandHandler("removeuser", removeuser_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    await app.initialize()
    await app.start()
    asyncio.create_task(app.updater.start_polling())
    await expire_loop()

if __name__ == "__main__":
    asyncio.run(main())
