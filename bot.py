import os
import re
import ast
import operator
import random
import time
import threading
import tempfile
import logging
from collections import deque
from difflib import SequenceMatcher
from logging.handlers import RotatingFileHandler

from cachetools import TTLCache
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask
from gTTS import gTTS
from pydub import AudioSegment
import speech_recognition as sr

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

# ============================================================
# CONFIG — SECURE ENVIRONMENT LOADERS
# ============================================================

def get_env(key, default=""):
    return os.getenv(key, default).strip()

BOT_TOKEN = get_env("BOT_TOKEN", "7042790112:AAGM4k5zIKBabxDJ35Pnw17o-N9Sf9hCYUU")
AI_API_KEY = get_env("AI_API_KEY", "sk-5d02b9dcd5a2caf79a7e9d4d97b490915cec2b51fb2be11b1662a42768505df5")
AI_BASE_URL = get_env("AI_BASE_URL", "https://api.mwapi.dev/v1")
AI_MODEL = get_env("AI_MODEL", "claude-sonnet-4-6")

SUPABASE_URL = get_env("SUPABASE_URL", "https://necofapukgwalgviqxue.supabase.co")
SUPABASE_KEY = get_env("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5lY29mYXB1a2d3YWxndmlxeHVlIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODY1ODk4NjIsImV4cCI6MjEwMjE2NTg2Mn0.mihtDHVKHeiacEC1Q8FnXtCdvFIYLMlRRApyKE2qcj8")

ADMIN_ID = int(get_env("ADMIN_ID", "8459158216"))
PORT = int(get_env("PORT", "10000"))

ENABLE_TTS = True
RESPOND_IN_GROUPS = True
AI_TIMEOUT = 18
AI_RETRIES = 2

# Check essential configs
if not BOT_TOKEN or not AI_API_KEY:
    raise RuntimeError("Critical API keys missing in environment configuration.")

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    handlers=[
        RotatingFileHandler("bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"),
        logging.StreamHandler(),
    ],
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("venu")

# ============================================================
# HTTP SESSION
# ============================================================

http = requests.Session()
retry_strategy = Retry(
    total=2,
    connect=2,
    read=2,
    backoff_factor=0.4,
    status_forcelist=[429, 502, 503, 504],
    allowed_methods=frozenset(["GET", "POST", "PATCH", "DELETE"]),
    raise_on_status=False,
)

http.mount("https://", HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=retry_strategy))
http.mount("http://", HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=2))

# ============================================================
# TELEGRAM SETUP
# ============================================================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None, threaded=True, num_threads=8)
BOT_ID = None
BOT_USERNAME = ""

try:
    me = bot.get_me()
    BOT_ID = me.id
    BOT_USERNAME = (me.username or "").lower()
    logger.info("Telegram connected: @%s (%s)", BOT_USERNAME, BOT_ID)
except Exception:
    logger.exception("Telegram get_me failed")

# ============================================================
# DATABASE (SUPABASE)
# ============================================================

class DB:
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def request(self, method, endpoint, payload=None, timeout=7):
        try:
            endpoint = endpoint.lstrip("/")
            url = f"{self.url}/rest/v1/{endpoint}"
            headers = self.headers.copy()
            method = method.upper()

            if method == "GET":
                response = http.get(url, headers=headers, timeout=timeout)
            elif method == "POST":
                headers["Prefer"] = "return=minimal"
                response = http.post(url, headers=headers, json=payload, timeout=timeout)
            elif method == "PATCH":
                response = http.patch(url, headers=headers, json=payload, timeout=timeout)
            elif method == "DELETE":
                response = http.delete(url, headers=headers, timeout=timeout)
            else:
                return None

            if response.status_code == 404:
                return None

            response.raise_for_status()
            return response.json() if response.text else None
        except Exception:
            logger.exception("DB %s %s failed", method, endpoint)
            return None

db = DB(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# GLOBAL STATE & MEMORY
# ============================================================

lock = threading.RLock()
memory = TTLCache(maxsize=2000, ttl=1800)
registered = TTLCache(maxsize=10000, ttl=86400)
recent_replies = {}
last_msg = {}
name_time = {}
games = {}
tts_users = set()
activity = {}

app = Flask(__name__)

@app.route("/")
def home():
    return "🤖 Venu AI Online - Sharp Female Persona Edition"

@app.route("/health")
def health():
    return {"status": "online", "bot_id": BOT_ID, "username": BOT_USERNAME, "model": AI_MODEL}

def run_flask():
    try:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    except Exception:
        logger.exception("Flask server stopped")

# ============================================================
# USER PROFILE & MEMORY LOGIC
# ============================================================

def default_profile(uid, name="Dost"):
    return {
        "user_id": uid,
        "name": name or "Dost",
        "age": "Not specified",
        "favorite_game": "Not specified",
        "favorite_movie": "Not specified",
        "language": "Hinglish",
        "relationship_status": "Not specified",
        "hobbies": "Not specified",
        "current_mood": "Chill",
        "emotional_momentum": "Stable",
    }

def register_user(uid, username, first_name):
    with lock:
        if uid in registered:
            return
        registered[uid] = True

    def worker():
        try:
            response = http.post(
                f"{db.url}/rest/v1/users",
                headers={**db.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                json={"user_id": uid, "username": username, "first_name": first_name, "is_verified": True},
                timeout=5,
            )
            response.raise_for_status()
        except Exception:
            logger.exception("User registration failed")

    threading.Thread(target=worker, daemon=True).start()

def get_memory(uid, name="Dost"):
    with lock:
        cached = memory.get(uid)
        if cached:
            return cached

    profile_rows = db.request("GET", f"user_profiles?user_id=eq.{uid}&limit=1") or []
    profile = profile_rows[0] if profile_rows else default_profile(uid, name)

    if not profile_rows:
        db.request("POST", "user_profiles", profile)

    summary_rows = db.request("GET", f"conversation_summary?user_id=eq.{uid}&limit=1") or []
    summary = summary_rows[0].get("summary", "Cool bestie connection.") if summary_rows else "Cool bestie connection."

    rows = db.request("GET", f"messages?user_id=eq.{uid}&order=created_at.desc&limit=12") or []
    history = []
    for row in reversed(rows):
        role = row.get("role")
        content = row.get("content")
        if role in {"user", "assistant"} and content:
            history.append({"role": role, "content": str(content)})

    packet = {"profile": profile, "summary": summary, "history": history[-12:]}
    with lock:
        memory[uid] = packet
    return packet

def save_message(uid, role, text):
    if not text:
        return
    text = str(text)
    with lock:
        packet = memory.get(uid)
        if packet:
            packet["history"].append({"role": role, "content": text})
            packet["history"] = packet["history"][-12:]

    def worker():
        db.request("POST", "messages", {"user_id": uid, "role": role, "content": text})

    threading.Thread(target=worker, daemon=True).start()

def update_profile(uid, field, value):
    allowed = {"name", "age", "favorite_game", "favorite_movie", "language", "relationship_status", "hobbies", "current_mood", "emotional_momentum"}
    if field not in allowed:
        return
    with lock:
        if uid in memory:
            memory[uid]["profile"][field] = value

    def worker():
        db.request("PATCH", f"user_profiles?user_id=eq.{uid}", {field: value})

    threading.Thread(target=worker, daemon=True).start()

def clear_memory(uid):
    db.request("DELETE", f"messages?user_id=eq.{uid}")
    with lock:
        memory.pop(uid, None)
        recent_replies.pop(uid, None)
        games.pop(uid, None)
        last_msg.pop(uid, None)
        tts_users.discard(uid)

def daily(uid, game=False):
    def worker():
        try:
            today = time.strftime("%Y-%m-%d")
            rows = db.request("GET", f"daily_stats?user_id=eq.{uid}&date=eq.{today}&limit=1") or []
            if rows:
                row = rows[0]
                msgs = int(row.get("messages_sent", 0) or 0)
                gms = int(row.get("games_played", 0) or 0)
                db.request("PATCH", f"daily_stats?user_id=eq.{uid}&date=eq.{today}", {"messages_sent": msgs + (0 if game else 1), "games_played": gms + (1 if game else 0)})
            else:
                db.request("POST", "daily_stats", {"user_id": uid, "date": today, "messages_sent": 0 if game else 1, "games_played": 1 if game else 0})
        except Exception:
            logger.exception("Daily stats execution failed")

    threading.Thread(target=worker, daemon=True).start()

# ============================================================
# SAFE CALCULATOR
# ============================================================

OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.USub: operator.neg, ast.UAdd: operator.pos}

def safe_eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        return OPS[type(node.op)](safe_eval(node.left), safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
        return OPS[type(node.op)](safe_eval(node.operand))
    raise ValueError

def calc(expression):
    try:
        if not expression or len(expression) > 100 or not re.fullmatch(r"[0-9+*/().\-\s]+", expression):
            return None
        tree = ast.parse(expression, mode="eval")
        value = safe_eval(tree.body)
        return round(value, 8) if isinstance(value, float) and not value.is_integer() else value
    except Exception:
        return None

# ============================================================
# AI CORE — SHARP FEMALE PERSONA (VENU)
# ============================================================

def mood(text):
    text = text.lower()
    sad_words = ["sad", "dukhi", "udaas", "rona", "breakup", "depress", "tension", "pareshan", "lonely", "akela"]
    angry_words = ["gussa", "angry", "hate", "bakwas", "pagal"]
    happy_words = ["mast", "awesome", "excited", "party", "jeet", "won", "op", "nice"]

    if any(w in text for w in sad_words): return "supportive"
    if any(w in text for w in angry_words): return "calm"
    if any(w in text for w in happy_words): return "playful"
    return "chill"

def prompt(profile, summary, text):
    current_mood = mood(text)

    vibe_context = {
        "supportive": "Be empathetic, calm, caring yet realistic like a strong, dependable female friend.",
        "calm": "Stay composed, sharp, unbothered, and witty without raising anger.",
        "playful": "High energy, witty, sarcastic banter, sharp humor.",
        "chill": "Relaxed, effortless swagger, confident female bestie vibe."
    }[current_mood]

    return f"""
You are VENU: a sharp-minded, extremely smart, cool, confident girl with high swagger chatting on Telegram.

Identity & Rules:
1. Speak as a female friend (confident, witty, sharp, never submissive or over-apologetic).
2. Natural, modern Hinglish (urban, sharp, relatable).
3. NO boring standard AI replies like "Kaise ho?", "Main A.I hoon", or generic greetings.
4. Give situational counter-replies or witty call-outs instead of dry answers.
5. Max 1-2 short sentences. Direct and impact-driven.
6. Absolutely NO over-cringe, NO repetitive lines, NO forced romantic lines, NO corporate robotic speak.
7. Adapt tone: {vibe_context}

User Context:
Name: {profile.get("name", "Dost")}
Mood: {profile.get("current_mood", "Chill")}
Context: {summary}
""".strip()

def clean_reply(text):
    text = str(text or "").strip()
    text = text.replace("```", "")
    text = re.sub(r"^(Venu|Assistant|Bot)\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"[ \t]+", " ", text)
    parts = [part.strip() for part in re.split(r"(?<=[.!?।])\s+", text) if part.strip()]
    text = " ".join(parts[:2])
    if len(text) > 240:
        text = text[:237].rsplit(" ", 1)[0] + "…"
    return text

def similar(text, replies):
    if len(text) < 12:
        return False
    for old in replies:
        if old == text or (len(old) >= 12 and SequenceMatcher(None, old.lower(), text.lower()).ratio() >= 0.82):
            return True
    return False

def ai(uid, packet, text):
    messages = [{"role": "system", "content": prompt(packet["profile"], packet["summary"], text)}]
    history = packet.get("history", [])

    if isinstance(history, list):
        for item in history[-12:]:
            if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and item.get("content"):
                messages.append({"role": item["role"], "content": str(item["content"])})

    if not (messages[-1].get("role") == "user" and messages[-1].get("content") == text):
        messages.append({"role": "user", "content": text})

    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    last_error = None

    for attempt in range(AI_RETRIES):
        try:
            response = http.post(
                f"{AI_BASE_URL}/chat/completions",
                headers=headers,
                json={
                    "model": AI_MODEL,
                    "messages": messages,
                    "temperature": 0.82 if attempt == 0 else 0.92,
                    "max_tokens": 100,
                },
                timeout=(5, AI_TIMEOUT),
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices") or []

            if not choices:
                raise ValueError("AI returned no response choices")

            content = choices[0].get("message", {}).get("content", "")
            if isinstance(content, list):
                content = "".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in content)

            content = clean_reply(content)
            if not content:
                raise ValueError("Empty response string from AI")

            with lock:
                replies = recent_replies.setdefault(uid, deque(maxlen=8))
                duplicate = similar(content, replies)

            if duplicate and attempt == 0:
                messages[0]["content"] += "\nUse completely unique, unexpected wording for this response."
                continue

            with lock:
                replies.append(content)

            return content, mood(text)

        except Exception as error:
            last_error = error
            logger.warning("AI Attempt %s/%s Failed: %s", attempt + 1, AI_RETRIES, error)
            if attempt < AI_RETRIES - 1:
                time.sleep(0.4)

    logger.error("AI Error: %s", last_error)
    fallback = {
        "supportive": "Suno, tension mat lo. Kya scene hua detail me batao?",
        "calm": "Bhai shaanti. Point pe aao sidha.",
        "playful": "Acha? Aur kitne fekne ka plan hai aaj? 😉",
        "chill": "Clear bolo dost, dimag aur waqt dono kam hai 💅"
    }[mood(text)]

    with lock:
        recent_replies.setdefault(uid, deque(maxlen=8)).append(fallback)

    return fallback, mood(text)

# ============================================================
# ACTION TYPING HANDLER
# ============================================================

class Typing:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.stop_event = threading.Event()

    def start(self):
        self.send()
        threading.Thread(target=self.loop, daemon=True).start()

    def send(self):
        try:
            bot.send_chat_action(self.chat_id, "typing")
        except Exception:
            pass

    def loop(self):
        while not self.stop_event.wait(4):
            self.send()

    def close(self):
        self.stop_event.set()

# ============================================================
# KEYBOARDS & UI
# ============================================================

def main_kb():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("💬 Talk", callback_data="talk"),
        types.InlineKeyboardButton("🎮 Games", callback_data="games")
    )
    keyboard.add(
        types.InlineKeyboardButton("🧠 Memory", callback_data="memory"),
        types.InlineKeyboardButton("👤 Profile", callback_data="profile")
    )
    keyboard.add(
        types.InlineKeyboardButton("😂 Fun", callback_data="fun"),
        types.InlineKeyboardButton("📊 Stats", callback_data="stats")
    )
    keyboard.add(
        types.InlineKeyboardButton("🎙️ Voice", callback_data="voice"),
        types.InlineKeyboardButton("ℹ️ Help", callback_data="help")
    )
    keyboard.add(
        types.InlineKeyboardButton("➕ Add To Group", callback_data="group"),
        types.InlineKeyboardButton("🧹 Clear", callback_data="clear")
    )
    return keyboard

def game_kb():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("🎯 Guess Number", callback_data="guess"),
        types.InlineKeyboardButton("🎲 Truth or Dare", callback_data="tod")
    )
    keyboard.add(
        types.InlineKeyboardButton("🧩 Riddle", callback_data="riddle"),
        types.InlineKeyboardButton("🔥 Roast", callback_data="roast")
    )
    keyboard.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back"))
    return keyboard

# ============================================================
# FUN CONTENT & GAMES
# ============================================================

JOKES = [
    "Maine kaha 'Mera dimaag mat khao', bole 'Dieting pe hoon' 💀",
    "Gharwale bolte hain kuch bada karega, main tension badha deta hoon 😂",
    "Procrastination ka level ye hai ki kal ki tension aaj le raha hoon."
]

SHAYARI = [
    "Smartness in built hai, baaki sab aesthetic hype hai 🔥",
    "Pyaar, dosti sab sahi hai... par sleep schedule ka kya? 😴"
]

FUN_LINES = [
    "🔥 Apne bestie ko bina wajah 'I know your secret' likh ke bhej.",
    "🧠 Next 5 seconds me ek random roast socho.",
]

def joke(message): bot.reply_to(message, "😂 " + random.choice(JOKES))
def shayari(message): bot.reply_to(message, random.choice(SHAYARI))
def fun(message): bot.reply_to(message, random.choice(FUN_LINES) + "\n\n" + random.choice(JOKES))

def profile(message):
    packet = get_memory(message.from_user.id, message.from_user.first_name or "Dost")
    p = packet["profile"]
    bot.reply_to(message, f"👤 Venu Profile\n\n📌 Name: {p.get('name')}\n🎮 Game: {p.get('favorite_game')}\n🎬 Movie: {p.get('favorite_movie')}\n🧠 Mood: {p.get('current_mood')}")

def mem(message):
    packet = get_memory(message.from_user.id, message.from_user.first_name or "Dost")
    p = packet["profile"]
    bot.reply_to(message, f"🧠 Memory\n\nName: {p.get('name')}\nGame: {p.get('favorite_game')}\n\n💭 {packet.get('summary')}")

def stats(message):
    rows = db.request("GET", f"daily_stats?user_id=eq.{message.from_user.id}&order=date.desc&limit=7") or []
    tot_m = sum(int(r.get("messages_sent", 0) or 0) for r in rows)
    tot_g = sum(int(r.get("games_played", 0) or 0) for r in rows)
    bot.reply_to(message, f"📊 Stats\n\nMessages: {tot_m}\nGames: {tot_g}")

def help_(message):
    bot.reply_to(message, "ℹ️ Venu Smart Persona\n\n💬 Natural Sharp Chat\n🎮 Games: Guess / Truth-Dare / Riddle / Roast\n🎙️ /voice & /novoice\n🧠 /memory & /profile\n🧹 /clear")

def add_group(message):
    if not BOT_USERNAME:
        bot.reply_to(message, "Invite link unavailable.")
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Add Venu To Group", url=f"[https://t.me/](https://t.me/){BOT_USERNAME}?startgroup=true"))
    bot.reply_to(message, "Group select karo 😎", reply_markup=kb)

RIDDLES = [
    ("Tootne par awaaz nahi karti?", "khamoshi"),
    ("Jitna nikaalo utna bada hota hai?", "gaddha"),
    ("Keys hain par locks nahi, space hai par room nahi?", "keyboard")
]
TRUTHS = ["Sabse embarrassing memory?", "Unfiltered opinion on your bestie?", "Bina wajeh aakhri baar kab hase?"]
DARES = ["Apne status pe 'I am a secret agent' lagao.", "Kisi friend ko 'Mission Complete' bhej bina context."]
ROASTS = ["Tera logic dekh ke autocorrect bhi confuse ho jata hai 😂", "Overthinking 4K me, action 144p me 😭"]

def start_game(message, game_type):
    uid = message.from_user.id
    game = {"type": game_type, "created": time.time(), "attempts": 0}

    if game_type == "guess":
        game["secret"] = random.randint(1, 50)
        text = "🎯 Guess Number!\n1–50 ke beech ka number guess kar."
    elif game_type == "tod":
        text = "🎲 Truth or Dare?\nReply `truth` ya `dare`."
    elif game_type == "riddle":
        game["question"], game["answer"] = random.choice(RIDDLES)
        text = "🧩 " + game["question"]
    else:
        text = "🔥 Roast Battle!\nKuch likho, counter milega 😈"

    with lock:
        games[uid] = game
    bot.send_message(message.chat.id, text, reply_markup=game_kb())

def game_process(message, text):
    uid = message.from_user.id
    with lock:
        game = games.get(uid)

    if not game: return False
    game_type = game["type"]
    val = text.strip().lower()

    if val in {"cancel", "/cancel", "exit", "quit"}:
        with lock: games.pop(uid, None)
        bot.reply_to(message, "🎮 Game cancelled.")
        return True

    if game_type == "guess":
        try: num = int(val)
        except Exception:
            bot.reply_to(message, "🔢 Valid number bhejo.")
            return True
        game["attempts"] += 1
        if num == game["secret"]:
            att = game["attempts"]
            with lock: games.pop(uid, None)
            bot.reply_to(message, f"🎉 Spot on! {num} hi tha. Attempts: {att}")
        elif num < game["secret"]:
            bot.reply_to(message, "📈 High try kar.")
        else:
            bot.reply_to(message, "📉 Low try kar.")
        return True

    if game_type == "tod":
        if val not in {"truth", "dare"}:
            bot.reply_to(message, "Only 'truth' or 'dare'.")
            return True
        with lock: games.pop(uid, None)
        bot.reply_to(message, ("🧠 Truth: " if val == "truth" else "🔥 Dare: ") + random.choice(TRUTHS if val == "truth" else DARES))
        return True

    if game_type == "riddle":
        ans = game["answer"]
        if val == ans or ans in val or SequenceMatcher(None, val, ans).ratio() >= 0.72:
            with lock: games.pop(uid, None)
            bot.reply_to(message, "🎉 Correct! Brains inside 🔥")
        else:
            bot.reply_to(message, "❌ Incorrect. Try again!")
        return True

    if game_type == "roast":
        with lock: games.pop(uid, None)
        bot.reply_to(message, random.choice(ROASTS))
        return True

    return False

# ============================================================
# MESSAGING HELPER FUNCTIONS
# ============================================================

def group_ok(message):
    if not RESPOND_IN_GROUPS: return False
    if message.chat.type not in {"group", "supergroup"}: return True
    text = message.text or ""
    if text.startswith("/"): return True
    if BOT_USERNAME and f"@{BOT_USERNAME}" in text.lower(): return True
    reply = message.reply_to_message
    if reply and reply.from_user and BOT_ID and reply.from_user.id == BOT_ID: return True
    return False

def strip_mention(text):
    if not BOT_USERNAME: return text.strip()
    return re.sub(rf"@{re.escape(BOT_USERNAME)}\b", "", text, flags=re.I).strip()

def name_prefix(uid, name):
    name = (name or "").strip()
    now = time.time()
    if not name or len(name) > 30 or not re.fullmatch(r"[\w .'-]+", name, re.UNICODE): return ""
    with lock: last = name_time.get(uid, 0)
    if now - last < 600 or random.random() > 0.12: return ""
    with lock: name_time[uid] = now
    return name + ", "

# ============================================================
# COMMAND HANDLERS
# ============================================================

@bot.message_handler(commands=["start"])
def start(message):
    register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    bot.reply_to(message, "Suno! ✨ Main Venu hoon. Btao kya scene hai? 😎", reply_markup=main_kb())

@bot.message_handler(commands=["help"])
def command_help(message): help_(message)

@bot.message_handler(commands=["profile"])
def command_profile(message): profile(message)

@bot.message_handler(commands=["memory"])
def command_memory(message): mem(message)

@bot.message_handler(commands=["stats"])
def command_stats(message): stats(message)

@bot.message_handler(commands=["clear"])
def command_clear(message):
    clear_memory(message.from_user.id)
    bot.reply_to(message, "🧹 Memory cleared! Fresh slate 😌")

@bot.message_handler(commands=["voice"])
def voice(message):
    tts_users.add(message.from_user.id)
    bot.reply_to(message, "🎙️ Voice replies active.")

@bot.message_handler(commands=["novoice"])
def novoice(message):
    tts_users.discard(message.from_user.id)
    bot.reply_to(message, "🔇 Voice replies disabled.")

@bot.message_handler(commands=["joke"])
def command_joke(message): joke(message)

@bot.message_handler(commands=["shayari"])
def command_shayari(message): shayari(message)

@bot.message_handler(commands=["fun"])
def command_fun(message): fun(message)

@bot.message_handler(commands=["dice"])
def command_dice(message): bot.reply_to(message, f"🎲 {random.randint(1, 6)}")

@bot.message_handler(commands=["coin"])
def command_coin(message): bot.reply_to(message, "🪙 " + random.choice(["Heads!", "Tails!"]))

@bot.message_handler(commands=["choose"])
def command_choose(message):
    raw = message.text.partition(" ")[2]
    opts = [item.strip() for item in re.split(r"[,|]", raw) if item.strip()]
    bot.reply_to(message, "🎯 " + random.choice(opts) if len(opts) >= 2 else "Usage: /choose optionA, optionB")

@bot.message_handler(commands=["id"])
def command_id(message): bot.reply_to(message, f"🆔 User: {message.from_user.id}\n💬 Chat: {message.chat.id}")

@bot.message_handler(commands=["ping"])
def ping(message):
    started = time.perf_counter()
    try:
        sent = bot.reply_to(message, "🏓 Checking...")
        ms = round((time.perf_counter() - started) * 1000, 1)
        bot.edit_message_text(f"🏓 Pong! {ms} ms", message.chat.id, sent.message_id)
    except Exception:
        logger.exception("Ping error")

@bot.message_handler(commands=["roast"])
def command_roast(message): start_game(message, "roast")

# ============================================================
# CALLBACK QUERY HANDLER
# ============================================================

@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    try:
        try: bot.answer_callback_query(call.id)
        except Exception: pass
        msg = call.message
        data = call.data

        if data == "back": bot.edit_message_text("😎 Venu — Btao kya karna hai?", msg.chat.id, msg.message_id, reply_markup=main_kb())
        elif data == "games": bot.edit_message_text("🎮 Game choose kar:", msg.chat.id, msg.message_id, reply_markup=game_kb())
        elif data in {"guess", "tod", "riddle", "roast"}: start_game(msg, data)
        elif data == "talk": bot.send_message(msg.chat.id, "Bolo, sun rahi hoon 😎")
        elif data == "memory": mem(msg)
        elif data == "profile": profile(msg)
        elif data == "fun": fun(msg)
        elif data == "stats": stats(msg)
        elif data == "voice": voice(msg)
        elif data == "help": help_(msg)
        elif data == "group": add_group(msg)
        elif data == "clear":
            clear_memory(msg.from_user.id)
            bot.edit_message_text("🧹 Memory reset complete.", msg.chat.id, msg.message_id, reply_markup=main_kb())
    except Exception:
        logger.exception("Callback processing error")

# ============================================================
# MAIN TEXT MESSAGES ROUTER
# ============================================================

def text_handler(message):
    typing = None
    try:
        if not group_ok(message): return
        uid = message.from_user.id
        text = strip_mention(message.text or "").strip()
        if not text: return

        now = time.time()
        with lock:
            prev = last_msg.get(uid)
            last_msg[uid] = now
            activity[uid] = now

        if prev and now - prev < 0.15: return

        register_user(uid, message.from_user.username, message.from_user.first_name)

        actions = {"🎮 Guess Number": "guess", "🔥 Roast Battle": "roast", "🎯 Truth or Dare": "tod", "🧩 Riddle Battle": "riddle"}
        if text in actions:
            start_game(message, actions[text])
            return

        if text == "😂 Joke": joke(message); return
        if text == "❤️ Shayari": shayari(message); return
        if text == "🎲 Fun Zone": fun(message); return
        if text == "📊 My Stats": stats(message); return
        if text == "🧠 My Memory": mem(message); return
        if text in {"👤 My Profile", "👤 View Profile"}: profile(message); return
        if text == "🎙️ Voice Mode": voice(message); return
        if text == "ℹ️ Help": help_(message); return
        if text == "➕ Add Me In Group": add_group(message); return
        if text == "🧹 Clear Chat":
            clear_memory(uid)
            bot.reply_to(message, "🧹 Memory cleared!")
            return

        if game_process(message, text):
            daily(uid, True)
            return

        result = calc(text)
        if result is not None:
            bot.reply_to(message, f"🧮 {result}")
            daily(uid)
            return

        # AI Processing
        typing = Typing(message.chat.id)
        typing.start()

        save_message(uid, "user", text)
        packet = get_memory(uid, message.from_user.first_name or "Dost")

        reply, detected_mood = ai(uid, packet, text)
        prefix = name_prefix(uid, message.from_user.first_name)
        if prefix: reply = prefix + reply

        reply = clean_reply(reply)
        update_profile(uid, "current_mood", detected_mood)
        save_message(uid, "assistant", reply)
        daily(uid)

        typing.close()
        typing = None

        bot.reply_to(message, reply)

        if uid in tts_users and ENABLE_TTS:
            threading.Thread(target=tts, args=(message.chat.id, reply), daemon=True).start()

    except Exception:
        logger.exception("Text handler execution failure")
        if typing: typing.close()
        try: bot.reply_to(message, "Connection glitch hua ek sec 😭 phirse bolo.")
        except Exception: pass

bot.message_handler(content_types=["text"])(text_handler)

# ============================================================
# VOICE PROCESSING
# ============================================================

def transcribe(message):
    try:
        f_info = bot.get_file(message.voice.file_id)
        data = bot.download_file(f_info.file_path)
        with tempfile.TemporaryDirectory() as tmpdir:
            ogg_p = os.path.join(tmpdir, "audio.ogg")
            wav_p = os.path.join(tmpdir, "audio.wav")
            with open(ogg_p, "wb") as f: f.write(data)
            AudioSegment.from_file(ogg_p).export(wav_p, format="wav")
            rec = sr.Recognizer()
            with sr.AudioFile(wav_p) as src: audio = rec.record(src)
            return rec.recognize_google(audio, language="hi-IN")
    except Exception:
        return None

def tts(chat_id, text):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            a_path = os.path.join(tmpdir, "venu.mp3")
            gTTS(text=text, lang="hi").save(a_path)
            with open(a_path, "rb") as f: bot.send_voice(chat_id, f, caption="🎙️ Venu")
    except Exception:
        logger.exception("TTS Engine Exception")

@bot.message_handler(content_types=["voice"])
def voice_handler(message):
    typing = Typing(message.chat.id)
    try:
        if not group_ok(message): return
        typing.start()
        uid = message.from_user.id
        register_user(uid, message.from_user.username, message.from_user.first_name)

        text = transcribe(message)
        if not text:
            typing.close()
            bot.reply_to(message, "🎙️ Awaaz clear nahi thi, wapas bolo.")
            return

        save_message(uid, "user", "[Voice] " + text)
        packet = get_memory(uid, message.from_user.first_name or "Dost")

        reply, detected_mood = ai(uid, packet, text)
        reply = clean_reply(reply)

        update_profile(uid, "current_mood", detected_mood)
        save_message(uid, "assistant", reply)
        daily(uid)

        typing.close()
        bot.reply_to(message, "🎙️ " + reply)

        if uid in tts_users and ENABLE_TTS:
            threading.Thread(target=tts, args=(message.chat.id, reply), daemon=True).start()
    except Exception:
        logger.exception("Voice pipeline failed")
        typing.close()

# ============================================================
# ADMIN FUNCTIONS
# ============================================================

def is_admin(message): return bool(ADMIN_ID and message.from_user and message.from_user.id == ADMIN_ID)

@bot.message_handler(commands=["refresh"])
def refresh(message):
    if not is_admin(message):
        bot.reply_to(message, "⛔ Restricted area.")
        return
    with lock:
        memory.clear(); registered.clear(); recent_replies.clear(); games.clear(); last_msg.clear(); name_time.clear(); activity.clear()
    bot.reply_to(message, "♻️ System state refreshed.")

@bot.message_handler(commands=["broadcast"])
def broadcast(message):
    if not is_admin(message):
        bot.reply_to(message, "⛔ Restricted area.")
        return
    text = message.text.partition(" ")[2].strip()
    if not text:
        bot.reply_to(message, "Usage: /broadcast message_content")
        return

    rows = db.request("GET", "users?select=user_id", timeout=10) or []
    bot.reply_to(message, f"📢 Broadcasting to {len(rows)} users...")

    def worker():
        s, f = 0, 0
        for row in rows:
            try:
                bot.send_message(int(row["user_id"]), text)
                s += 1
                time.sleep(0.05)
            except Exception: f += 1
        try: bot.send_message(message.chat.id, f"📢 Broadcast finished.\n✅ Sent: {s}\n❌ Failed: {f}")
        except Exception: pass

    threading.Thread(target=worker, daemon=True).start()

# ============================================================
# CLEANUP DAEMON & POLLING
# ============================================================

def cleanup():
    while True:
        time.sleep(300)
        try:
            now = time.time()
            with lock:
                for uid, g in list(games.items()):
                    if now - g.get("created", now) > 1800: games.pop(uid, None)
                for uid, act in list(activity.items()):
                    if now - act > 7200:
                        activity.pop(uid, None)
                        last_msg.pop(uid, None)
        except Exception:
            logger.exception("Cleanup routine failed")

def start_polling():
    reconnect_delay = 5
    while True:
        try:
            logger.info("Starting Telegram polling engine...")
            bot.infinity_polling(timeout=25, long_polling_timeout=25, skip_pending=True, allowed_updates=["message", "callback_query"], none_stop=False)
            reconnect_delay = 5
        except ApiTelegramException as error:
            if "409" in str(error) or "Conflict" in str(error):
                logger.error("TELEGRAM 409 CONFLICT: Duplicate instance detected. Sleep for 15s...")
                time.sleep(15)
                continue
            logger.exception("Telegram API Failure")
            time.sleep(reconnect_delay)
        except Exception:
            logger.exception("Bot Crash Encountered. Restarting...")
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)

# ============================================================
# MAIN ENTRYPOINT
# ============================================================

def main():
    logger.info("🚀 Venu Production Bot Initializing")
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=cleanup, daemon=True).start()

    try:
        bot.remove_webhook()
        time.sleep(1)
    except Exception:
        logger.exception("Remove Webhook Exception")

    start_polling()

if __name__ == "__main__":
    main()
