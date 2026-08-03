import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Any
from urllib.parse import unquote

import cloudscraper
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from telegram import Update, constants, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import uvicorn

# ============================================================
# CONFIGURATION - EDIT THESE
# ============================================================
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"          # Replace with your bot token
ADMIN_IDS = {1234567890}                   # Replace with your Telegram user ID(s)
OWNER_USERNAME = "@NEVER_DIE8"             # For display only

# API endpoints
EMAIL_API = "https://zecora0.serv00.net/Gmail.php"
SYNTX_AUTH_SEND_OTP = "https://api.syntx.ai/api/v1/auth/email/send-otp"
SYNTX_AUTH_VERIFY_OTP = "https://api.syntx.ai/api/v1/auth/email/verify-otp"
SYNTX_CHATS = "https://api.syntx.ai/api/v1/chats"
SYNTX_UPLOAD = "https://api.syntx.ai/api/v1/chats/upload-files"

HTTP_TIMEOUT = 15
OTP_TIMEOUT = 120
REPLY_TIMEOUT = 120
POLL_INTERVAL = 2

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

# ============================================================
# GLOBAL STATE
# ============================================================
ALLOWED_USERS: set[int] = set(ADMIN_IDS)
USER_EXPIRY: Dict[int, float] = {}          # user_id -> expiry timestamp
USER_SESSIONS: Dict[int, Dict] = {}         # active sessions

scraper = cloudscraper.create_scraper()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# MINI APP HTML (embedded)
# ============================================================
MINI_APP_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Claude Chat</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {
      --bg: #1a1a2e; --surface: #16213e; --text: #e0e0e0;
      --accent: #0f3460; --highlight: #e94560; --user-bg: #0f3460;
      --bot-bg: #16213e; --border: #0f3460;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); }
    .container { max-width: 700px; margin: auto; padding: 20px; display: flex; flex-direction: column; height: 100vh; }
    .header { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--border); }
    .header h1 { font-size: 1.5rem; color: var(--highlight); }
    .model-select { background: var(--surface); color: var(--text); border: 1px solid var(--border); padding: 5px 10px; border-radius: 5px; }
    .messages { flex: 1; overflow-y: auto; padding: 10px 0; }
    .message { margin-bottom: 15px; padding: 10px 15px; border-radius: 15px; max-width: 80%; word-wrap: break-word; }
    .user-msg { background: var(--user-bg); margin-left: auto; border-bottom-right-radius: 0; }
    .bot-msg { background: var(--bot-bg); margin-right: auto; border-bottom-left-radius: 0; }
    .input-area { display: flex; gap: 10px; padding: 10px 0; border-top: 1px solid var(--border); }
    .input-area input { flex: 1; padding: 10px; border-radius: 20px; border: 1px solid var(--border); background: var(--surface); color: var(--text); }
    .input-area button { background: var(--highlight); border: none; border-radius: 20px; padding: 10px 20px; color: white; cursor: pointer; }
    .status { font-size: 0.8rem; color: #888; text-align: center; margin: 5px 0; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Claude Chat</h1>
      <select id="modelSelect" class="model-select"></select>
    </div>
    <div class="messages" id="messages"></div>
    <div class="status" id="status">Ready</div>
    <div class="input-area">
      <input type="text" id="messageInput" placeholder="Type a message..." autocomplete="off">
      <button id="sendBtn">Send</button>
    </div>
  </div>
  <script>
    const tg = window.Telegram.WebApp;
    tg.expand();

    const apiBase = '/api';
    let userId = null;
    let currentModel = 'claude-sonnet-5';

    async function loadModels() {
      const resp = await fetch(apiBase + '/models');
      const models = await resp.json();
      const select = document.getElementById('modelSelect');
      models.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.id;
        opt.text = m.name;
        select.appendChild(opt);
      });
      select.value = currentModel;
      select.addEventListener('change', async () => {
        currentModel = select.value;
        await fetch(apiBase + '/set_model', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({user_id: userId, model_id: currentModel})
        });
      });
    }

    function addMessage(text, sender) {
      const msgDiv = document.createElement('div');
      msgDiv.className = 'message ' + (sender === 'user' ? 'user-msg' : 'bot-msg');
      msgDiv.textContent = text;
      document.getElementById('messages').appendChild(msgDiv);
      document.getElementById('messages').scrollTop = document.getElementById('messages').scrollHeight;
    }

    async function sendMessage() {
      const input = document.getElementById('messageInput');
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      addMessage(text, 'user');
      document.getElementById('status').textContent = 'Thinking...';
      const resp = await fetch(apiBase + '/send_message', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({user_id: userId, text: text})
      });
      const data = await resp.json();
      document.getElementById('status').textContent = 'Ready';
      if (data.reply) {
        addMessage(data.reply, 'bot');
      } else {
        addMessage('❌ No reply', 'bot');
      }
    }

    async function init() {
      try {
        const resp = await fetch(apiBase + '/init', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({initData: tg.initData})
        });
        const data = await resp.json();
        if (data.user_id) {
          userId = data.user_id;
          document.getElementById('status').textContent = 'Connected';
          await loadModels();
        } else {
          document.getElementById('status').textContent = 'Auth failed';
        }
      } catch (e) {
        document.getElementById('status').textContent = 'Error connecting';
      }
    }

    document.getElementById('sendBtn').addEventListener('click', sendMessage);
    document.getElementById('messageInput').addEventListener('keypress', (e) => {
      if (e.key === 'Enter') sendMessage();
    });

    init();
  </script>
</body>
</html>
"""

# ============================================================
# HELPERS (email, OTP, Syntx APIs)
# ============================================================
async def create_email() -> tuple[str, str]:
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

async def upload_image_api(token: str, chat_uuid: str, file_path: str) -> Optional[str]:
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
        logger.error(f"Image upload failed: {e}")
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

# ============================================================
# ACCESS CONTROL
# ============================================================
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
    """Create a new Syntx.ai session for user. Returns error message or None."""
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

# ============================================================
# TELEGRAM BOT HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await check_access(user.id):
        await update.message.reply_text("⛔ Access denied. Contact admin.")
        return
    err = await new_session(user.id)
    if err:
        await update.message.reply_text(err)
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Open Web Chat", web_app=WebAppInfo(url="https://your-domain.com/mini-app"))]
    ])
    await update.message.reply_text(
        "✅ Session started! Chat here or use the Mini App.",
        reply_markup=keyboard
    )

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
        "Send a text message or a photo with optional caption.\n"
        "Use the Web App for a richer experience."
    )

# ----- Admin Commands -----
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

# ----- Message handlers (text and photo) -----
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
    await context.bot.send_chat_action(chat_id=user.id, action=constants.ChatAction.TYPING)
    msg_id = await send_message_api(session['token'], session['chat_uuid'], objects)
    if not msg_id:
        await update.message.reply_text("❌ Failed to send message.")
        return
    reply = await fetch_reply_api(session['token'], session['chat_uuid'], msg_id)
    if reply:
        # split long messages if needed
        for i in range(0, len(reply), 4000):
            await update.message.reply_text(reply[i:i+4000])
        session['message_count'] += 1
    else:
        await update.message.reply_text("❌ No reply received.")

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

    # Download photo
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        await photo_file.download_to_memory()
        tmp.write(photo_file.download_as_bytearray())
        tmp_path = tmp.name

    await context.bot.send_chat_action(chat_id=user.id, action=constants.ChatAction.UPLOAD_PHOTO)

    # Upload to Syntx
    img_url = await upload_image_api(session['token'], session['chat_uuid'], tmp_path)
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

    msg_id = await send_message_api(session['token'], session['chat_uuid'], objects)
    if not msg_id:
        await update.message.reply_text("❌ Failed to send message.")
        return
    reply = await fetch_reply_api(session['token'], session['chat_uuid'], msg_id)
    if reply:
        for i in range(0, len(reply), 4000):
            await update.message.reply_text(reply[i:i+4000])
        session['message_count'] += 1
    else:
        await update.message.reply_text("❌ No reply received.")

# ============================================================
# FASTAPI APP (Mini App backend)
# ============================================================
fastapi_app = FastAPI()

def verify_init_data(init_data: str) -> Optional[int]:
    """Verify Telegram WebApp initData, return user_id if valid."""
    try:
        parsed = dict(pair.split('=') for pair in init_data.split('&'))
        received_hash = parsed.pop('hash', '')
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if computed_hash != received_hash:
            return None
        user_data = json.loads(unquote(parsed.get('user', '{}')))
        return user_data.get('id')
    except Exception:
        return None

@fastapi_app.post("/api/init")
async def init_endpoint(request: Request):
    data = await request.json()
    init_data = data.get('initData')
    uid = verify_init_data(init_data)
    if not uid:
        raise HTTPException(status_code=403, detail="Invalid initData")
    if uid not in USER_SESSIONS:
        raise HTTPException(status_code=404, detail="No active session. Please /start in bot.")
    return {"user_id": uid}

@fastapi_app.get("/api/models")
async def get_models():
    return [{"id": mid, "name": mname} for mid, mname in MODELS]

@fastapi_app.post("/api/set_model")
async def set_model(request: Request):
    data = await request.json()
    uid = data.get('user_id')
    model_id = data.get('model_id')
    if uid not in USER_SESSIONS:
        raise HTTPException(status_code=404)
    session = USER_SESSIONS[uid]
    session['model_id'] = model_id
    for mid, name in MODELS:
        if mid == model_id:
            session['model_name'] = name
            break
    return {"status": "ok"}

@fastapi_app.post("/api/send_message")
async def send_message_endpoint(request: Request):
    data = await request.json()
    uid = data.get('user_id')
    text = data.get('text')
    if uid not in USER_SESSIONS:
        raise HTTPException(status_code=404)
    session = USER_SESSIONS[uid]
    objects = [{
        "object_type": "text",
        "object_url": None,
        "object_text": text,
        "model_type": session['model_id']
    }]
    msg_id = await send_message_api(session['token'], session['chat_uuid'], objects)
    if not msg_id:
        return {"reply": None, "error": "Send failed"}
    reply = await fetch_reply_api(session['token'], session['chat_uuid'], msg_id)
    return {"reply": reply}

@fastapi_app.get("/mini-app", response_class=HTMLResponse)
async def serve_mini_app():
    return MINI_APP_HTML

# ============================================================
# BACKGROUND TASK: expire old users
# ============================================================
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

# ============================================================
# MAIN
# ============================================================
async def main():
    # Telegram bot application
    app = Application.builder().token(BOT_TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("model", model_cmd))
    app.add_handler(CommandHandler("new", new_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    # Admin commands
    app.add_handler(CommandHandler("addid", addid_cmd))
    app.add_handler(CommandHandler("removeuser", removeuser_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    # Message handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Start bot polling and FastAPI server concurrently
    await app.initialize()
    await app.start()
    asyncio.create_task(app.updater.start_polling())

    config = uvicorn.Config(fastapi_app, host="0.0.0.0", port=8000)
    server = uvicorn.Server(config)
    await asyncio.gather(server.serve(), expire_loop())

if __name__ == "__main__":
    asyncio.run(main())
