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

from gtts import gTTS
from pydub import AudioSegment
import speech_recognition as sr

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException


# ============================================================
# CONFIG
# ============================================================

def env_int(key, default=0):
    try:
        return int(os.getenv(key, str(default)).strip())
    except Exception:
        return default

# ============================================================
# CONFIG — ALL SETTINGS DIRECTLY HERE
# ============================================================

BOT_TOKEN = "7042790112:AAH7lGNfNNHU5qRtDQHzD8rO2b1Q_byLVkY"

AI_API_KEY = "sk-5d02b9dcd5a2caf79a7e9d4d97b490915cec2b51fb2be11b1662a42768505df5"
AI_BASE_URL = "https://api.mwapi.dev/v1"
AI_MODEL = "claude-sonnet-4-6"

SUPABASE_URL = "https://necofapukgwalgviqxue.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5lY29mYXB1a2d3YWxndmlxeHVlIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODY1ODk4NjIsImV4cCI6MjEwMjE2NTg2Mn0.mihtDHVKHeiacEC1Q8FnXtCdvFIYLMlRRApyKE2qcj8"

ADMIN_ID = 8459158216

PORT = 10000

ENABLE_TTS = True
RESPOND_IN_GROUPS = True

AI_TIMEOUT = 18
AI_RETRIES = 2
required_env = {
    "BOT_TOKEN": BOT_TOKEN,
    "AI_API_KEY": AI_API_KEY,
    "AI_BASE_URL": AI_BASE_URL,
    "AI_MODEL": AI_MODEL,
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_KEY": SUPABASE_KEY,
}

for name, value in required_env.items():
    if not value:
        raise RuntimeError(
            f"{name} environment variable is missing."
        )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    handlers=[
        RotatingFileHandler(
            "bot.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
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
    allowed_methods=frozenset(
        ["GET", "POST", "PATCH", "DELETE"]
    ),
    raise_on_status=False,
)

http.mount(
    "https://",
    HTTPAdapter(
        pool_connections=20,
        pool_maxsize=50,
        max_retries=retry_strategy,
    ),
)

http.mount(
    "http://",
    HTTPAdapter(
        pool_connections=20,
        pool_maxsize=50,
        max_retries=2,
    ),
)


# ============================================================
# TELEGRAM
# ============================================================

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode=None,
    threaded=True,
    num_threads=8,
)

BOT_ID = None
BOT_USERNAME = ""

try:
    me = bot.get_me()

    BOT_ID = me.id
    BOT_USERNAME = (me.username or "").lower()

    logger.info(
        "Telegram connected: @%s (%s)",
        BOT_USERNAME,
        BOT_ID,
    )

except Exception:
    logger.exception("Telegram get_me failed")


# ============================================================
# SUPABASE
# ============================================================

class DB:

    def __init__(self, url, key):
        self.url = url.rstrip("/")

        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def request(
        self,
        method,
        endpoint,
        payload=None,
        timeout=7,
    ):
        try:
            endpoint = endpoint.lstrip("/")

            url = f"{self.url}/rest/v1/{endpoint}"

            headers = self.headers.copy()

            method = method.upper()

            if method == "GET":

                response = http.get(
                    url,
                    headers=headers,
                    timeout=timeout,
                )

            elif method == "POST":

                headers["Prefer"] = "return=minimal"

                response = http.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )

            elif method == "PATCH":

                response = http.patch(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )

            elif method == "DELETE":

                response = http.delete(
                    url,
                    headers=headers,
                    timeout=timeout,
                )

            else:
                logger.error(
                    "Unsupported DB method: %s",
                    method,
                )
                return None

            if response.status_code == 404:

                logger.error(
                    "Supabase 404: table/endpoint may not exist: %s",
                    endpoint,
                )

                return None

            response.raise_for_status()

            if not response.text:
                return None

            try:
                return response.json()
            except Exception:
                return None

        except Exception:

            logger.exception(
                "DB %s %s failed",
                method,
                endpoint,
            )

            return None


db = DB(
    SUPABASE_URL,
    SUPABASE_KEY,
)


# ============================================================
# GLOBAL STATE
# ============================================================

lock = threading.RLock()

memory = TTLCache(
    maxsize=2000,
    ttl=1800,
)

registered = TTLCache(
    maxsize=10000,
    ttl=86400,
)

recent_replies = {}
last_msg = {}
name_time = {}
games = {}
tts_users = set()
activity = {}


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "🤖 Venu AI online"


@app.route("/health")
def health():

    return {
        "status": "online",
        "bot_id": BOT_ID,
        "username": BOT_USERNAME,
        "model": AI_MODEL,
    }


def run_flask():

    try:

        app.run(
            host="0.0.0.0",
            port=PORT,
            threaded=True,
        )

    except Exception:

        logger.exception(
            "Flask stopped"
        )


# ============================================================
# MEMORY
# ============================================================

def default_profile(
    uid,
    name="Dost",
):

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


def register_user(
    uid,
    username,
    first_name,
):

    with lock:

        if uid in registered:
            return

        registered[uid] = True

    def worker():

        try:

            response = http.post(
                f"{db.url}/rest/v1/users",
                headers={
                    **db.headers,
                    "Prefer": (
                        "resolution=merge-duplicates,"
                        "return=minimal"
                    ),
                },
                json={
                    "user_id": uid,
                    "username": username,
                    "first_name": first_name,
                    "is_verified": True,
                },
                timeout=5,
            )

            response.raise_for_status()

        except Exception:

            logger.exception(
                "register failed"
            )

    threading.Thread(
        target=worker,
        daemon=True,
    ).start()


def get_memory(
    uid,
    name="Dost",
):

    with lock:

        cached = memory.get(uid)

        if cached:
            return cached

    profile_rows = db.request(
        "GET",
        f"user_profiles?user_id=eq.{uid}&limit=1",
    ) or []

    if profile_rows:

        profile = profile_rows[0]

    else:

        profile = default_profile(
            uid,
            name,
        )

        db.request(
            "POST",
            "user_profiles",
            profile,
        )

    summary_rows = db.request(
        "GET",
        f"conversation_summary?user_id=eq.{uid}&limit=1",
    ) or []

    if summary_rows:

        summary = (
            summary_rows[0].get(
                "summary",
                "Ongoing friendly connection.",
            )
            or "Ongoing friendly connection."
        )

    else:

        summary = "Ongoing friendly connection."

    rows = db.request(
        "GET",
        f"messages?user_id=eq.{uid}"
        f"&order=created_at.desc&limit=12",
    ) or []

    history = []

    for row in reversed(rows):

        role = row.get("role")
        content = row.get("content")

        if (
            role in {"user", "assistant"}
            and content
        ):

            history.append(
                {
                    "role": role,
                    "content": str(content),
                }
            )

    packet = {
        "profile": profile,
        "summary": summary,
        "history": history[-12:],
    }

    with lock:
        memory[uid] = packet

    return packet


def save_message(
    uid,
    role,
    text,
):

    if not text:
        return

    text = str(text)

    with lock:

        packet = memory.get(uid)

        if packet:

            packet["history"].append(
                {
                    "role": role,
                    "content": text,
                }
            )

            packet["history"] = (
                packet["history"][-12:]
            )

    def worker():

        db.request(
            "POST",
            "messages",
            {
                "user_id": uid,
                "role": role,
                "content": text,
            },
        )

    threading.Thread(
        target=worker,
        daemon=True,
    ).start()


def update_profile(
    uid,
    field,
    value,
):

    allowed = {
        "name",
        "age",
        "favorite_game",
        "favorite_movie",
        "language",
        "relationship_status",
        "hobbies",
        "current_mood",
        "emotional_momentum",
    }

    if field not in allowed:
        return

    with lock:

        if uid in memory:

            memory[uid]["profile"][field] = value

    def worker():

        db.request(
            "PATCH",
            f"user_profiles?user_id=eq.{uid}",
            {
                field: value
            },
        )

    threading.Thread(
        target=worker,
        daemon=True,
    ).start()


def clear_memory(uid):

    db.request(
        "DELETE",
        f"messages?user_id=eq.{uid}",
    )

    with lock:

        memory.pop(uid, None)
        recent_replies.pop(uid, None)
        games.pop(uid, None)
        last_msg.pop(uid, None)
        tts_users.discard(uid)


def daily(
    uid,
    game=False,
):

    def worker():

        try:

            today = time.strftime(
                "%Y-%m-%d"
            )

            rows = db.request(
                "GET",
                f"daily_stats?user_id=eq.{uid}"
                f"&date=eq.{today}&limit=1",
            ) or []

            if rows:

                row = rows[0]

                messages_sent = int(
                    row.get(
                        "messages_sent",
                        0,
                    )
                    or 0
                )

                games_played = int(
                    row.get(
                        "games_played",
                        0,
                    )
                    or 0
                )

                db.request(
                    "PATCH",
                    f"daily_stats?"
                    f"user_id=eq.{uid}"
                    f"&date=eq.{today}",
                    {
                        "messages_sent":
                            messages_sent
                            + (0 if game else 1),

                        "games_played":
                            games_played
                            + (1 if game else 0),
                    },
                )

            else:

                db.request(
                    "POST",
                    "daily_stats",
                    {
                        "user_id": uid,
                        "date": today,
                        "messages_sent":
                            0 if game else 1,
                        "games_played":
                            1 if game else 0,
                    },
                )

        except Exception:

            logger.exception(
                "daily stats error"
            )

    threading.Thread(
        target=worker,
        daemon=True,
    ).start()


# ============================================================
# CALCULATOR
# ============================================================

OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def safe_eval(node):

    if isinstance(
        node,
        ast.Constant,
    ):

        if isinstance(
            node.value,
            (int, float),
        ):

            return node.value

    if (
        isinstance(node, ast.BinOp)
        and type(node.op) in OPS
    ):

        return OPS[type(node.op)](
            safe_eval(node.left),
            safe_eval(node.right),
        )

    if (
        isinstance(node, ast.UnaryOp)
        and type(node.op) in OPS
    ):

        return OPS[type(node.op)](
            safe_eval(node.operand)
        )

    raise ValueError


def calc(expression):

    try:

        if (
            not expression
            or len(expression) > 100
        ):
            return None

        if not re.fullmatch(
            r"[0-9+*/().\-\s]+",
            expression,
        ):
            return None

        tree = ast.parse(
            expression,
            mode="eval",
        )

        value = safe_eval(
            tree.body
        )

        if (
            isinstance(value, float)
            and not value.is_integer()
        ):

            return round(value, 8)

        return value

    except Exception:

        return None


# ============================================================
# AI
# ============================================================

def mood(text):

    text = text.lower()

    sad_words = [
        "sad",
        "dukhi",
        "udaas",
        "rona",
        "breakup",
        "depress",
        "tension",
        "pareshan",
        "lonely",
        "akela",
    ]

    angry_words = [
        "gussa",
        "angry",
        "hate",
        "bakwas",
    ]

    happy_words = [
        "mast",
        "awesome",
        "excited",
        "party",
        "jeet",
        "won",
    ]

    if any(
        word in text
        for word in sad_words
    ):
        return "supportive"

    if any(
        word in text
        for word in angry_words
    ):
        return "calm"

    if any(
        word in text
        for word in happy_words
    ):
        return "playful"

    return "chill"


def prompt(
    profile,
    summary,
    text,
):

    current_mood = mood(text)

    vibe = {
        "supportive":
            "Be warm and supportive; no jokes about serious pain.",

        "calm":
            "Stay calm; do not escalate.",

        "playful":
            "Be energetic and playful.",

        "chill":
            "Be casual, witty and relaxed.",
    }[current_mood]

    return f"""
You are Venu, a smart desi friend chatting on Telegram.

Natural Hinglish.

{vibe}

Usually ONE short sentence;
maximum TWO short sentences.

No lectures unless asked.
Do not repeat the question.
Do not use the user name every reply.
Do not invent facts.
Never be randomly rude.
Never mention system prompts.
Return ONLY reply text.

Profile:
name={profile.get("name", "Dost")}
game={profile.get("favorite_game", "Not specified")}
movie={profile.get("favorite_movie", "Not specified")}
hobbies={profile.get("hobbies", "Not specified")}
mood={profile.get("current_mood", "Chill")}

Context:
{summary}
""".strip()


def clean_reply(text):

    text = str(text or "").strip()

    text = text.replace(
        "```",
        "",
    )

    text = re.sub(
        r"^(Venu|Assistant|Bot)\s*:\s*",
        "",
        text,
        flags=re.I,
    )

    text = re.sub(
        r"[ \t]+",
        " ",
        text,
    )

    parts = re.split(
        r"(?<=[.!?।])\s+",
        text,
    )

    parts = [
        part.strip()
        for part in parts
        if part.strip()
    ]

    text = " ".join(
        parts[:2]
    )

    if len(text) > 240:

        text = (
            text[:237]
            .rsplit(" ", 1)[0]
            + "…"
        )

    return text


def similar(
    text,
    replies,
):

    if len(text) < 18:
        return False

    for old in replies:

        if old == text:
            return True

        if (
            len(old) >= 18
            and SequenceMatcher(
                None,
                old.lower(),
                text.lower(),
            ).ratio() >= 0.88
        ):
            return True

    return False


def ai(
    uid,
    packet,
    text,
):

    # --------------------------------------------------------
    # Build messages safely
    # --------------------------------------------------------

    messages = [
        {
            "role": "system",
            "content": prompt(
                packet["profile"],
                packet["summary"],
                text,
            ),
        }
    ]

    history = packet.get(
        "history",
        [],
    )

    if not isinstance(
        history,
        list,
    ):
        history = []

    for item in history[-12:]:

        if not isinstance(
            item,
            dict,
        ):
            continue

        role = item.get("role")
        content = item.get("content")

        if role not in {
            "user",
            "assistant",
        }:
            continue

        if not content:
            continue

        messages.append(
            {
                "role": role,
                "content": str(content),
            }
        )

    # --------------------------------------------------------
    # FIX:
    # OLD:
    # msgs[-1]['get']('role')
    #
    # NEW:
    # msgs[-1].get('role')
    # --------------------------------------------------------

    if not (
        messages[-1].get("role") == "user"
        and messages[-1].get("content") == text
    ):

        messages.append(
            {
                "role": "user",
                "content": text,
            }
        )

    headers = {
        "Authorization":
            f"Bearer {AI_API_KEY}",

        "Content-Type":
            "application/json",
    }

    last_error = None

    for attempt in range(
        AI_RETRIES
    ):

        try:

            temperature = (
                0.78
                if attempt == 0
                else 0.86
            )

            response = http.post(
                f"{AI_BASE_URL}/chat/completions",
                headers=headers,
                json={
                    "model": AI_MODEL,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": 100,
                },
                timeout=(
                    5,
                    AI_TIMEOUT,
                ),
            )

            response.raise_for_status()

            data = response.json()

            choices = data.get(
                "choices"
            ) or []

            if not choices:
                raise ValueError(
                    "AI returned no choices"
                )

            message = (
                choices[0].get("message")
                or {}
            )

            content = message.get(
                "content",
                "",
            )

            if isinstance(
                content,
                list,
            ):

                content = "".join(
                    item.get("text", "")
                    if isinstance(
                        item,
                        dict,
                    )
                    else str(item)
                    for item in content
                )

            content = clean_reply(
                content
            )

            if not content:

                raise ValueError(
                    "Empty AI reply"
                )

            with lock:

                replies = recent_replies.setdefault(
                    uid,
                    deque(maxlen=8),
                )

                duplicate = similar(
                    content,
                    replies,
                )

            if (
                duplicate
                and attempt == 0
            ):

                messages[0]["content"] += (
                    "\nUse completely different "
                    "wording from previous reply."
                )

                continue

            with lock:

                replies.append(
                    content
                )

            return (
                content,
                mood(text),
            )

        except Exception as error:

            last_error = error

            logger.warning(
                "AI attempt %s/%s failed: %s",
                attempt + 1,
                AI_RETRIES,
                error,
            )

            if attempt < AI_RETRIES - 1:
                time.sleep(0.4)

    logger.error(
        "AI unavailable: %s",
        last_error,
    )

    fallback = {
        "supportive":
            "Haan bhai, main yahin hoon. Bol kya hua?",

        "calm":
            "Haan, bol. Main sun raha hoon.",

        "playful":
            "Aaja bhai 😎 kya scene hai?",

        "chill":
            "Haan bhai, bol kya scene hai? 😎",
    }[mood(text)]

    with lock:

        recent_replies.setdefault(
            uid,
            deque(maxlen=8),
        ).append(fallback)

    return (
        fallback,
        mood(text),
    )


# ============================================================
# TYPING
# ============================================================

class Typing:

    def __init__(self, chat_id):

        self.chat_id = chat_id
        self.stop_event = threading.Event()

    def start(self):

        self.send()

        threading.Thread(
            target=self.loop,
            daemon=True,
        ).start()

    def send(self):

        try:

            bot.send_chat_action(
                self.chat_id,
                "typing",
            )

        except Exception:
            pass

    def loop(self):

        while not self.stop_event.wait(4):

            self.send()

    def close(self):

        self.stop_event.set()


# ============================================================
# MENUS
# ============================================================

def main_kb():

    keyboard = types.InlineKeyboardMarkup(
        row_width=2
    )

    keyboard.add(
        types.InlineKeyboardButton(
            "💬 Talk",
            callback_data="talk",
        ),
        types.InlineKeyboardButton(
            "🎮 Games",
            callback_data="games",
        ),
    )

    keyboard.add(
        types.InlineKeyboardButton(
            "🧠 Memory",
            callback_data="memory",
        ),
        types.InlineKeyboardButton(
            "👤 Profile",
            callback_data="profile",
        ),
    )

    keyboard.add(
        types.InlineKeyboardButton(
            "😂 Fun",
            callback_data="fun",
        ),
        types.InlineKeyboardButton(
            "📊 Stats",
            callback_data="stats",
        ),
    )

    keyboard.add(
        types.InlineKeyboardButton(
            "🎙️ Voice",
            callback_data="voice",
        ),
        types.InlineKeyboardButton(
            "ℹ️ Help",
            callback_data="help",
        ),
    )

    keyboard.add(
        types.InlineKeyboardButton(
            "➕ Add To Group",
            callback_data="group",
        ),
        types.InlineKeyboardButton(
            "🧹 Clear",
            callback_data="clear",
        ),
    )

    return keyboard


def game_kb():

    keyboard = types.InlineKeyboardMarkup(
        row_width=2
    )

    keyboard.add(
        types.InlineKeyboardButton(
            "🎯 Guess Number",
            callback_data="guess",
        ),
        types.InlineKeyboardButton(
            "🎲 Truth or Dare",
            callback_data="tod",
        ),
    )

    keyboard.add(
        types.InlineKeyboardButton(
            "🧩 Riddle",
            callback_data="riddle",
        ),
        types.InlineKeyboardButton(
            "🔥 Roast",
            callback_data="roast",
        ),
    )

    keyboard.add(
        types.InlineKeyboardButton(
            "⬅️ Back",
            callback_data="back",
        )
    )

    return keyboard


# ============================================================
# FUN
# ============================================================

JOKES = [
    "Maine diet start ki thi... phir samose ne aankhon mein aankhein daal di 😭",
    "WiFi slow aur salary khatam dono bina warning ke hote hain 💀",
    "Mera motivation Monday ke saath long-distance relationship mein hai 😂",
]

SHAYARI = [
    "Chai garam, mausam suhana, dost tu mil jaaye toh scene mastana ☕❤️",
    "Zindagi chhoti si hai, tension badi bana rakhi hai. Hans le bhai 😌",
]

FUN_LINES = [
    "🎯 Kisi dost ko bina context “mission successful” bhej.",
    "🧠 10 seconds mein 5 fruits ke naam bol.",
    "🎭 Apni life ko ek movie title de.",
]


def joke(message):

    bot.reply_to(
        message,
        "😂 " + random.choice(JOKES),
    )


def shayari(message):

    bot.reply_to(
        message,
        random.choice(SHAYARI),
    )


def fun(message):

    bot.reply_to(
        message,
        random.choice(FUN_LINES)
        + "\n\n"
        + random.choice(JOKES),
    )


def profile(message):

    packet = get_memory(
        message.from_user.id,
        message.from_user.first_name or "Dost",
    )

    profile_data = packet["profile"]

    bot.reply_to(
        message,
        "👤 Venu Profile\n\n"
        f"📌 Name: {profile_data.get('name')}\n"
        f"🎮 Game: {profile_data.get('favorite_game')}\n"
        f"🎬 Movie: {profile_data.get('favorite_movie')}\n"
        f"🧠 Mood: {profile_data.get('current_mood')}",
    )


def mem(message):

    packet = get_memory(
        message.from_user.id,
        message.from_user.first_name or "Dost",
    )

    profile_data = packet["profile"]

    bot.reply_to(
        message,
        "🧠 Memory\n\n"
        f"Name: {profile_data.get('name')}\n"
        f"Game: {profile_data.get('favorite_game')}\n"
        f"Hobbies: {profile_data.get('hobbies')}\n\n"
        f"💭 {packet.get('summary')}",
    )


def stats(message):

    uid = message.from_user.id

    rows = db.request(
        "GET",
        f"daily_stats?user_id=eq.{uid}"
        f"&order=date.desc&limit=7",
    ) or []

    total_messages = sum(
        int(
            row.get(
                "messages_sent",
                0,
            )
            or 0
        )
        for row in rows
    )

    total_games = sum(
        int(
            row.get(
                "games_played",
                0,
            )
            or 0
        )
        for row in rows
    )

    bot.reply_to(
        message,
        "📊 Stats\n\n"
        f"Messages: {total_messages}\n"
        f"Games: {total_games}",
    )


def help_(message):

    bot.reply_to(
        message,
        "ℹ️ Venu\n\n"
        "💬 Natural AI chat\n"
        "🎮 Guess / Truth-Dare / Riddle / Roast\n"
        "😂 Joke / Shayari / Fun\n"
        "🎙️ /voice /novoice\n"
        "🧠 /memory\n"
        "👤 /profile\n"
        "📊 /stats\n"
        "🧹 /clear\n"
        "🆔 /id\n"
        "🏓 /ping",
    )


def add_group(message):

    if not BOT_USERNAME:

        bot.reply_to(
            message,
            "Invite link abhi available nahi 😭",
        )

        return

    keyboard = types.InlineKeyboardMarkup()

    keyboard.add(
        types.InlineKeyboardButton(
            "➕ Add Venu To Group",
            url=(
                f"https://t.me/"
                f"{BOT_USERNAME}"
                f"?startgroup=true"
            ),
        )
    )

    bot.reply_to(
        message,
        "Group select karo 😎",
        reply_markup=keyboard,
    )


# ============================================================
# GAMES
# ============================================================

RIDDLES = [
    (
        "Tootne par awaaz nahi karti?",
        "khamoshi",
    ),
    (
        "Jitna nikaalo utna bada hota hai?",
        "gaddha",
    ),
    (
        "Keys hain, locks nahi; space hai, room nahi?",
        "keyboard",
    ),
]

TRUTHS = [
    "Sabse embarrassing moment?",
    "Weird talent kya hai?",
    "Kis cheez se instantly khush hote ho?",
]

DARES = [
    "Last emoji se funny sentence bana.",
    "Kisi friend ko “mission successful 🫡” bhej.",
    "Apni life ko movie title de.",
]

ROASTS = [
    "Teri typing dekh ke autocorrect bhi resign kar de 😂",
    "Confidence 4K mein, logic 144p mein 😭",
    "Tera plan solid tha... bas plan mein plan hi nahi tha 💀",
]


def start_game(
    message,
    game_type,
):

    uid = message.from_user.id

    game = {
        "type": game_type,
        "created": time.time(),
        "attempts": 0,
    }

    if game_type == "guess":

        game["secret"] = random.randint(
            1,
            50,
        )

        text = (
            "🎯 Guess Number!\n"
            "1–50 ke beech number bhej."
        )

    elif game_type == "tod":

        text = (
            "🎲 Truth or Dare?\n"
            "`truth` ya `dare` bhej."
        )

    elif game_type == "riddle":

        (
            game["question"],
            game["answer"],
        ) = random.choice(RIDDLES)

        text = (
            "🧩 "
            + game["question"]
        )

    else:

        text = (
            "🔥 Roast Battle!\n"
            "Koi line bhej, halka roast milega 😈"
        )

    with lock:
        games[uid] = game

    bot.send_message(
        message.chat.id,
        text,
        reply_markup=game_kb(),
    )


def game_process(
    message,
    text,
):

    uid = message.from_user.id

    with lock:
        game = games.get(uid)

    if not game:
        return False

    game_type = game["type"]

    value = text.strip().lower()

    if value in {
        "cancel",
        "/cancel",
        "exit",
        "quit",
    }:

        with lock:
            games.pop(uid, None)

        bot.reply_to(
            message,
            "🎮 Game cancel.",
        )

        return True

    # --------------------------------------------------------
    # GUESS
    # --------------------------------------------------------

    if game_type == "guess":

        try:
            number = int(value)

        except Exception:

            bot.reply_to(
                message,
                "🔢 Number bhej, jaise 27.",
            )

            return True

        if not 1 <= number <= 50:

            bot.reply_to(
                message,
                "1 se 50 ke beech 😭",
            )

            return True

        game["attempts"] += 1

        secret = game["secret"]

        if number == secret:

            attempts = game["attempts"]

            with lock:
                games.pop(uid, None)

            bot.reply_to(
                message,
                f"🎉 Correct! {secret} tha. "
                f"Attempts: {attempts}",
            )

        elif number < secret:

            bot.reply_to(
                message,
                "📈 Thoda bada try kar.",
            )

        else:

            bot.reply_to(
                message,
                "📉 Thoda chhota try kar.",
            )

        return True

    # --------------------------------------------------------
    # TRUTH OR DARE
    # --------------------------------------------------------

    if game_type == "tod":

        if value not in {
            "truth",
            "dare",
        }:

            bot.reply_to(
                message,
                "Sirf truth ya dare 😎",
            )

            return True

        with lock:
            games.pop(uid, None)

        if value == "truth":

            bot.reply_to(
                message,
                "🧠 Truth: "
                + random.choice(TRUTHS),
            )

        else:

            bot.reply_to(
                message,
                "🔥 Dare: "
                + random.choice(DARES),
            )

        return True

    # --------------------------------------------------------
    # RIDDLE
    # --------------------------------------------------------

    if game_type == "riddle":

        answer = game["answer"]

        correct = (
            value == answer
            or answer in value
            or SequenceMatcher(
                None,
                value,
                answer,
            ).ratio() >= 0.72
        )

        if correct:

            with lock:
                games.pop(uid, None)

            bot.reply_to(
                message,
                "🎉 Correct! Riddle master 🔥",
            )

        else:

            bot.reply_to(
                message,
                "❌ Nope 😭 Ek aur try.",
            )

        return True

    # --------------------------------------------------------
    # ROAST
    # --------------------------------------------------------

    if game_type == "roast":

        with lock:
            games.pop(uid, None)

        bot.reply_to(
            message,
            random.choice(ROASTS),
        )

        return True

    return False


# ============================================================
# GROUP
# ============================================================

def group_ok(message):

    if not RESPOND_IN_GROUPS:
        return False

    if message.chat.type not in {
        "group",
        "supergroup",
    }:

        return True

    text = message.text or ""

    if text.startswith("/"):
        return True

    if (
        BOT_USERNAME
        and f"@{BOT_USERNAME}" in text.lower()
    ):
        return True

    reply = message.reply_to_message

    if (
        reply
        and reply.from_user
        and BOT_ID
        and reply.from_user.id == BOT_ID
    ):
        return True

    return False


def strip_mention(text):

    if not BOT_USERNAME:
        return text.strip()

    return re.sub(
        rf"@{re.escape(BOT_USERNAME)}\b",
        "",
        text,
        flags=re.I,
    ).strip()


def name_prefix(
    uid,
    name,
):

    name = (name or "").strip()

    now = time.time()

    if (
        not name
        or len(name) > 30
        or not re.fullmatch(
            r"[\w .'-]+",
            name,
            re.UNICODE,
        )
    ):
        return ""

    with lock:
        last = name_time.get(uid, 0)

    if now - last < 600:
        return ""

    if random.random() > 0.12:
        return ""

    with lock:
        name_time[uid] = now

    return name + ", "


# ============================================================
# COMMANDS
# ============================================================

@bot.message_handler(
    commands=["start"]
)
def start(message):

    register_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )

    bot.reply_to(
        message,
        "Oye bhai! ✨ Main Venu hoon. "
        "Kya scene hai? 😎",
        reply_markup=main_kb(),
    )


@bot.message_handler(
    commands=["help"]
)
def command_help(message):
    help_(message)


@bot.message_handler(
    commands=["profile"]
)
def command_profile(message):
    profile(message)


@bot.message_handler(
    commands=["memory"]
)
def command_memory(message):
    mem(message)


@bot.message_handler(
    commands=["stats"]
)
def command_stats(message):
    stats(message)


@bot.message_handler(
    commands=["clear"]
)
def command_clear(message):

    clear_memory(
        message.from_user.id
    )

    bot.reply_to(
        message,
        "🧹 Memory clear. Fresh start 😌",
    )


@bot.message_handler(
    commands=["voice"]
)
def voice(message):

    tts_users.add(
        message.from_user.id
    )

    bot.reply_to(
        message,
        "🎙️ Voice replies ON.",
    )


@bot.message_handler(
    commands=["novoice"]
)
def novoice(message):

    tts_users.discard(
        message.from_user.id
    )

    bot.reply_to(
        message,
        "🔇 Voice replies OFF.",
    )


@bot.message_handler(
    commands=["joke"]
)
def command_joke(message):
    joke(message)


@bot.message_handler(
    commands=["shayari"]
)
def command_shayari(message):
    shayari(message)


@bot.message_handler(
    commands=["fun"]
)
def command_fun(message):
    fun(message)


@bot.message_handler(
    commands=["dice"]
)
def command_dice(message):

    bot.reply_to(
        message,
        f"🎲 {random.randint(1, 6)}",
    )


@bot.message_handler(
    commands=["coin"]
)
def command_coin(message):

    bot.reply_to(
        message,
        "🪙 "
        + random.choice(
            [
                "Heads!",
                "Tails!",
            ]
        ),
    )


@bot.message_handler(
    commands=["choose"]
)
def command_choose(message):

    raw = message.text.partition(
        " "
    )[2]

    options = [
        item.strip()
        for item in re.split(
            r"[,|]",
            raw,
        )
        if item.strip()
    ]

    if len(options) >= 2:

        bot.reply_to(
            message,
            "🎯 "
            + random.choice(options),
        )

    else:

        bot.reply_to(
            message,
            "Usage: /choose chai, coffee",
        )


@bot.message_handler(
    commands=["id"]
)
def command_id(message):

    bot.reply_to(
        message,
        f"🆔 User: {message.from_user.id}\n"
        f"💬 Chat: {message.chat.id}",
    )


@bot.message_handler(
    commands=["ping"]
)
def ping(message):

    started = time.perf_counter()

    try:

        sent = bot.reply_to(
            message,
            "🏓 Checking...",
        )

        milliseconds = round(
            (
                time.perf_counter()
                - started
            )
            * 1000,
            1,
        )

        try:

            bot.edit_message_text(
                f"🏓 Pong! {milliseconds} ms",
                message.chat.id,
                sent.message_id,
            )

        except Exception:
            pass

    except Exception:

        logger.exception(
            "ping error"
        )


@bot.message_handler(
    commands=["roast"]
)
def command_roast(message):

    start_game(
        message,
        "roast",
    )


# ============================================================
# CALLBACKS
# ============================================================

@bot.callback_query_handler(
    func=lambda call: True
)
def callback(call):

    try:

        try:
            bot.answer_callback_query(
                call.id
            )
        except Exception:
            pass

        message = call.message
        data = call.data

        if data == "back":

            bot.edit_message_text(
                "😎 Venu — kya karna hai?",
                message.chat.id,
                message.message_id,
                reply_markup=main_kb(),
            )

        elif data == "games":

            bot.edit_message_text(
                "🎮 Game choose kar:",
                message.chat.id,
                message.message_id,
                reply_markup=game_kb(),
            )

        elif data in {
            "guess",
            "tod",
            "riddle",
            "roast",
        }:

            start_game(
                message,
                data,
            )

        elif data == "talk":

            bot.send_message(
                message.chat.id,
                "Bol bhai 😎",
            )

        elif data == "memory":

            mem(message)

        elif data == "profile":

            profile(message)

        elif data == "fun":

            fun(message)

        elif data == "stats":

            stats(message)

        elif data == "voice":

            voice(message)

        elif data == "help":

            help_(message)

        elif data == "group":

            add_group(message)

        elif data == "clear":

            clear_memory(
                message.from_user.id
            )

            bot.edit_message_text(
                "🧹 Memory clear. Fresh start 😌",
                message.chat.id,
                message.message_id,
                reply_markup=main_kb(),
            )

    except Exception:

        logger.exception(
            "callback error"
        )


# ============================================================
# TEXT HANDLER
# ============================================================

def text_handler(message):

    typing = None

    try:

        if not group_ok(message):
            return

        uid = message.from_user.id

        text = strip_mention(
            message.text or ""
        ).strip()

        if not text:
            return

        now = time.time()

        with lock:

            previous = last_msg.get(uid)

            last_msg[uid] = now

            activity[uid] = now

        if (
            previous
            and now - previous < 0.15
        ):
            return

        register_user(
            uid,
            message.from_user.username,
            message.from_user.first_name,
        )

        actions = {
            "🎮 Guess Number": "guess",
            "🔥 Roast Battle": "roast",
            "🎯 Truth or Dare": "tod",
            "🧩 Riddle Battle": "riddle",
        }

        if text in actions:

            start_game(
                message,
                actions[text],
            )

            return

        if text == "😂 Joke":

            joke(message)
            return

        if text == "❤️ Shayari":

            shayari(message)
            return

        if text == "🎲 Fun Zone":

            fun(message)
            return

        if text == "📊 My Stats":

            stats(message)
            return

        if text == "🧠 My Memory":

            mem(message)
            return

        if text in {
            "👤 My Profile",
            "👤 View Profile",
        }:

            profile(message)
            return

        if text == "🎙️ Voice Mode":

            voice(message)
            return

        if text == "ℹ️ Help":

            help_(message)
            return

        if text == "➕ Add Me In Group":

            add_group(message)
            return

        if text == "🧹 Clear Chat":

            clear_memory(uid)

            bot.reply_to(
                message,
                "🧹 Memory clear. Fresh start 😌",
            )

            return

        # ----------------------------------------------------
        # GAME
        # ----------------------------------------------------

        if game_process(
            message,
            text,
        ):

            daily(
                uid,
                True,
            )

            return

        # ----------------------------------------------------
        # CALCULATOR
        # ----------------------------------------------------

        result = calc(text)

        if result is not None:

            bot.reply_to(
                message,
                f"🧮 {result}",
            )

            daily(uid)

            return

        # ----------------------------------------------------
        # AI
        # ----------------------------------------------------

        typing = Typing(
            message.chat.id
        )

        typing.start()

        save_message(
            uid,
            "user",
            text,
        )

        packet = get_memory(
            uid,
            message.from_user.first_name
            or "Dost",
        )

        reply, detected_mood = ai(
            uid,
            packet,
            text,
        )

        prefix = name_prefix(
            uid,
            message.from_user.first_name,
        )

        if prefix:
            reply = prefix + reply

        reply = clean_reply(
            reply
        )

        update_profile(
            uid,
            "current_mood",
            detected_mood,
        )

        save_message(
            uid,
            "assistant",
            reply,
        )

        daily(uid)

        typing.close()
        typing = None

        bot.reply_to(
            message,
            reply,
        )

        if (
            uid in tts_users
            and ENABLE_TTS
        ):

            threading.Thread(
                target=tts,
                args=(
                    message.chat.id,
                    reply,
                ),
                daemon=True,
            ).start()

    except Exception:

        logger.exception(
            "text handler error"
        )

        if typing:
            typing.close()

        try:

            bot.reply_to(
                message,
                "Bhai ek sec, connection hiccup hua 😭 "
                "phir se bol.",
            )

        except Exception:
            pass


bot.message_handler(
    content_types=["text"]
)(text_handler)


# ============================================================
# VOICE
# ============================================================

def transcribe(message):

    try:

        file_info = bot.get_file(
            message.voice.file_id
        )

        data = bot.download_file(
            file_info.file_path
        )

        with tempfile.TemporaryDirectory() as directory:

            ogg_path = os.path.join(
                directory,
                "audio.ogg",
            )

            wav_path = os.path.join(
                directory,
                "audio.wav",
            )

            with open(
                ogg_path,
                "wb",
            ) as file:

                file.write(data)

            AudioSegment.from_file(
                ogg_path
            ).export(
                wav_path,
                format="wav",
            )

            recognizer = sr.Recognizer()

            with sr.AudioFile(
                wav_path
            ) as source:

                audio = recognizer.record(
                    source
                )

            return recognizer.recognize_google(
                audio,
                language="hi-IN",
            )

    except sr.UnknownValueError:

        return None

    except Exception:

        logger.exception(
            "transcription error"
        )

        return None


def tts(
    chat_id,
    text,
):

    try:

        with tempfile.TemporaryDirectory() as directory:

            audio_path = os.path.join(
                directory,
                "venu.mp3",
            )

            gTTS(
                text=text,
                lang="hi",
            ).save(
                audio_path
            )

            with open(
                audio_path,
                "rb",
            ) as audio_file:

                bot.send_voice(
                    chat_id,
                    audio_file,
                    caption="🎙️ Venu",
                )

    except Exception:

        logger.exception(
            "TTS error"
        )


@bot.message_handler(
    content_types=["voice"]
)
def voice_handler(message):

    typing = Typing(
        message.chat.id
    )

    try:

        if not group_ok(message):
            return

        typing.start()

        uid = message.from_user.id

        register_user(
            uid,
            message.from_user.username,
            message.from_user.first_name,
        )

        text = transcribe(
            message
        )

        if not text:

            typing.close()

            bot.reply_to(
                message,
                "🎙️ Awaaz clear nahi aayi 😭",
            )

            return

        save_message(
            uid,
            "user",
            "[Voice] " + text,
        )

        packet = get_memory(
            uid,
            message.from_user.first_name
            or "Dost",
        )

        reply, detected_mood = ai(
            uid,
            packet,
            text,
        )

        reply = clean_reply(
            reply
        )

        update_profile(
            uid,
            "current_mood",
            detected_mood,
        )

        save_message(
            uid,
            "assistant",
            reply,
        )

        daily(uid)

        typing.close()

        bot.reply_to(
            message,
            "🎙️ " + reply,
        )

        if (
            uid in tts_users
            and ENABLE_TTS
        ):

            threading.Thread(
                target=tts,
                args=(
                    message.chat.id,
                    reply,
                ),
                daemon=True,
            ).start()

    except Exception:

        logger.exception(
            "voice handler error"
        )

        typing.close()


# ============================================================
# ADMIN
# ============================================================

def is_admin(message):

    return bool(
        ADMIN_ID
        and message.from_user
        and message.from_user.id == ADMIN_ID
    )


@bot.message_handler(
    commands=["refresh"]
)
def refresh(message):

    if not is_admin(message):

        bot.reply_to(
            message,
            "⛔ Admin only.",
        )

        return

    with lock:

        memory.clear()
        registered.clear()
        recent_replies.clear()
        games.clear()
        last_msg.clear()
        name_time.clear()
        activity.clear()

    bot.reply_to(
        message,
        "♻️ State refreshed.",
    )


@bot.message_handler(
    commands=["broadcast"]
)
def broadcast(message):

    if not is_admin(message):

        bot.reply_to(
            message,
            "⛔ Admin only.",
        )

        return

    text = message.text.partition(
        " "
    )[2].strip()

    if not text:

        bot.reply_to(
            message,
            "Usage: /broadcast your message",
        )

        return

    rows = db.request(
        "GET",
        "users?select=user_id",
        timeout=10,
    ) or []

    bot.reply_to(
        message,
        f"📢 Sending to {len(rows)} users...",
    )

    def worker():

        success = 0
        failed = 0

        for row in rows:

            try:

                user_id = int(
                    row["user_id"]
                )

                bot.send_message(
                    user_id,
                    text,
                )

                success += 1

                time.sleep(
                    0.05
                )

            except Exception:

                failed += 1

        try:

            bot.send_message(
                message.chat.id,
                "📢 Done\n"
                f"✅ {success}\n"
                f"❌ {failed}",
            )

        except Exception:
            pass

    threading.Thread(
        target=worker,
        daemon=True,
    ).start()


# ============================================================
# CLEANUP
# ============================================================

def cleanup():

    while True:

        time.sleep(300)

        try:

            now = time.time()

            with lock:

                for uid, game in list(
                    games.items()
                ):

                    created = game.get(
                        "created",
                        now,
                    )

                    if now - created > 1800:

                        games.pop(
                            uid,
                            None,
                        )

                for uid, last_activity in list(
                    activity.items()
                ):

                    if (
                        now - last_activity
                        > 7200
                    ):

                        activity.pop(
                            uid,
                            None,
                        )

                        last_msg.pop(
                            uid,
                            None,
                        )

        except Exception:

            logger.exception(
                "cleanup error"
            )


# ============================================================
# TELEGRAM POLLING
# ============================================================

def start_polling():

    """
    Starts Telegram long polling.

    IMPORTANT:
    Telegram allows only ONE getUpdates
    consumer for a bot token.

    If another Render service/local Python
    process is using the same BOT_TOKEN,
    Telegram returns:

        409 Conflict:
        terminated by other getUpdates request
    """

    reconnect_delay = 5

    while True:

        try:

            logger.info(
                "Starting Telegram polling..."
            )

            bot.infinity_polling(
                timeout=25,
                long_polling_timeout=25,
                skip_pending=True,
                allowed_updates=[
                    "message",
                    "callback_query",
                ],
                none_stop=False,
            )

            logger.warning(
                "Telegram polling stopped."
            )

            reconnect_delay = 5

        except KeyboardInterrupt:

            logger.info(
                "Polling stopped by keyboard."
            )

            break

        except ApiTelegramException as error:

            error_text = str(error)

            if (
                "409" in error_text
                or "Conflict" in error_text
                or "terminated by other getUpdates"
                in error_text
            ):

                logger.error(
                    "=================================================="
                )

                logger.error(
                    "TELEGRAM 409 CONFLICT"
                )

                logger.error(
                    "Another bot instance is using "
                    "the same BOT_TOKEN."
                )

                logger.error(
                    "Stop the other local/Render instance "
                    "before polling again."
                )

                logger.error(
                    "=================================================="
                )

                # Do not hammer Telegram continuously.
                time.sleep(15)

                continue

            logger.exception(
                "Telegram API error"
            )

            time.sleep(
                reconnect_delay
            )

        except Exception:

            logger.exception(
                "Polling crashed; reconnecting"
            )

            time.sleep(
                reconnect_delay
            )

            reconnect_delay = min(
                reconnect_delay * 2,
                60,
            )


# ============================================================
# MAIN
# ============================================================

def main():

    logger.info(
        "=============================================="
    )

    logger.info(
        "🚀 Venu production bot starting"
    )

    logger.info(
        "AI Base URL: %s",
        AI_BASE_URL,
    )

    logger.info(
        "AI Model: %s",
        AI_MODEL,
    )

    logger.info(
        "AI Timeout: %s",
        AI_TIMEOUT,
    )

    logger.info(
        "Telegram Bot: @%s",
        BOT_USERNAME,
    )

    logger.info(
        "Telegram Bot ID: %s",
        BOT_ID,
    )

    logger.info(
        "=============================================="
    )

    # Flask health server
    threading.Thread(
        target=run_flask,
        daemon=True,
    ).start()

    # Cleanup worker
    threading.Thread(
        target=cleanup,
        daemon=True,
    ).start()

    # Remove webhook before polling
    try:

        logger.info(
            "Removing Telegram webhook..."
        )

        bot.remove_webhook()

        time.sleep(1)

    except Exception:

        logger.exception(
            "remove webhook failed"
        )

    # Start polling
    start_polling()


# ============================================================
# ENTRY
# ============================================================

if __name__ == "__main__":

    main()
