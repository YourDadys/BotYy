# bot.py
# Telegram Referral Bot (Polling Mode + Dummy Web)
# -----------------------------------------------
# ✅ No webhook needed
# ✅ Works 24x7 on Koyeb / Render
# ✅ Dummy web on port 8080 (for uptime ping)
# -----------------------------------------------

import os
import time
import uuid
import threading
import sqlite3
import requests
from flask import Flask, render_template_string

# ---------- CONFIG ----------
BOT_TOKEN = "8536505559:AAHtlNxU0XS2FW4yw--0JXNA7OrZqkI4_W8"  # ⚠️ Replace with your bot token
PORT = 8080
THRESHOLD = 5  # referrals needed for reward

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_PATH = "referral.db"

app = Flask(__name__)

# ---------- Database ----------
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            started_from INTEGER,
            started_at INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER,
            ts INTEGER,
            UNIQUE(referrer_id, referred_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rewards (
            user_id INTEGER PRIMARY KEY,
            rewarded INTEGER DEFAULT 0,
            code TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ---------- Telegram Helpers ----------
def tg_request(method, payload=None):
    try:
        url = f"{API_URL}/{method}"
        if payload:
            r = requests.post(url, json=payload, timeout=10)
        else:
            r = requests.get(url, timeout=10)
        return r.json()
    except Exception as e:
        print("Telegram error:", e)
        return None

def tg_send(chat_id, text):
    tg_request("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})

def get_bot_username():
    res = tg_request("getMe")
    if res and res.get("ok"):
        return res["result"]["username"]
    return "unknown_bot"

# ---------- Referral System ----------
def handle_start(user, ref_param):
    uid = user["id"]
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    if cur.fetchone():
        conn.close()
        return

    referrer_id = None
    if ref_param and ref_param.isdigit():
        referrer_id = int(ref_param)
        if referrer_id == uid:
            referrer_id = None

    ts = int(time.time())
    cur.execute("INSERT INTO users (user_id, username, first_name, started_from, started_at) VALUES (?,?,?,?,?)",
                (uid, user.get("username"), user.get("first_name"), referrer_id, ts))
    conn.commit()

    # record referral
    if referrer_id:
        cur.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_id, ts) VALUES (?,?,?)",
                    (referrer_id, uid, ts))
        conn.commit()
        cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id=?", (referrer_id,))
        count = cur.fetchone()["c"]

        # reward check
        if count >= THRESHOLD:
            cur.execute("SELECT * FROM rewards WHERE user_id=?", (referrer_id,))
            r = cur.fetchone()
            if not r or r["rewarded"] == 0:
                code = "REWARD-" + uuid.uuid4().hex[:8].upper()
                cur.execute("INSERT OR REPLACE INTO rewards (user_id, rewarded, code) VALUES (?,?,?)",
                            (referrer_id, 1, code))
                conn.commit()
                tg_send(referrer_id, f"🎉 Aapke {THRESHOLD} referrals complete hue!\nReward Code: <b>{code}</b>")
    conn.close()

def process_message(msg):
    text = msg.get("text", "")
    user = msg.get("from", {})
    uid = user.get("id")

    if not uid or not text:
        return

    if text.startswith("/start"):
        parts = text.split()
        ref_param = parts[1] if len(parts) > 1 else None
        handle_start(user, ref_param)

        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id=?", (uid,))
        count = cur.fetchone()["c"]
        cur.execute("SELECT * FROM rewards WHERE user_id=?", (uid,))
        rw = cur.fetchone()
        reward = rw["code"] if rw and rw["rewarded"] else None
        conn.close()

        bot_username = get_bot_username()
        ref_link = f"https://t.me/{bot_username}?start={uid}"

        msg_text = (
            f"👋 Hi <b>{user.get('first_name','')}</b>!\n\n"
            f"🔗 Referral Link:\n{ref_link}\n\n"
            f"👥 Referrals: {count}/{THRESHOLD}\n"
            f"🎁 Reward: {'✅ ' + reward if reward else '❌ Not yet'}\n\n"
            "Invite 5 friends to unlock your reward!"
        )
        tg_send(uid, msg_text)

# ---------- Polling Thread ----------
def polling_loop():
    print("🤖 Polling started...")
    offset = None
    while True:
        try:
            updates = tg_request("getUpdates", {"offset": offset, "timeout": 30})
            if updates and updates.get("ok"):
                for upd in updates["result"]:
                    offset = upd["update_id"] + 1
                    if "message" in upd:
                        process_message(upd["message"])
        except Exception as e:
            print("Polling error:", e)
        time.sleep(2)

# ---------- Dummy Web ----------
@app.route("/")
def home():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT u.user_id, u.username, COUNT(r.id) as count, COALESCE(w.code,'-') as code
        FROM users u
        LEFT JOIN referrals r ON u.user_id=r.referrer_id
        LEFT JOIN rewards w ON u.user_id=w.user_id
        GROUP BY u.user_id
        ORDER BY count DESC LIMIT 20
    """)
    rows = cur.fetchall()
    conn.close()

    html = """
    <html><head><title>Referral Bot</title></head><body>
    <h1>Referral Bot Dummy Web</h1>
    <p>This is a fake dashboard to keep your bot alive (port 8080).</p>
    <table border=1 cellpadding=5>
    <tr><th>User ID</th><th>Username</th><th>Referrals</th><th>Reward</th></tr>
    {% for r in rows %}
    <tr><td>{{r.user_id}}</td><td>{{r.username}}</td><td>{{r.count}}</td><td>{{r.code}}</td></tr>
    {% endfor %}
    </table>
    <p style="color:gray;font-size:12px;">Running 24x7 - Polling Mode</p>
    </body></html>
    """
    return render_template_string(html, rows=rows)

# ---------- Run ----------
if __name__ == "__main__":
    threading.Thread(target=polling_loop, daemon=True).start()
    print("✅ Bot & dummy web running on port", PORT)
    app.run(host="0.0.0.0", port=PORT)
