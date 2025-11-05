# bot.py
# Pyrogram referral bot (polling mode) — single-file
# Usage: python3 bot.py
# NOTE: If you will push to public repo, replace BOT_TOKEN with a placeholder first.

import sqlite3
import time
import uuid
import os
from pyrogram import Client, filters
from pyrogram.types import Message

# ---------- CONFIG (edit here) ----------
API_ID = 26343513               # replace with your api_id
API_HASH = "12712c972da9bdf5225f63a628e1b7a3"  # replace with your api_hash
BOT_TOKEN = "8536505559:AAHtlNxU0XS2FW4yw--0JXNA7OrZqkI4_W8"  # replace with your bot token (from BotFather)
ADMIN_ID = 6743586157           # admin user id (change if needed)
THRESHOLD = 5                   # referrals needed to get reward
DB_PATH = "referral.db"
# ---------------------------------------

if BOT_TOKEN.startswith("PUT_YOUR"):
    print("WARNING: BOT_TOKEN is placeholder. Replace BOT_TOKEN in bot.py before running.")
if not API_ID or not API_HASH:
    print("WARNING: API_ID/API_HASH may be invalid or missing.")

# ---------- DB helpers ----------
def get_db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        started_from INTEGER,
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
        rewarded INTEGER DEFAULT 0,
        code TEXT
    );
    """)
    conn.commit()
    conn.close()

init_db()

# ---------- Utility ----------
def gen_code():
    return "REWARD-" + uuid.uuid4().hex[:10].upper()

def get_bot_username(app: Client):
    me = app.get_me()
    return me.username if me and me.username else "your_bot"

# ---------- Bot ----------
app = Client("referral-bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# When a new user clicks deep link: /start <param>
@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message):
    args = message.text.split()
    uid = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name or ""

    # store user if new
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (uid,))
    if cur.fetchone():
        # existing user — update name/username if changed
        cur.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (username, first_name, uid))
        conn.commit()
    else:
        # new user; may have started from ref param
        started_from = None
        if len(args) > 1:
            param = args[1]
            # support verify_ style tokens or numeric referrer id
            if param.startswith("verify_"):
                # not used for referrals here, skip
                started_from = None
            else:
                if param.isdigit():
                    started_from = int(param)
                    if started_from == uid:
                        started_from = None
        ts = int(time.time())
        cur.execute("INSERT INTO users (user_id, username, first_name, started_from, started_at) VALUES (?,?,?,?,?)",
                    (uid, username, first_name, started_from, ts))
        conn.commit()

        # if there is a valid referrer, register referral
        if started_from:
            # ensure referrer exists in users table
            cur.execute("SELECT * FROM users WHERE user_id = ?", (started_from,))
            if cur.fetchone():
                cur.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_id, ts) VALUES (?,?,?)",
                            (started_from, uid, ts))
                conn.commit()
                # check count and award if threshold reached
                cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id = ?", (started_from,))
                count = cur.fetchone()["c"]
                if count >= THRESHOLD:
                    cur.execute("SELECT * FROM rewards WHERE user_id = ?", (started_from,))
                    r = cur.fetchone()
                    if not r or r["rewarded"] == 0:
                        code = gen_code()
                        cur.execute("INSERT OR REPLACE INTO rewards (user_id, rewarded, code) VALUES (?,?,?)",
                                    (started_from, 1, code))
                        conn.commit()
                        try:
                            await client.send_message(started_from, f"🎉 Congratulations! You reached {THRESHOLD} referrals.\nYour reward code: <b>{code}</b>")
                        except Exception:
                            pass

    # reply to new or existing user with their referral link & status
    cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id = ?", (uid,))
    row = cur.fetchone()
    count = row["c"] if row else 0
    cur.execute("SELECT * FROM rewards WHERE user_id = ?", (uid,))
    rrow = cur.fetchone()
    reward_msg = f"✅ Reward: {rrow['code']}" if rrow and rrow["rewarded"] else f"❌ No reward yet — get {THRESHOLD} referrals"
    bot_username = (await client.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={uid}"
    await message.reply_text(
        f"👋 Hi {first_name}!\n\n"
        f"🔗 Your referral link:\n{ref_link}\n\n"
        f"👥 Referrals: {count}/{THRESHOLD}\n"
        f"{reward_msg}\n\n"
        "Share your link. Unique users who click and start via your link count as referrals."
    )
    conn.close()

# Admin: get stats
@app.on_message(filters.command("stats") & filters.user(ADMIN_ID))
async def stats_cmd(client: Client, message: Message):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as u FROM users")
    users = cur.fetchone()["u"]
    cur.execute("SELECT COUNT(*) as r FROM referrals")
    refs = cur.fetchone()["r"]
    cur.execute("SELECT COUNT(*) as w FROM rewards WHERE rewarded=1")
    rewarded = cur.fetchone()["w"]
    conn.close()
    await message.reply_text(f"📊 Stats:\nUsers: {users}\nReferrals recorded: {refs}\nRewards granted: {rewarded}")

# Admin: grant reward manually
@app.on_message(filters.command("grant") & filters.user(ADMIN_ID))
async def grant_cmd(client: Client, message: Message):
    # usage: /grant <user_id>
    args = message.text.split()
    if len(args) < 2:
        await message.reply_text("Usage: /grant <user_id>")
        return
    try:
        uid = int(args[1])
    except ValueError:
        await message.reply_text("Invalid user_id.")
        return
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM rewards WHERE user_id = ?", (uid,))
    r = cur.fetchone()
    if r and r["rewarded"]:
        await message.reply_text("User already rewarded.")
        conn.close()
        return
    code = gen_code()
    cur.execute("INSERT OR REPLACE INTO rewards (user_id, rewarded, code) VALUES (?,?,?)", (uid, 1, code))
    conn.commit()
    conn.close()
    try:
        await client.send_message(uid, f"🎁 Admin granted a reward: <b>{code}</b>")
    except Exception:
        pass
    await message.reply_text(f"Granted reward to {uid}: {code}")

# Optional: command to view own referrals (simple)
@app.on_message(filters.command("myrefs"))
async def myrefs(client: Client, message: Message):
    uid = message.from_user.id
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id = ?", (uid,))
    c = cur.fetchone()["c"]
    cur.execute("SELECT * FROM rewards WHERE user_id = ?", (uid,))
    r = cur.fetchone()
    reward = r["code"] if r and r["rewarded"] else None
    conn.close()
    await message.reply_text(f"👥 You have {c} referrals.\nReward: {reward if reward else 'Not yet'}")

# Run the bot
if __name__ == "__main__":
    print("✅ Referral bot (Pyrogram) starting...")
    app.run()
