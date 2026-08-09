# ============================================================
# VENU AI — MERGED SINGLE-FILE TELEGRAM BOT
# ============================================================
# Features merged into one file:
# - Groq AI / Llama 3.3 70B
# - Supabase persistent users, profiles, messages and daily stats
# - TTL memory/cache + duplicate-response protection
# - Guess Number / Truth or Dare / Riddle / Roast Battle
# - Roast Mode / Bakchodi Mode
# - Calculator with safe AST evaluation
# - Reply keyboard, Add-to-Group, Profile, Clear Chat
# - Help, Stats, Memory viewer, Joke, Shayari, Fun Zone, Dice, Coin, Choose
# - Better conversational prompting and natural follow-up chat
# - Private-chat + group mention/reply filtering
# - Voice input -> speech recognition -> AI -> optional TTS reply
# - Admin broadcast / refresh
# - Flask keep-alive endpoint
# - Retry/backoff logging
#
# IMPORTANT:
# Put secrets in environment variables:
# BOT_TOKEN
# GROQ_API_KEY
# SUPABASE_URL
# SUPABASE_KEY
#
# Do NOT hard-code Telegram/Groq/Supabase secrets in source code.
# ============================================================

from collections import deque
from difflib import SequenceMatcher
import ast
import json
import logging
from logging.handlers import RotatingFileHandler
import operator
import os
import random
import re
import tempfile
import threading
import time

from cachetools import TTLCache
from flask import Flask
from gtts import gTTS
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pydub import AudioSegment
import speech_recognition as sr
import telebot
from telebot import types


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "7042790112:AAH7bpJr8bMoMstA8MNNHq1bn6JTiYqla90").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://hhelxewgwuqcloofyeyw.supabase.co").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhoZWx4ZXdnd3VxY2xvb2Z5ZXl3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzk0NzIyNTUsImV4cCI6MjA5NTA0ODI1NX0.EL0wb1HKvT9lJLtMW7p-y0X3fwgC1LeFrts7ErHVD54").strip()

ADMIN_ID = 7422809081
BOT_USERNAME = "lunwtbwts_bot"
MODEL_NAME = os.getenv("MODEL_NAME", "llama-3.3-70b-versatile")

PORT = int(os.getenv("PORT", "10000"))
ENABLE_TTS = os.getenv("ENABLE_TTS", "true").lower() in {"1", "true", "yes", "on"}
RESPOND_IN_GROUPS = os.getenv("RESPOND_IN_GROUPS", "true").lower() in {"1", "true", "yes", "on"}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY environment variable is missing.")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY environment variables are required.")


# ============================================================
# LOGGING
# ============================================================

handler = RotatingFileHandler(
    "bot.log",
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)

logging.basicConfig(
    level=logging.INFO,
    handlers=[handler, logging.StreamHandler()],
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

try:
    BOT_ID = bot.get_me().id
    BOT_INFO = bot.get_me()
    if not BOT_USERNAME:
        BOT_USERNAME = (BOT_INFO.username or "").lower()
except Exception:
    logger.exception("Could not fetch Telegram bot information during startup")
    BOT_ID = None


# ============================================================
# SUPABASE CLIENT
# ============================================================

class SupabaseClient:
    def __init__(self, url, key):
        self.url = url
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

        self.session = requests.Session()

        retry = Retry(
            total=4,
            connect=4,
            read=4,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST", "PATCH", "DELETE"]),
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            pool_connections=20,
            pool_maxsize=20,
            max_retries=retry,
        )

        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def request(self, method, endpoint, payload=None, params=None):
        target_url = f"{self.url}/rest/v1/{endpoint}"

        try:
            method = method.upper()

            if method == "GET":
                response = self.session.get(
                    target_url,
                    headers=self.headers,
                    params=params,
                    timeout=12,
                )
            elif method == "POST":
                response = self.session.post(
                    target_url,
                    headers=self.headers,
                    json=payload,
                    timeout=12,
                )
            elif method == "PATCH":
                response = self.session.patch(
                    target_url,
                    headers=self.headers,
                    json=payload,
                    timeout=12,
                )
            elif method == "DELETE":
                response = self.session.delete(
                    target_url,
                    headers=self.headers,
                    timeout=12,
                )
            else:
                logger.error("Unsupported Supabase method: %s", method)
                return None

            response.raise_for_status()

            if response.text:
                return response.json()

            return None

        except Exception:
            logger.exception("Supabase request error [%s %s]", method, endpoint)
            return None


db = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)


# ============================================================
# GLOBAL STATE / CACHES
# ============================================================

state_lock = threading.RLock()

user_memory_cache = TTLCache(maxsize=1500, ttl=5400)
registered_users_cache = TTLCache(maxsize=5000, ttl=86400)

last_message_time = {}
user_recent_replies = {}
ACTIVE_GAME_SESSIONS = {}
ROAST_MODE_USERS = set()
TTS_USERS = set()
USER_ACTIVITY = {}


# ============================================================
# FLASK KEEP-ALIVE
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return (
        "🤖 Venu AI is online — AI, Supabase Memory, Games, "
        "Roast Mode, Group Support, Voice/TTS."
    )


@app.route("/health")
def health():
    return {
        "status": "online",
        "bot_id": BOT_ID,
        "model": MODEL_NAME,
    }


def run_flask():
    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True,
    )


# ============================================================
# PROFILE / MEMORY
# ============================================================

def default_profile(user_id, name="Dost"):
    return {
        "user_id": user_id,
        "name": name or "Dost",
        "age": "Not specified",
        "favorite_game": "Not specified",
        "favorite_movie": "Not specified",
        "language": "Hinglish",
        "roast_level": "Medium",
        "relationship_status": "Not specified",
        "hobbies": "Not specified",
        "current_mood": "Witty, loyal, and consistently chill",
        "emotional_momentum": "Stable",
    }


def register_user(user_id, username, first_name):
    with state_lock:
        if user_id in registered_users_cache:
            return

    payload = {
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "is_verified": True,
    }

    headers = {
        **db.headers,
        "Prefer": "resolution=merge-duplicates",
    }

    try:
        response = db.session.post(
            f"{db.url}/rest/v1/users",
            headers=headers,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()

        with state_lock:
            registered_users_cache[user_id] = True

    except Exception:
        logger.exception("Error registering user")


def clear_user_memory(user_id):
    with state_lock:
        db.request("DELETE", f"messages?user_id=eq.{user_id}")

        user_memory_cache.pop(user_id, None)

        if user_id in user_recent_replies:
            user_recent_replies[user_id].clear()

        last_message_time.pop(user_id, None)
        ACTIVE_GAME_SESSIONS.pop(user_id, None)
        ROAST_MODE_USERS.discard(user_id)
        TTS_USERS.discard(user_id)


def get_user_memory(user_id, first_name="Dost"):
    with state_lock:
        if user_id in user_memory_cache:
            return user_memory_cache[user_id]

    rows = db.request(
        "GET",
        f"user_profiles?user_id=eq.{user_id}",
    )

    if rows:
        profile = rows[0]
    else:
        profile = default_profile(user_id, first_name)
        db.request(
            "POST",
            "user_profiles",
            payload=profile,
        )

    summary_rows = db.request(
        "GET",
        f"conversation_summary?user_id=eq.{user_id}",
    )

    summary = (
        summary_rows[0].get("summary", "Ongoing friendly connection.")
        if summary_rows
        else "Ongoing friendly connection."
    )

    message_rows = db.request(
        "GET",
        f"messages?user_id=eq.{user_id}&order=created_at.desc&limit=20",
    )

    history = (
        [
            {
                "role": row["role"],
                "content": row["content"],
            }
            for row in reversed(message_rows)
        ]
        if message_rows
        else []
    )

    memory_packet = {
        "profile": profile,
        "summary": summary,
        "history": history,
    }

    with state_lock:
        user_memory_cache[user_id] = memory_packet

    return memory_packet


def update_profile_field(user_id, field, value):
    allowed_fields = {
        "name",
        "age",
        "favorite_game",
        "favorite_movie",
        "language",
        "roast_level",
        "relationship_status",
        "hobbies",
        "current_mood",
        "emotional_momentum",
    }

    if field not in allowed_fields:
        return

    db.request(
        "PATCH",
        f"user_profiles?user_id=eq.{user_id}",
        payload={field: value},
    )

    with state_lock:
        if user_id in user_memory_cache:
            user_memory_cache[user_id]["profile"][field] = value


def save_message(user_id, role, content):
    if not content:
        return

    ignored = {
        "hi",
        "hello",
        "ok",
        "hmm",
        "k",
        "acha",
        "hlo",
    }

    if role == "user" and content.lower().strip() in ignored:
        return

    db.request(
        "POST",
        "messages",
        payload={
            "user_id": user_id,
            "role": role,
            "content": content,
        },
    )

    with state_lock:
        if user_id in user_memory_cache:
            history = user_memory_cache[user_id]["history"]
            history.append(
                {
                    "role": role,
                    "content": content,
                }
            )

            if len(history) > 20:
                history.pop(0)


def increment_daily_stats(user_id, is_game=False):
    try:
        date_str = time.strftime("%Y-%m-%d")

        existing = db.request(
            "GET",
            f"daily_stats?user_id=eq.{user_id}&date=eq.{date_str}",
        )

        if existing:
            messages_sent = existing[0].get("messages_sent", 0)
            games_played = existing[0].get("games_played", 0)

            messages_sent += 0 if is_game else 1
            games_played += 1 if is_game else 0

            db.request(
                "PATCH",
                f"daily_stats?user_id=eq.{user_id}&date=eq.{date_str}",
                payload={
                    "messages_sent": messages_sent,
                    "games_played": games_played,
                },
            )
        else:
            db.request(
                "POST",
                "daily_stats",
                payload={
                    "user_id": user_id,
                    "date": date_str,
                    "messages_sent": 0 if is_game else 1,
                    "games_played": 1 if is_game else 0,
                },
            )

    except Exception:
        logger.exception("Daily stats update error")


# ============================================================
# SAFE CALCULATOR
# ============================================================

SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def safe_eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value

    if isinstance(node, ast.BinOp) and type(node.op) in SAFE_OPERATORS:
        left = safe_eval(node.left)
        right = safe_eval(node.right)
        return SAFE_OPERATORS[type(node.op)](left, right)

    if isinstance(node, ast.UnaryOp) and type(node.op) in SAFE_OPERATORS:
        operand = safe_eval(node.operand)
        return SAFE_OPERATORS[type(node.op)](operand)

    raise ValueError("Unsafe math expression.")


def evaluate_math(expression):
    try:
        expression = expression.strip()

        if len(expression) > 100:
            return None

        node = ast.parse(expression, mode="eval")
        return safe_eval(node.body)

    except Exception:
        return None


# ============================================================
# GAME DATA
# ============================================================

TRUTH_QUESTIONS = ['Life mein sabse bada fattu wala kaam kaunsa kiya hai? 🤨', 'Tera pehla crush kaun tha aur kya usne reject kar diya tha? 👀', 'Bachpan mein kaunsa sabse bada kaand kiya tha jo ghar walon ko aaj tak nahi pata? 🤫', 'Agar tujhe ek din ke liye invisible hone ka mauka mile, toh tu sabse pehle kahan jayega? 👻', 'Aisa kaun sa jhoot hai jo tune apne best friend se bola hai? 🤥', 'Tera aaj tak ka sabse embarrassing moment kaunsa raha hai? 😳', 'Agar tujhe apne phone ki gallery sabko dikhani pade, toh tu kitna dरेगा? 📱', 'Tune aakhri baar kis baat par jhoot bola tha? 🤥', 'Tera sabse ajeeb darr (phobia) kya hai? 🕷️', 'Agar tu ek din ke liye opposite gender ban jaye, toh sabse pehle kya karega? 🙃', 'Tune bina bill diye dukaan se bachpan mein kya churaya tha? 🛒', 'Tera sabse ganda habit kya hai jo kisi ko nahi pata? 🦥', 'Agar tujhe ek Billionaire banna ho, toh tu sabse pehle kya kharidega? 💰', 'Tera sabse ajeeb khana khane ka combination kya hai? 🍕', 'Agar tujhe kisi celebrity ke sath ek din bitane mile, toh tu kise chuntega? 🌟', 'Kya tune kabhi exam mein cheating ki hai? Kaise? 📝', 'Tera sabse bada regret kya hai life mein? 🥀', 'Agar tujhe kisi ek insaan ki memory erase karni ho, toh kiske karega? 🧠', 'Tune aakhri baar internet par kya ajeeb cheez search ki thi? 🔍', 'Tera dream partner kaisa hona chahiye? ✨', 'Agar tu ek din ke liye desh ka PM ban jaye, toh sabse pehla rule kya badlega? 🏛️', 'Kya tujhe apne naam se nafrat hai? Agar haan, toh kya rakhna chahega? 📛', 'Tera sabse bada secret talent kya hai? 🎭', 'Kya tune kabhi raat ko bhoot dekhne ka natak kiya hai? 👻', 'Tera sabse purana aur ajeeb toy kaun sa tha? 🧸', 'Agar tujhe ek hi khana puri zindagi khana pade, toh tu kya chuntega? 🍛', 'Tera sabse awkward date kaisa raha tha? 🥀', 'Kya tune kabhi public place par zor se aawaz mein gana gaya hai? 🎤', 'Tera favorite cartoon character kaun sa tha bachpan mein? 📺', 'Agar koi tera phone bina lock khole check kar le, toh tu kitna darega? 📱', 'Tu apne doston ke group mein sabse zyada kis baat ke liye roast hota hai? 🔥', 'Tune aakhri baar kisko block kiya tha aur kyu? 🚫', 'Tera sabse bada guilty pleasure kya hai? 🍫', 'Kya tune kabhi kisi ki chat chupke se padhi hai? 🔏', 'Agar tu ek din ke liye gayab ho sake, toh kiske ghar ki spy-cam banega? 🕵️\u200d♂️', 'Tera sabse zyada paisa kahan barbaad hota hai? 💸', 'Apni life ka sabse badaawkward moment ek line mein bata! 😬', 'Kya tune kabhi aaine ke samne khade hokar khud se baat ki hai? 🪞', 'Agar tujhe kisi movie ka villain banne ka mauka mile, toh kiska role karega? 🦹\u200d♂️', 'Tera sabse favourite gaana kaun sa hai jo tu bathroom mein gata hai? 🚿', 'Tune abhi tak kitni baar apna relationship status badla hai? 💔', 'Kya tujhe darr lagta hai akkele andhere mein sone se? 🌑', 'Tera phone ka wallpaper kya hai aur kyu? 🖼️', 'Agar tujhe koi ek superpower mile, toh kya karega? ⚡', 'Tune apne ghar walon se sabse bada jhoot kya bola hai? 🤥', 'Kya tune kabhi online dating ki hai? Kaisa anubhav raha? 💻', 'Tera aaj tak ka sabse kharab haircut kaunsa tha? 💇\u200d♂️', 'Agar tu ek din ke liye teacher ban jaye, toh sabse pehle kis student ko punish karega? 👨\u200d🏫', 'Kya tujhe cooking aati hai ya sirf maggi banata hai? 🍜', 'Tera sabse favorite dialogue kaun sa hai movies ka? 🎬']

DARE_TASKS = ["Apne kisi friend ko voice note bhej kar bol — 'Mujhe apne aap se pyaar ho gaya hai' aur screenshot bhej! 🤣", 'Apne phone ki gallery ka sabse random aur ajeeb photo bina context ke kisi dost ko bhej! 📸', "Agle 10 minutes tak tu jo bhi message karega, uske aakhiri mein 'UwU 🥺' lagana padega! ✨", 'Apne last call log ka screenshot bhej (jisme naam dikhe ya blur karde agar sharam aaye)! 📞', 'Apni crush ya ex ka naam chat mein type karke turant delete kar de! 🏃\u200d♂️', "Apne kisi bhi dost ko emoji ke sath 'I need help, hide the body' message bhej! 🚨", 'Apne haath ki anokhi position ka photo khinch kar bhej! ✋', 'Agle 5 messages bina kisi vowels (A, E, I, O, U) ke likh kar dikha! 🔠', 'Apne kisi close friend ko call karke bina wajeh hasna shuru kar de aur phone kaat de! 📞', 'Apne room ki sabse gandi jagah ka photo khinch kar bhej! 🧹', 'Apni profile picture 10 minutes ke liye koi funny meme laga kar dikha! 🖼️', 'Apne kisi dost ko ek romantic shayari bhej aur screen recording bhej! 💌', 'Agle 3 minutes tak sirf caps lock mein chat karega! 🔊', 'Apne ghar ke sabse bade bartan ke sath selfie bhej! 🍳', "Apne kisi dost ko text kar — 'Mujhe sapne mein alien dikha tha jo tera cousin tha' 👽", 'Apne phone ki battery percentage ka screenshot bhej! 🔋', 'Apne kisi dost ko bina kisi reason ke voice note mein funny laugh record karke bhej! 😂', 'Agle 3 messages mein sirf emojis ka use karega! 🎨', 'Apne paas rakhi hui sabse ajeeb cheez ki photo bhej! 📦', "Apne kisi dost ko message kar — 'Mujhe sach bata, tu alien toh nahi?' 🛸", 'Apne right hand se apna naam ulta likh kar photo bhej! ✍️', "Agle 5 minutes tak har sentence ke aage 'Sirji' lagayega! 🫡", 'Apne sabse purane dost ko ek embarrassing purani yaad bhej kar chhed! 🐒', 'Apne keyboard ki suggestions se ek funny sentence bana kar bhej! ⌨️', 'Apne ghar ke kisi paudhe ke sath selfie bhej! 🌱', "Apne kisi friend ko message kar — 'Bhai urgent kaam hai, 500 rupees gpay kar de' aur fir bol mazak tha! 💸", 'Agle 2 messages mein sirf English mein nahi, pure shuddh Hindi mein baat kar! 🇮🇳', 'Apne room ki ceiling ka photo khinch kar bhej! 🏠', 'Apne phone ka koi bhi random app open karke uska screenshot bhej! 📱', 'Apne kisi dost ko voice note mein ek movie ka dialogue bol kar suna! 🎬', 'Agle 5 messages mein exclamation mark (!) zaroor lagayega! ❗', 'Apne paas rakhe paani ke glass ke sath ek selfie bhej! 🥛', "Apne kisi dost ko text kar — 'Mujhe sapne mein kal tu mila tha aur tu nach raha tha' 💃", 'Apne baalon ko haath se kharab karke unki photo bhej! 🦁', 'Agle 3 messages mein koi bhi punctuation mark use mat kar! 🚷', 'Apne shoe ya slipper ka photo khinch kar bhej! 👟', "Apne kisi close friend ko message kar — 'Main aaj se sadhu ban raha hoon' 🙏", 'Apne phone ki screen brightness full karke photo bhej! ☀️', "Agle 2 messages mein bas 'Hahaha' se reply shuru karega! 😂", 'Apne ghar ke fridge ka photo khinch kar bhej! 🧊', "Apne kisi dost ko text kar — 'Pata hai kal kya hua?' aur fir reply mat de! 🤡", 'Apne table ya desk ki current condition ka photo bhej! 🪑', "Agle 4 messages mein har word ke baad 'bhai' lagana padega! 🤝", 'Apne pen ya pencil box ki photo bhej! ✏️', "Apne kisi dost ko text kar — 'Aap chronology samajhiye' 🇮🇳", 'Apne paas rakhi kisi kitab ka pehla page khol kar photo bhej! 📖', 'Agle 3 messages bilkul chhote yaani sirf 1 word ke honge! ⚡', 'Apne ghar ke darwaze ka photo khinch kar bhej! 🚪', "Apne kisi friend ko message kar — 'Mission successful ho gaya hai' 🕶️", 'Apne haath ki palm ki line ka photo bhej! ✋']

RIDDLES_DATA = [('Aisi kaun si cheez hai jo jitni zyada saaf karo, utni hi gandi hoti hai?', ['blackboard', 'black board', 'board']), ('Woh kya hai jo paida hote hi bina pairo ke bhagne lagti hai?', ['hawa', 'wind', 'air']), ('Aisi kaun si cheez hai jo samandar mein paida hoti hai aur ghar mein aate hi gayab ho jati hai?', ['namak', 'salt']), ('Aisi kaun si cheez hai jise aage se tum dekhte ho aur peeche se bhagwan dekhta hai?', ['bicycle', 'cycle']), ('Aisi kaun si cheez hai jiske paas pankh nahi hain par fir bhi woh udti hai?', ['patang', 'kite']), ('Aisa kaun sa phool hai jo rang nahi deta par sabke sar par sajta hai?', ['genda', 'flower']), ('Aisi kaun si cheez hai jo dhup mein bhi nahi sukhti?', ['paseena', 'sweat']), ('Woh kya hai jo saal mein ek baar aati hai aur mahine mein do baar, par din mein ek baar bhi nahi?', ['m', 'letter m']), ('Aisi kaun si cheez hai jise todne par aawaz nahi aati?', ['bharosa', 'trust']), ('Kaun sa jal hai jo kabhi pyas nahi bujha pata?', ['aankh ka jal', 'aansu', 'tears']), ('Aisi kaun si cheez hai jo jitni khinchoge, utni hi choti hoti jayegi?', ['cigarette', 'bidi']), ('Kala ghoda, safed sawari, ek utra toh dusri ki baari?', ['tota aur mirchi', 'pen aur ink']), ('Ek thal motiyo se bhara, sabke sar par ulta dhara?', ['aasmaan', 'sky', 'aasman']), ('Hari thi man bhari thi, lakh motiyo se jadi thi, raja ji ke bag mein dushala odh ke khadi thi?', ['makka', 'corn']), ('Na mooh hai na hath hai, fir bhi sabka pet bharti hai?', ['roti', 'khana', 'food']), ('Aisa kaun sa shehar hai jahan bina ticket ke ghoom sakte ho?', ['andher nagri', 'sapno ka shehar']), ('Woh kaun si cheez hai jo baandhne par chalti hai aur kholne par ruk jati hai?', ['joota', 'shoes', 'watch']), ('Aisi kaun si cheez hai jo bina pair ke chalti hai?', ['ghadi', 'clock', 'watch']), ('Aisa kaun sa fal hai jise pakne par meetha nahi hota?', ['mirch', 'chilli', 'mirchi']), ('Jitna zyada isko loge, utna hi peeche chhodte jaoge?', ['kadam', 'steps', 'footsteps']), ('Aisi kaun si cheez hai jiske paas ek aankh hai par woh dekh nahi sakti?', ['suui', 'needle']), ('Aisi kaun si cheez hai jo paani peete hi mar jati hai?', ['aag', 'fire']), ('Woh kaun hai jo apna saara kaam sir par uthakar karta hai?', ['bojh', 'coolie', 'mazdoor']), ('Aisi kaun si cheez hai jise hum bina chuhe kharid nahi sakte?', ['mouse', 'computer mouse']), ('Aisa kaun saajal hai jo jam nahi sakta?', ['aankh ka jal', 'aansu']), ('Aisi kaun si cheez hai jo zinda ho toh dafnate hain aur murda ho toh khate hain?', ['zinda aur murda paudha', 'pata', 'leaf']), ('Aisa kaun sa kaam hai jo admi karta hai aur aurat chupchap dekhti hai?', ['hajamat', 'cutting']), ('Aisi kaun si cheez hai jo bina pankh ke aasmaan mein udti hai?', ['patang', 'rocket', 'badal']), ('Woh kya hai jo apne pairon par chalti hai par sar par chadh kar bolti hai?', ['nasha', 'sharab']), ('Aisi kaun si cheez hai jo jitni baanti jaye, utni hi badhti hai?', ['gyan', 'knowledge', 'khushi']), ('Aisa kaun sa janwar hai jo bol nahi sakta par sun sakta hai?', ['machhli', 'fish']), ('Woh kya hai jo subah ko char pairon par, dopahar ko do pairon par aur sham ko teen pairon par chalti hai?', ['insaan', 'human']), ('Aisi kaun si cheez hai jo chalti hai toh rorti nahi, rukti hai toh ro deti hai?', ['cycle', 'vehicle']), ('Aisa kaun sa phalon ka raja hai jo ped par nahi ugta?', ['gulab jamun', 'papaya']), ('Aisi kaun si cheez hai jo andar se khali hoti hai aur bahaar se gol?', ['ring', 'anagthi', 'ball']), ('Woh kya hai jo sabke paas hoti hai aur sab alag alag bolte hain?', ['aawaz', 'voice', 'name']), ('Aisi kaun si cheez hai jo ghar mein ho toh shanti aur bahar ho toh shor?', ['bache', 'kids']), ('Aisa kaun sa rasta hai jahan koi nahi chal sakta?', ['sapne ka rasta', 'band rasta']), ('Woh kya hai jo ek hi jagah khadi rehti hai par poori duniya ghumati hai?', ['ticket', 'naksha', 'map']), ('Aisi kaun si cheez hai jo jitni purani ho, utni hi kimti hoti hai?', ['sharab', 'wine', 'purani yaad']), ('Aisa kaun sa janwar hai jiska pet uski peeth par hota hai?', ['kangroo']), ('Woh kya hai jise hum dekh sakte hain par chu nahi sakte?', ['sapna', 'aasmaan', 'chhand']), ('Aisi kaun si cheez hai jo bina haath ke darwaza khol sakti hai?', ['hawa', 'wind']), ('Aisa kaun sa jaanwar hai jo apne bachho ko pet ki thaili mein rakhta hai?', ['kangroo']), ('Woh kya hai jo aapko bina dekhe pehchan leti hai?', ['aawaz', 'dog']), ('Aisi kaun si cheez hai jo aag mein nahi jalti aur paani mein nahi doobti?', ['baraf', 'ice']), ('Aisa kaun sa ped hai jis par koi phal nahi lagta?', ['fasla ped', 'plastic ka ped']), ('Woh kya hai jo subah hari hoti hai aur sham ko laal?', ['suraj', 'sooraj']), ('Aisi kaun si cheez hai jo gandi hone par safed dikhti hai?', ['chalk', 'board']), ('Aisa kaun sa din hai jo saal mein sirf ek baar aata hai par har saal aata hai?', ['birthday'])]

ROAST_PROMPTS = ['Bata bhai, itni lambi umar ho gayi par aaj tak koi dhang ki achievement hai ya bas resume mein jhuth likhne ki ninja technique aati hai? 💀', 'Tera screen time dekh kar toh lagta hai tu real life se zyada digital world mein reject hota hai! 😂', 'Aisi shakal ke sath confidence kahan se laate ho? Thodi training humein bhi dilwa do! 🤭', 'Tujhse baat karke lagta hai ki evolution ne beech mein hi process rokk diya tha! 🔥', 'Tera dimaag aur Internet Explorer dono ek jaisi speed par chalte hain! 🐢', 'Itna confuse toh GPS bhi nahi hota jitna tu apni life ke decisions ko lekar rehta hai! 🧭', "Tujhe dekh kar lagta hai ki 'common sense' duniya ki sabse rare luxury ban chuki hai! 📉", 'Tera confidence aur tera talent dono alag-alag parallel universe mein rehte hain! 🌌', 'Agar laziness ka Olympic hota, toh tu pakka gold medal jeet kar sota rehta! 🥇', "Tujhe dekh kar lagta hai ki nature bhi kabhi-kabhi 'undo' button dabana bhool jata hai! 🖥️", 'Tujhe dekh kar lagta hai ki Google bhi search karke thak gaya hoga ki tera dimaag kahan hai! 🔍', 'Tera aur seriousness ka dur-dur tak koi rishta nahi hai! 🎭', 'Tu agar coding karne baithe, toh bugs bhi tujhse dar kar bhag jayein! 💻', 'Tera potential dekh kar lagta hai ki battery hamesha 1% par hi chal rahi hai! 🔋', 'Tujhse bada procrastination king maine apni poori life mein nahi dekha! 👑', 'Tera plan execution aur Monday morning dono sabse boring hote hain! 🥱', 'Tujhe dekh kar lagta hai ki alertness naam ki cheez ka birth hi nahi hua tere andar! 🦥', 'Teri speed dekh kar lagta hai ki turtle bhi tujhse race jeet jayega! 🐢', 'Tera dimaag khali plot ki tarah hai jis par board lag gaya hai! 🪧', 'Tujh se zyadato automatic washing machine smart hai! 🧺', 'Tera confidence dekh kar lagta hai ki ignorance truly is bliss! ✨', 'Tu jab serious hota hai, tab sabse zyada hasi aati hai! 🤡', 'Tera daily target bas sona aur scroll karna reh gaya hai! 📱', 'Tujhse behtar toh calculator answer de deta hai bina soche! 🧮', 'Tera talent hidden hi reh gaya, shayad exist hi nahi karta tha! 👻', 'Tujhe dekh kar lagta hai ki WiFi signal bhi tujhse weak hai! 📶', 'Tu jab advice deta hai, toh lagta hai ulta nuqsaan hone wala hai! ⚠️', 'Tera planning skills dekh kar Einstein bhi ro padte! 📈', "Tujhe dekh kar lagta hai ki 'hard work' word dictionary se delete ho chuka hai! 📖", 'Tu jab kuch naya sikhne ki koshish karta hai, toh history repeat hoti hai failure ke sath! 📜', 'Tera focus level goldfish se bhi kam hai! 🐠', 'Tu jab bolta hai, toh lagta hai time waste ka naya record ban raha hai! ⏱️', 'Tera excuse sunkar toh bhagwan bhi confuse ho jayein! 😇', 'Tujhe dekh kar lagta hai ki lazy ki definition redefine honi chahiye! 🛋️', 'Tera career graph ekdum flatline ki tarah chal raha hai! 📉', 'Tu jab gym jata hai, toh dumbbells bhi thak kar so jaate hain! 🏋️\u200d♂️', 'Tera mood swings dekh kar weather department bhi fail ho jaye! 🌦️', 'Tujhe dekh kar lagta hai ki sleep mode hi tera permanent state hai! 💤', 'Tu jab recipe banata hai, toh chemistry lab jaisa lagta hai! 🧪', 'Tera luck factor hamesha negative mein hi kyun rehta hai? 📉', "Tujhe dekh kar lagta hai ki 'try again' button sirf tere liye bana hai! 🕹️", 'Tu jab exam likhne baithta hai, toh sheet blank dekh kar darr jati hai! 📝', 'Tera overthinking level NASA ki calculations se upar chala jata hai! 🚀', 'Tujhe dekh kar lagta hai ki confusion tera best friend hai! 🫂', 'Tu jab joke marta hai, toh sannata aur bhi gehra ho jata hai! 🤫', 'Tera style dekh kar fashion police bhi resign kar degi! 🚨', 'Tujhe dekh kar lagta hai ki Bluetooth pair hone mein bhi sharmata hai! 📲', 'Tu jab task complete karta hai, toh history ban jati hai (woh bhi buri wali)! 🏆', 'Tera memory card lagta hai hamesha full hi rehta hai faltu baaton se! 💾', 'Tujh jaise genius ko dekh kar toh albert einstein bhi apna sar pakad lete! 🧠']


# ============================================================
# AI RESPONSE ENGINE
# ============================================================

def check_similarity(new_text, previous_texts, threshold=0.75):
    for previous in previous_texts:
        if SequenceMatcher(
            None,
            new_text.lower(),
            previous.lower(),
        ).ratio() >= threshold:
            return True

    return False


def clean_json_response(content):
    content = content.strip()

    if content.startswith("```"):
        parts = content.split("```")

        if len(parts) >= 2:
            content = parts[1].strip()

        if content.lower().startswith("json"):
            content = content[4:].strip()

    return content


def generate_unified_ai_response(
    user_id,
    memory_packet,
    latest_user_text,
):
    profile = memory_packet["profile"]
    summary = memory_packet["summary"]
    history = memory_packet["history"]

    with state_lock:
        is_roast_active = user_id in ROAST_MODE_USERS

        if user_id not in user_recent_replies:
            user_recent_replies[user_id] = deque(maxlen=10)

    mood_desc = (
        "Savage, mercilessly roasting, full of bakchodi vibes and comedy"
        if is_roast_active
        else "Witty, loyal, emotionally intelligent, supportive and chill"
    )

    system_prompt = f"""
You are Venu, a consistent desi best friend.

PERSONA:
- Natural Hinglish.
- Witty, street-smart and funny.
- Loyal and supportive when the user is serious.
- Savage only when the situation or Roast Mode calls for it.
- Never become randomly rude in a serious/emotional situation.
- Do not claim to be a human.
- Keep continuity with prior conversation.

CURRENT VIBE:
{mood_desc}

IMPORTANT:
Return STRICT JSON only with exactly two top-level keys:
{{
  "classification": {{
    "mood": "...",
    "intent": "chat|game|help|roast|calculator"
  }},
  "reply": "..."
}}

Reply must be 1-3 natural sentences in Hinglish.

USER PROFILE:
Name: {profile.get("name")}
Favorite Game: {profile.get("favorite_game")}
Favorite Movie: {profile.get("favorite_movie")}
Language: {profile.get("language")}
Roast Level: {profile.get("roast_level")}
Current Mood: {profile.get("current_mood")}

ONGOING SUMMARY:
{summary}
""".strip()

    messages = [
        {
            "role": "system",
            "content": system_prompt,
        }
    ]

    for msg in history[-20:]:
        role = msg.get("role")
        content = msg.get("content")

        if role in {"user", "assistant"} and content:
            messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

    messages.append(
        {
            "role": "user",
            "content": latest_user_text,
        }
    )

    url = "https://api.groq.com/openai/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    for attempt in range(3):
        try:
            payload = {
                "model": MODEL_NAME,
                "messages": messages,
                "temperature": 0.75 + (attempt * 0.05),
                "max_tokens": 300,
            }

            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=25,
            )

            response.raise_for_status()

            content = response.json()["choices"][0]["message"]["content"]
            content = clean_json_response(content)

            parsed = json.loads(content)

            reply = str(parsed.get("reply", "")).strip()

            classification = parsed.get(
                "classification",
                {
                    "mood": "Witty and Supportive",
                    "intent": "chat",
                },
            )

            if not reply:
                raise ValueError("Groq returned an empty reply.")

            with state_lock:
                if not check_similarity(
                    reply,
                    user_recent_replies[user_id],
                    threshold=0.75,
                ):
                    user_recent_replies[user_id].append(reply)
                    return classification, reply

        except Exception:
            logger.exception(
                "Groq unified API exception on attempt %s",
                attempt + 1,
            )

    fallback = (
        "Arey yaar, connection thoda slow ho gaya tha 😭 "
        "par main yahin hoon. Bata kya scene hai? 🤭"
    )

    with state_lock:
        user_recent_replies[user_id].append(fallback)

    return (
        {
            "mood": "Witty and Supportive",
            "intent": "chat",
        },
        fallback,
    )


# ============================================================
# KEYBOARD
# ============================================================

def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2,
    )

    markup.add(
        types.KeyboardButton("🎮 Guess Number"),
        types.KeyboardButton("🎯 Truth or Dare"),
        types.KeyboardButton("🧩 Riddle Battle"),
        types.KeyboardButton("🔥 Roast War"),
        types.KeyboardButton("😂 Joke"),
        types.KeyboardButton("❤️ Shayari"),
        types.KeyboardButton("🎲 Fun Zone"),
        types.KeyboardButton("📊 My Stats"),
        types.KeyboardButton("👤 View Profile"),
        types.KeyboardButton("➕ Add Me In Group"),
        types.KeyboardButton("💬 Talk To Venu"),
        types.KeyboardButton("ℹ️ Help"),
        types.KeyboardButton("🧹 Clear Chat"),
    )

    return markup


# ============================================================
# GAME MANAGER
# ============================================================

def handle_game_manager(message, game_type):
    user_id = message.from_user.id

    with state_lock:
        if game_type == "guess":
            target = random.randint(1, 50)

            ACTIVE_GAME_SESSIONS[user_id] = {
                "type": "guess",
                "target": target,
                "attempts": 0,
                "max_attempts": 5,
                "created": time.time(),
            }

            bot.reply_to(
                message,
                "🎮 Number Guessing Challenge (1-50)!\n"
                "Tere paas 5 attempts hain. Sahi number guess kar! 🎯",
            )

        elif game_type == "truth_or_dare":
            choice_type = random.choice(["Truth", "Dare"])

            task = random.choice(
                TRUTH_QUESTIONS
                if choice_type == "Truth"
                else DARE_TASKS
            )

            ACTIVE_GAME_SESSIONS[user_id] = {
                "type": "tod",
                "sub_type": choice_type,
                "created": time.time(),
            }

            bot.reply_to(
                message,
                f"🎯 Truth or Dare [{choice_type}]:\n\n"
                f"{task}\n\n"
                "💬 Answer/proof bhej aur round complete kar!",
            )

        elif game_type == "riddle":
            riddle, answers = random.choice(RIDDLES_DATA)

            ACTIVE_GAME_SESSIONS[user_id] = {
                "type": "riddle",
                "answers": answers,
                "created": time.time(),
            }

            bot.reply_to(
                message,
                f"🧩 Riddle Challenge:\n\n{riddle}\n\n"
                "🧠 Sahi jawab type kar!",
            )

        elif game_type == "roast_battle":
            roast = random.choice(ROAST_PROMPTS)

            ACTIVE_GAME_SESSIONS[user_id] = {
                "type": "roast",
                "created": time.time(),
            }

            bot.reply_to(
                message,
                f"🔥 Roast Battle:\n{roast}\n\n"
                "Ab solid comeback de!",
            )


def process_active_game(message, user_id, text_content):
    with state_lock:
        if user_id not in ACTIVE_GAME_SESSIONS:
            return False

        session = ACTIVE_GAME_SESSIONS[user_id]
        game_type = session["type"]

        if game_type == "guess":
            if not text_content.isdigit():
                bot.reply_to(
                    message,
                    "Bhai seedha 1-50 ka number type kar na! 🔢",
                )
                return True

            guess = int(text_content)

            if not 1 <= guess <= 50:
                bot.reply_to(
                    message,
                    "Bhai 1 se 50 ke beech number daal! 😭",
                )
                return True

            session["attempts"] += 1

            target = session["target"]
            attempts_left = session["max_attempts"] - session["attempts"]

            if guess == target:
                ACTIVE_GAME_SESSIONS.pop(user_id, None)

                bot.reply_to(
                    message,
                    f"🎉 Jeet gaye! Sahi number {target} tha. "
                    f"{session['attempts']} attempts mein phod diya! 🏆🔥",
                )

            elif attempts_left <= 0:
                ACTIVE_GAME_SESSIONS.pop(user_id, None)

                bot.reply_to(
                    message,
                    f"❌ Game Over! Sahi number {target} tha. "
                    "Agli baar aur dimaag lagaana 😜",
                )

            elif guess < target:
                responses = [
                    f"📈 Bahut chhota hai bhai! Upar jaa. Attempts: {attempts_left}",
                    f"🚀 Target isse bada hai! Attempts bache: {attempts_left}",
                    f"⬆️ Thoda aur upar! Attempts: {attempts_left}",
                ]

                bot.reply_to(
                    message,
                    random.choice(responses),
                )

            else:
                responses = [
                    f"📉 Bahut bada daal diya! Niche aa. Attempts: {attempts_left}",
                    f"🔻 Target chhota hai bhai! Attempts: {attempts_left}",
                    f"⬇️ Thoda down jaa! Attempts: {attempts_left}",
                ]

                bot.reply_to(
                    message,
                    random.choice(responses),
                )

            return True

        if game_type == "riddle":
            answer = text_content.lower().strip()
            correct_answers = session["answers"]

            ACTIVE_GAME_SESSIONS.pop(user_id, None)

            if any(
                correct in answer
                for correct in correct_answers
            ):
                bot.reply_to(
                    message,
                    "🏆 Sahi jawab! Maan gaye bhai, dimaag tez chal raha hai! ✨",
                )
            else:
                bot.reply_to(
                    message,
                    "❌ Galat jawab! Sahi answer: "
                    + ", ".join(correct_answers)
                    + " 😎",
                )

            return True

        if game_type in {"tod", "roast"}:
            ACTIVE_GAME_SESSIONS.pop(user_id, None)

            if game_type == "tod":
                bot.reply_to(
                    message,
                    "🔥 Round complete bhai! Ab next challenge ke liye menu use kar.",
                )
            else:
                bot.reply_to(
                    message,
                    "🔥 Solid comeback! Is round mein tu bach gaya 😂",
                )

            return True

    return False


# ============================================================
# GROUP FILTER
# ============================================================

def is_group_message(message):
    return message.chat.type in {"group", "supergroup"}


def should_respond_in_group(message):
    if not RESPOND_IN_GROUPS:
        return False

    if not is_group_message(message):
        return True

    text = message.text or ""
    lowered = text.lower()

    if text.startswith("/"):
        return True

    if BOT_USERNAME and f"@{BOT_USERNAME.lower()}" in lowered:
        return True

    reply = message.reply_to_message

    if reply and reply.from_user:
        if BOT_ID and reply.from_user.id == BOT_ID:
            return True

    return False


def strip_bot_mention(text):
    if not text:
        return text

    if BOT_USERNAME:
        text = text.replace(
            f"@{BOT_USERNAME}",
            "",
        )
        text = text.replace(
            f"@{BOT_USERNAME.lower()}",
            "",
        )

    return text.strip()


# ============================================================
# EXTRA FUN / CHAT FEATURES
# ============================================================

JOKES = [
    "Bhai mera future itna bright hai ki phone ki brightness auto low ho gayi 😂",
    "Maine diet start ki thi... phir samose ne aankhon mein aankhein daal di. 😭",
    "Life mein do cheezein fast hain: WiFi kabhi nahi, aur salary ka khatam hona hamesha. 💀",
    "Mera motivation aur Monday ki dosti school ke crush jaisi hai—sirf door se. 😂",
    "Padhai ka sabse bada enemy distraction nahi, '5 minute aur' hai. 😭",
]

SHAYARI = [
    "Dil se nikli baat ko lafzon ka sahara kya,\nTu online ho toh notification se pyara kya. ❤️",
    "Chai garam, mausam suhana,\nDost tu mil jaaye toh scene mastana. ☕❤️",
    "Zindagi chhoti si hai, tension badi bana rakhi hai,\nHans le bhai, duniya ne kaunsi guarantee de rakhi hai. 😌",
    "Tere reply ka intezaar bhi kamaal karta hai,\nEk 'hmm' poora paragraph barbaad karta hai. 😂",
]

FUN_PROMPTS = [
    "🎯 Aaj ka challenge: kisi dost ko bina context 'mission successful' bhej.",
    "🧠 Mini challenge: 10 seconds mein 5 fruits ke naam bol.",
    "😂 Challenge: apne last used emoji se ek sentence bana.",
    "🎭 Challenge: apni life ko ek movie title de.",
    "⚡ Rapid fire: chai ya coffee? Android ya iPhone? Night owl ya early bird?",
]

CHOICE_REPLIES = [
    "Bhai obvious hai — **{choice}** 😎",
    "Mera vote **{choice}** ko. Ab dekhte hain tera choice kitna dangerous hai 😂",
    "**{choice}**. Final answer. Lock kar diya 🔒🔥",
]


def send_help(message):
    bot.reply_to(
        message,
        "ℹ️ **Venu Features**\n\n"
        "💬 Natural Hinglish AI chat + long-term Supabase memory\n"
        "🎮 Guess, Truth/Dare, Riddle, Roast Battle\n"
        "😂 Joke / ❤️ Shayari / 🎲 Fun Zone\n"
        "🔥 /roast + /unroast\n"
        "🎙️ /voice + /novoice for voice replies\n"
        "📊 /stats for activity\n"
        "🧠 /memory for saved profile summary\n"
        "🆔 /id for Telegram IDs\n"
        "🏓 /ping for bot health\n"
        "➕ Add me to a group and mention @Chatbotgebot\n\n"
        "Group mein Venu tab reply karega jab mention ya reply kiya jayega.",
        reply_markup=get_main_keyboard(),
    )


def send_stats(message):
    user_id = message.from_user.id
    today = time.strftime("%Y-%m-%d")
    rows = db.request("GET", f"daily_stats?user_id=eq.{user_id}&order=date.desc&limit=7") or []
    today_row = next((r for r in rows if r.get("date") == today), None)
    total_messages = sum(int(r.get("messages_sent", 0) or 0) for r in rows)
    total_games = sum(int(r.get("games_played", 0) or 0) for r in rows)
    bot.reply_to(
        message,
        "📊 **Venu Stats**\n\n"
        f"📅 Today: {today_row.get('messages_sent', 0) if today_row else 0} messages\n"
        f"🎮 Today games: {today_row.get('games_played', 0) if today_row else 0}\n"
        f"📈 Last 7 days messages: {total_messages}\n"
        f"🏆 Last 7 days games: {total_games}\n"
        f"🔥 Roast Mode: {'ON' if user_id in ROAST_MODE_USERS else 'OFF'}\n"
        f"🎙️ Voice: {'ON' if user_id in TTS_USERS else 'OFF'}",
        reply_markup=get_main_keyboard(),
    )


def send_memory_summary(message):
    memory = get_user_memory(message.from_user.id, message.from_user.first_name or "Dost")
    profile = memory["profile"]
    summary = memory.get("summary") or "No long-term summary yet."
    bot.reply_to(
        message,
        "🧠 **What Venu remembers**\n\n"
        f"Name: {profile.get('name')}\n"
        f"Game: {profile.get('favorite_game')}\n"
        f"Movie: {profile.get('favorite_movie')}\n"
        f"Hobbies: {profile.get('hobbies')}\n"
        f"Relationship: {profile.get('relationship_status')}\n\n"
        f"💭 Context: {summary}",
        reply_markup=get_main_keyboard(),
    )


def send_fun_zone(message):
    bot.reply_to(
        message,
        random.choice(FUN_PROMPTS) + "\n\n" + random.choice(JOKES),
        reply_markup=get_main_keyboard(),
    )


def send_joke(message):
    bot.reply_to(message, "😂 " + random.choice(JOKES), reply_markup=get_main_keyboard())


def send_shayari(message):
    bot.reply_to(message, random.choice(SHAYARI), reply_markup=get_main_keyboard())


def send_add_me_in_group(message):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(
        "➕ Add Venu To Group",
        url=f"https://t.me/{BOT_USERNAME}?startgroup=true"
    ))
    bot.reply_to(
        message,
        "➕ **Add Venu In Group**\n\n"
        "Button dabao, group select karo aur Venu ko add kar do. 😎🔥\n\n"
        "Group mein mujhe **@lunwtbwts_bot** mention karo ya meri message ko reply karo.",
        reply_markup=markup,
    )

# ============================================================
# PROFILE / GROUP / MEMORY
# ============================================================

def send_profile(message):
    try:
        memory = get_user_memory(
            message.from_user.id,
            message.from_user.first_name or "Dost",
        )

        profile = memory["profile"]

        text = (
            "👤 Venu Long-Term Memory Profile\n\n"
            f"📌 Name: {profile.get('name')}\n"
            f"🎂 Age: {profile.get('age')}\n"
            f"🎮 Favorite Game: {profile.get('favorite_game')}\n"
            f"🎬 Favorite Movie: {profile.get('favorite_movie')}\n"
            f"🔥 Roast Level: {profile.get('roast_level')}\n"
            f"🧠 Mood: {profile.get('current_mood')}\n"
            f"💭 Momentum: {profile.get('emotional_momentum')}"
        )

        bot.reply_to(
            message,
            text,
            reply_markup=get_main_keyboard(),
        )

    except Exception:
        logger.exception("Profile command execution error")


# ============================================================
# TTS
# ============================================================

def send_tts_reply(chat_id, text):
    if not ENABLE_TTS:
        return False

    try:
        with tempfile.TemporaryDirectory() as tmp:
            mp3_path = os.path.join(tmp, "venu_reply.mp3")

            gTTS(
                text=text,
                lang="hi",
                slow=False,
            ).save(mp3_path)

            with open(mp3_path, "rb") as audio:
                bot.send_voice(
                    chat_id,
                    audio,
                    caption="🎙️ Venu",
                )

        return True

    except Exception:
        logger.exception("TTS generation failed")
        return False


def transcribe_telegram_voice(message):
    try:
        file_info = bot.get_file(message.voice.file_id)

        downloaded = bot.download_file(file_info.file_path)

        with tempfile.TemporaryDirectory() as tmp:
            ogg_path = os.path.join(tmp, "input.ogg")
            wav_path = os.path.join(tmp, "input.wav")

            with open(ogg_path, "wb") as file:
                file.write(downloaded)

            AudioSegment.from_file(ogg_path).export(
                wav_path,
                format="wav",
            )

            recognizer = sr.Recognizer()

            with sr.AudioFile(wav_path) as source:
                audio = recognizer.record(source)

            return recognizer.recognize_google(
                audio,
                language="hi-IN",
            )

    except sr.UnknownValueError:
        return None

    except Exception:
        logger.exception("Voice transcription failed")
        return None


# ============================================================
# ADMIN
# ============================================================

def admin_only(message):
    return bool(
        ADMIN_ID
        and message.from_user
        and message.from_user.id == ADMIN_ID
    )


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message):
    if not admin_only(message):
        bot.reply_to(message, "⛔ Admin only.")
        return

    text = message.text.partition(" ")[2].strip()

    if not text:
        bot.reply_to(
            message,
            "Usage: /broadcast your message",
        )
        return

    rows = db.request(
        "GET",
        "users?select=user_id",
    ) or []

    success = 0
    failed = 0

    for row in rows:
        user_id = row.get("user_id")

        if not user_id:
            continue

        try:
            bot.send_message(
                int(user_id),
                text,
            )
            success += 1
            time.sleep(0.04)

        except Exception:
            failed += 1

    bot.reply_to(
        message,
        f"📢 Broadcast finished.\n"
        f"✅ Sent: {success}\n"
        f"❌ Failed: {failed}",
    )


@bot.message_handler(commands=["refresh"])
def cmd_refresh(message):
    if not admin_only(message):
        bot.reply_to(message, "⛔ Admin only.")
        return

    with state_lock:
        user_memory_cache.clear()
        registered_users_cache.clear()
        user_recent_replies.clear()
        ACTIVE_GAME_SESSIONS.clear()
        last_message_time.clear()

    bot.reply_to(
        message,
        "♻️ Venu caches/state refreshed successfully.",
    )


# ============================================================
# BASIC COMMANDS
# ============================================================

@bot.message_handler(commands=["start"])
def cmd_start(message):
    try:
        user = message.from_user

        register_user(
            user.id,
            user.username,
            user.first_name,
        )

        get_user_memory(
            user.id,
            user.first_name or "Dost",
        )

        bot.reply_to(
            message,
            f"Oye {user.first_name or 'Dost'}! ✨\n"
            "Main Venu hoon. Bata aaj kya scene hai? 😎🔥",
            reply_markup=get_main_keyboard(),
        )

    except Exception:
        logger.exception("Start command execution error")


@bot.message_handler(commands=["profile"])
def cmd_profile(message):
    send_profile(message)


@bot.message_handler(commands=["clear"])
def cmd_clear(message):
    try:
        clear_user_memory(message.from_user.id)

        bot.reply_to(
            message,
            "🧹 Purani chat memory clear kar di. "
            "Naye sire se shuru karte hain 😌✨",
            reply_markup=get_main_keyboard(),
        )

    except Exception:
        logger.exception("Clear command execution error")


@bot.message_handler(commands=["roast"])
def cmd_roast(message):
    user_id = message.from_user.id

    with state_lock:
        ROAST_MODE_USERS.add(user_id)

    update_profile_field(
        user_id,
        "roast_level",
        "Savage",
    )

    bot.reply_to(
        message,
        "🔥 Roast Mode ON!\n"
        "Ab Venu soft nahi padega. Bakchodi level max 😂💀\n"
        "Band karne ke liye /unroast",
    )


@bot.message_handler(commands=["unroast"])
def cmd_unroast(message):
    user_id = message.from_user.id

    with state_lock:
        ROAST_MODE_USERS.discard(user_id)

    update_profile_field(
        user_id,
        "roast_level",
        "Medium",
    )

    bot.reply_to(
        message,
        "😌 Roast Mode OFF. Ab normal Venu vibes.",
    )


@bot.message_handler(commands=["voice"])
def cmd_voice(message):
    user_id = message.from_user.id

    with state_lock:
        TTS_USERS.add(user_id)

    bot.reply_to(
        message,
        "🎙️ Voice replies ON. Ab Venu text ke saath voice bhi de sakta hai.",
    )


@bot.message_handler(commands=["novoice"])
def cmd_novoice(message):
    user_id = message.from_user.id

    with state_lock:
        TTS_USERS.discard(user_id)

    bot.reply_to(
        message,
        "🔇 Voice replies OFF.",
    )


# ============================================================
# EXTRA COMMANDS
# ============================================================

@bot.message_handler(commands=["help"])
def cmd_help(message):
    send_help(message)


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    send_stats(message)


@bot.message_handler(commands=["memory"])
def cmd_memory(message):
    send_memory_summary(message)


@bot.message_handler(commands=["id"])
def cmd_id(message):
    bot.reply_to(
        message,
        f"🆔 User ID: `{message.from_user.id}`\n"
        f"💬 Chat ID: `{message.chat.id}`\n"
        f"👥 Chat type: `{message.chat.type}`",
    )


@bot.message_handler(commands=["ping"])
def cmd_ping(message):
    started = time.perf_counter()
    sent = bot.reply_to(message, "🏓 Pinging Venu...")
    ms = round((time.perf_counter() - started) * 1000, 1)
    try:
        bot.edit_message_text(
            f"🏓 **Pong!** {ms} ms\n🤖 Venu is online and ready.",
            message.chat.id,
            sent.message_id,
        )
    except Exception:
        pass


@bot.message_handler(commands=["joke"])
def cmd_joke(message):
    send_joke(message)


@bot.message_handler(commands=["shayari"])
def cmd_shayari(message):
    send_shayari(message)


@bot.message_handler(commands=["fun"])
def cmd_fun(message):
    send_fun_zone(message)


@bot.message_handler(commands=["choose"])
def cmd_choose(message):
    raw = message.text.partition(" ")[2].strip()
    choices = [x.strip() for x in re.split(r"[,|]", raw) if x.strip()]
    if len(choices) < 2:
        bot.reply_to(message, "🎲 Usage: /choose chai, coffee, cold drink")
        return
    bot.reply_to(message, random.choice(CHOICE_REPLIES).format(choice=random.choice(choices)))


@bot.message_handler(commands=["coin"])
def cmd_coin(message):
    bot.reply_to(message, "🪙 " + random.choice(["Heads! 🗣️", "Tails! 🪙"]))


@bot.message_handler(commands=["dice"])
def cmd_dice(message):
    bot.reply_to(message, f"🎲 Dice: **{random.randint(1, 6)}**")


# ============================================================
# MAIN TEXT HANDLER
# ============================================================

@bot.message_handler(
    content_types=["text"]
)
def handle_text_message(message):
    try:
        if not should_respond_in_group(message):
            return

        user_id = message.from_user.id
        chat_id = message.chat.id
        with state_lock:
            USER_ACTIVITY[user_id] = time.time()

        text_content = strip_bot_mention(
            message.text or ""
        ).strip()

        if not text_content:
            return

        current_time = time.time()

        with state_lock:
            previous_time = last_message_time.get(user_id)

            if (
                previous_time is not None
                and current_time - previous_time < 1.0
            ):
                return

            last_message_time[user_id] = current_time

        register_user(
            user_id,
            message.from_user.username,
            message.from_user.first_name,
        )

        # Keyboard actions
        if text_content == "🎮 Guess Number":
            handle_game_manager(message, "guess")
            return

        if text_content == "🎯 Truth or Dare":
            handle_game_manager(message, "truth_or_dare")
            return

        if text_content == "🧩 Riddle Battle":
            handle_game_manager(message, "riddle")
            return

        if text_content == "🔥 Roast War":
            handle_game_manager(message, "roast_battle")
            return

        if text_content == "😂 Joke":
            send_joke(message)
            return

        if text_content == "❤️ Shayari":
            send_shayari(message)
            return

        if text_content == "🎲 Fun Zone":
            send_fun_zone(message)
            return

        if text_content == "📊 My Stats":
            send_stats(message)
            return

        if text_content == "💬 Talk To Venu":
            bot.reply_to(message, "💬 Bol bhai, main sun raha hoon. Aaj ka scene kya hai? 😎", reply_markup=get_main_keyboard())
            return

        if text_content == "ℹ️ Help":
            send_help(message)
            return

        if text_content == "👤 View Profile":
            send_profile(message)
            return

        if text_content == "➕ Add Me In Group":
            send_add_me_in_group(message)
            return

        if text_content == "🧹 Clear Chat":
            cmd_clear(message)
            return

        # Active game
        if process_active_game(
            message,
            user_id,
            text_content,
        ):
            increment_daily_stats(
                user_id,
                is_game=True,
            )
            return

        # Calculator
        math_result = evaluate_math(text_content)

        if math_result is not None:
            bot.reply_to(
                message,
                f"🧮 Result: {math_result}",
                reply_markup=get_main_keyboard(),
            )

            increment_daily_stats(
                user_id,
                is_game=False,
            )
            return

        # AI
        try:
            bot.send_chat_action(
                chat_id,
                "typing",
            )
        except Exception:
            pass

        save_message(
            user_id,
            "user",
            text_content,
        )

        memory_packet = get_user_memory(
            user_id,
            message.from_user.first_name or "Dost",
        )

        classification, response = generate_unified_ai_response(
            user_id,
            memory_packet,
            text_content,
        )

        update_profile_field(
            user_id,
            "current_mood",
            classification.get(
                "mood",
                "Witty and Supportive",
            ),
        )

        save_message(
            user_id,
            "assistant",
            response,
        )

        increment_daily_stats(
            user_id,
            is_game=False,
        )

        bot.reply_to(
            message,
            response,
            reply_markup=get_main_keyboard(),
        )

        with state_lock:
            should_tts = user_id in TTS_USERS

        if should_tts:
            threading.Thread(
                target=send_tts_reply,
                args=(chat_id, response),
                daemon=True,
            ).start()

    except Exception:
        logger.exception(
            "Critical error in text handler"
        )

        try:
            bot.reply_to(
                message,
                "Arey yaar, server thoda busy hai 😭 "
                "ek baar phir bol.",
            )
        except Exception:
            pass


# ============================================================
# VOICE HANDLER
# ============================================================

@bot.message_handler(
    content_types=["voice"]
)
def handle_voice_message(message):
    try:
        if not should_respond_in_group(message):
            return

        user_id = message.from_user.id

        register_user(
            user_id,
            message.from_user.username,
            message.from_user.first_name,
        )

        bot.send_chat_action(
            message.chat.id,
            "typing",
        )

        text_content = transcribe_telegram_voice(message)

        if not text_content:
            bot.reply_to(
                message,
                "🎙️ Bhai awaaz clear nahi aayi 😭 "
                "Ek baar thoda clearly bol.",
            )
            return

        # Reuse the same AI pipeline as text.
        save_message(
            user_id,
            "user",
            "[Voice] " + text_content,
        )

        memory_packet = get_user_memory(
            user_id,
            message.from_user.first_name or "Dost",
        )

        classification, response = generate_unified_ai_response(
            user_id,
            memory_packet,
            text_content,
        )

        update_profile_field(
            user_id,
            "current_mood",
            classification.get(
                "mood",
                "Witty and Supportive",
            ),
        )

        save_message(
            user_id,
            "assistant",
            response,
        )

        increment_daily_stats(
            user_id,
            is_game=False,
        )

        bot.reply_to(
            message,
            f"🎙️ Tu bola: {text_content}\n\n"
            f"{response}",
            reply_markup=get_main_keyboard(),
        )

        with state_lock:
            should_tts = (
                ENABLE_TTS
                and (
                    user_id in TTS_USERS
                    or user_id in ROAST_MODE_USERS
                )
            )

        if should_tts:
            threading.Thread(
                target=send_tts_reply,
                args=(message.chat.id, response),
                daemon=True,
            ).start()

    except Exception:
        logger.exception(
            "Critical error in voice handler"
        )

        try:
            bot.reply_to(
                message,
                "🎙️ Voice processing mein issue aa gaya. "
                "Ek baar phir try kar.",
            )
        except Exception:
            pass


# ============================================================
# BACKGROUND CLEANUP
# ============================================================

def background_cleanup_daemon():
    while True:
        time.sleep(300)

        try:
            current_time = time.time()

            with state_lock:
                stale_games = [
                    user_id
                    for user_id, data
                    in ACTIVE_GAME_SESSIONS.items()
                    if current_time
                    - data.get("created", current_time)
                    > 1800
                ]

                for user_id in stale_games:
                    ACTIVE_GAME_SESSIONS.pop(
                        user_id,
                        None,
                    )

                stale_times = [
                    user_id
                    for user_id, timestamp
                    in last_message_time.items()
                    if current_time - timestamp > 7200
                ]

                for user_id in stale_times:
                    last_message_time.pop(
                        user_id,
                        None,
                    )
                    USER_ACTIVITY.pop(user_id, None)

            logger.info(
                "Background cleanup daemon executed successfully."
            )

        except Exception:
            logger.exception(
                "Background cleanup daemon error"
            )


# ============================================================
# ERROR HANDLER
# ============================================================

# Middleware intentionally disabled for compatibility with pyTelegramBotAPI.
# Incoming updates are already logged inside the message handlers.


# ============================================================
# STARTUP
# ============================================================

def main():
    logger.info(
        "🚀 Starting ULTIMATE Venu AI Telegram Bot — enhanced chat, games, memory, fun and group mode..."
    )

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
        name="flask-keepalive",
    )
    flask_thread.start()

    cleanup_thread = threading.Thread(
        target=background_cleanup_daemon,
        daemon=True,
        name="cleanup-daemon",
    )
    cleanup_thread.start()

    try:
        bot.remove_webhook()
        logger.info(
            "🧹 Existing webhook cleared."
        )
    except Exception:
        logger.exception(
            "Could not remove existing webhook"
        )

    while True:
        try:
            logger.info(
                "🔄 Telegram polling started..."
            )

            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True,
                allowed_updates=[
                    "message",
                    "edited_message",
                ],
            )

        except KeyboardInterrupt:
            logger.info(
                "🛑 Bot stopped by user."
            )
            break

        except Exception:
            logger.exception(
                "Polling exception. Reconnecting in 5 seconds..."
            )
            time.sleep(5)


if __name__ == "__main__":
    main()
