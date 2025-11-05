# bot.py
# Referral + Private-channel verification bot (Koyeb-ready)
import os
import sqlite3
import time
import uuid
import traceback
import threading

from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ------------------------
# CONFIG FROM ENV
# ------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")                      
CHANNEL_INVITE = os.getenv("CHANNEL_INVITE")            
CHANNEL_ID = os.getenv("CHANNEL_ID")                    
API_ID = os.getenv("API_ID")                            
API_HASH = os.getenv("API_HASH")                        
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise SystemExit("Missing BOT_TOKEN environment variable")
if not CHANNEL_INVITE:
    raise SystemExit("Missing CHANNEL_INVITE environment variable")

if CHANNEL_ID is not None and CHANNEL_ID.strip() == "":
    CHANNEL_ID = None

# ------------------------
# DB setup
# ------------------------
DB_PATH = "referral.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    referrer_id INTEGER,
    verified INTEGER DEFAULT 0,
    started_at INTEGER
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER,
    referred_id INTEGER,
    ts INTEGER,
    UNIQUE(referrer_id, referred_id)
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS rewards (
    user_id INTEGER PRIMARY KEY,
    rewards INTEGER DEFAULT 0
);
""")
conn.commit()

# ------------------------
# Pyrogram bot client
# ------------------------
app = Client("ref-bot", bot_token=BOT_TOKEN)

# Optional: user_client
user_client = None
use_user_client = False
if API_ID and API_HASH:
    try:
        user_client = Client("ref-user", api_id=int(API_ID), api_hash=API_HASH)
        use_user_client = True
    except Exception as e:
        print("Could not initialize user client (API_ID/API_HASH invalid?):", e)
        user_client = None
        use_user_client = False

# ------------------------
# Flask health check (Koyeb TCP)
# ------------------------
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Referral Bot is running ✅"

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

# ------------------------
# Helpers
# ------------------------
def gen_reward_code():
    return "REWARD-" + uuid.uuid4().hex[:8].upper()

def get_bot_username_sync():
    me = app.get_me()
    return me.username if me and me.username else "bot"

def register_user(user_obj, ref_param=None):
    uid = user_obj.id
    username = user_obj.username
    first_name = user_obj.first_name

    cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    existing = cur.fetchone()
    if existing:
        cur.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (username, first_name, uid))
        conn.commit()
        return False

    ref_id = None
    if ref_param:
        if isinstance(ref_param, str) and ref_param.startswith("ref_"):
            ref_param = ref_param.replace("ref_", "")
        if str(ref_param).isdigit():
            candidate = int(ref_param)
            if candidate != uid:
                cur.execute("SELECT 1 FROM users WHERE user_id=?", (candidate,))
                if cur.fetchone():
                    ref_id = candidate
                else:
                    ref_id = candidate

    ts = int(time.time())
    cur.execute("INSERT INTO users (user_id, username, first_name, referrer_id, verified, started_at) VALUES (?,?,?,?,0,?)",
                (uid, username, first_name, ref_id, ts))
    conn.commit()

    if ref_id:
        try:
            cur.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_id, ts) VALUES (?,?,?)",
                        (ref_id, uid, ts))
            conn.commit()
            try:
                app.send_message(ref_id,
                                 f"🎯 Your referral joined: {first_name if first_name else username} (id: {uid})")
            except Exception:
                pass

            cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id=?", (ref_id,))
            c = cur.fetchone()["c"]
            if c >= 5:
                cur.execute("SELECT rewards FROM rewards WHERE user_id=?", (ref_id,))
                row = cur.fetchone()
                if row:
                    newr = row["rewards"] + 1
                    cur.execute("UPDATE rewards SET rewards=? WHERE user_id=?", (newr, ref_id))
                else:
                    cur.execute("INSERT INTO rewards (user_id, rewards) VALUES (?,?)", (ref_id, 1))
                conn.commit()
                try:
                    app.send_message(ref_id, f"🎉 Congratulations — you reached 5 referrals and earned 1 reward!")
                except Exception:
                    pass
        except Exception as e:
            print("Referral insert error:", e)

    return True

# ------------------------
# Keyboards
# ------------------------
def keyboard_before_verify():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 Join Channel", url=CHANNEL_INVITE)],
            [InlineKeyboardButton("✅ Verify (I sent request)", callback_data="verify")]
        ]
    )

def keyboard_after_verify(user_id):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👥 Check My Referrals", callback_data="check_refs")],
            [InlineKeyboardButton("🎁 Claim Reward (5 refs = 1)", callback_data="claim_reward")]
        ]
    )

# ------------------------
# Check membership / pending requests
# ------------------------
def bot_check_membership(user_id):
    try:
        chat_identifier = int(CHANNEL_ID) if CHANNEL_ID else CHANNEL_INVITE
        member = app.get_chat_member(chat_identifier, int(user_id))
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        print("bot_check_membership error:", e)
        return False

async def userclient_check_request(user_id):
    try:
        chat = int(CHANNEL_ID) if CHANNEL_ID else (await user_client.get_chat(CHANNEL_INVITE)).id
    except Exception as e:
        print("Could not resolve channel invite via user_client:", e)
        return "not"

    try:
        member = await user_client.get_chat_member(chat, int(user_id))
        if member and member.status in ("member", "administrator", "creator"):
            return "joined"
    except Exception:
        pass

    try:
        reqs = await user_client.get_chat_join_requests(chat)
        if reqs:
            for r in reqs:
                if getattr(r, "user", None) and r.user.id == user_id:
                    return "pending"
    except Exception as e:
        print("get_chat_join_requests error or not available:", e)

    return "not"

# ------------------------
# Handlers
# ------------------------
@app.on_message(filters.command("start"))
def on_start(client, message):
    arg = message.text.split()[1] if len(message.text.split()) > 1 else None
    register_user(message.from_user, arg)
    username = get_bot_username_sync()
    ref_link = f"https://t.me/{username}?start={message.from_user.id}"
    text = ("👋 Welcome!\n\n"
            "Please join our private channel using the button below.\n"
            "After you send a join request, come back and press Verify.\n\n"
            f"🔗 Your referral link:\n{ref_link}\n\n")
    message.reply(text, reply_markup=keyboard_before_verify())

@app.on_callback_query(filters.regex("^verify$"))
def on_verify(client, callback_query):
    uid = callback_query.from_user.id
    if use_user_client and user_client:
        try:
            res = user_client.loop.run_until_complete(userclient_check_request(uid))
            if res == "joined":
                cur.execute("UPDATE users SET verified=1 WHERE user_id=?", (uid,))
                conn.commit()
                callback_query.message.edit_text("✅ Verified: you are a channel member.", reply_markup=keyboard_after_verify(uid))
            elif res == "pending":
                callback_query.answer("✅ Request found (pending). Verification will succeed once admin approves.", show_alert=True)
            else:
                callback_query.answer("❌ No request found. Please open channel link and tap 'Join', then press Verify.", show_alert=True)
        except Exception as e:
            print("user_client verify error:", e)
            traceback.print_exc()
            callback_query.answer("❌ Could not check join requests (user client error).", show_alert=True)
    else:
        if bot_check_membership(uid):
            cur.execute("UPDATE users SET verified=1 WHERE user_id=?", (uid,))
            conn.commit()
            callback_query.message.edit_text("✅ Verified: you are a channel member.", reply_markup=keyboard_after_verify(uid))
        else:
            callback_query.answer(
                "❌ Could not detect join. If you have only sent a join request (pending) the bot cannot detect it without API_ID/API_HASH.", 
                show_alert=True
            )

@app.on_callback_query(filters.regex("^check_refs$"))
def on_check_refs(client, callback_query):
    uid = callback_query.from_user.id
    cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id=?", (uid,))
    count = cur.fetchone()["c"]
    cur.execute("SELECT rewards FROM rewards WHERE user_id=?", (uid,))
    row = cur.fetchone()
    rewards = row["rewards"] if row else 0
    callback_query.answer()
    callback_query.message.reply_text(f"👥 Your referrals: {count}\n🎁 Rewards: {rewards}")

@app.on_callback_query(filters.regex("^claim_reward$"))
def on_claim_reward(client, callback_query):
    uid = callback_query.from_user.id
    cur.execute("SELECT rewards FROM rewards WHERE user_id=?", (uid,))
    row = cur.fetchone()
    if not row or row["rewards"] <= 0:
        callback_query.answer("❌ You have no rewards to claim.", show_alert=True)
        return
    code = gen_reward_code()
    newr = row["rewards"] - 1
    cur.execute("UPDATE rewards SET rewards=? WHERE user_id=?", (newr, uid))
    conn.commit()
    try:
        app.send_message(uid, f"🎁 Reward claimed! Your code: <b>{code}</b>")
    except Exception:
        pass
    callback_query.answer("✅ Reward claimed — check your messages.", show_alert=True)

# ------------------------
# Start everything
# ------------------------
def start_both():
    t = threading.Thread(target=run_flask)
    t.start()

    if use_user_client and user_client:
        try:
            print("Starting user_client (MTProto) for pending-request checks...")
            user_client.start()
            print("user_client started.")
        except Exception as e:
            print("Could not start user_client:", e)

    print("Starting bot (bot client)...")
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    start_both()
