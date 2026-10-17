import os
import re
import string
import time
import logging
import asyncio
import random
import httpx

# Setup logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Sembunyikan log internal MTProtoSender Telethon agar terminal bersih
logging.getLogger("telethon.network.mtprotosender").setLevel(logging.WARNING)
logging.getLogger("telethon.network.telegrambarebodysender").setLevel(logging.WARNING)

# Load dotenv jika dijalankan secara lokal
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telegram import (
    Update, InlineQueryResultArticle, InputTextMessageContent,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    InlineQueryHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
)
# [TAMBAHAN] untuk fitur auto-ban saat left channel
from telegram import ChatMember
from telegram.ext import ChatMemberHandler

# Configuration from Environment Variables
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# [TAMBAHAN] Channel yang dipantau untuk auto-ban saat user left (bot harus admin di channel ini)
AUTOBAN_CHANNELS = {-1003882339899}

DATA_DIR = "./"
BAN_FILE = f"{DATA_DIR}banned.txt"
USER_FILE = f"{DATA_DIR}users.txt"
WATCH_FILE = f"{DATA_DIR}watch.txt"

# Pengaturan pemantauan (bisa diubah lewat env)
# /pantau selalu lewat web (t.me), TIDAK pakai akun Telethon.
# Akun Telethon hanya dipakai untuk scan username available (inline) dan /cek.
WATCH_INTERVAL = float(os.getenv("WATCH_INTERVAL", "20"))  # jeda antar putaran (detik)
WATCH_DELAY_MIN = float(os.getenv("WATCH_DELAY_MIN", "1.0"))  # jeda antar cek username
WATCH_DELAY_MAX = float(os.getenv("WATCH_DELAY_MAX", "2.0"))

BANNED_USERS = set()
clients = []
CLIENT_BY_SLOT = {}
client_cooldown = {}
client_acc = {}
client_index = 0
ALL_USERS = set()

# Cache sementara hasil scan per inline message id
SCAN_CACHE = {}

# Pantauan aktif: {(mode_key, base): asyncio.Task}
WATCHERS = {}

# Username valid untuk scan/pantau (cek available): minimal 5 karakter
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{4,31}$")
# Username valid untuk list varian: format tetap valid, tapi tanpa batas minimal huruf
USERNAME_LOOSE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def min_base_len(mode_key):
    """Minimal huruf username dasar untuk scan/pantau (cek available).
    ganhur & switch = 5, mode lain (penambahan satu huruf dll) = 4."""
    m = mode_key.split(".")[0]
    return 5 if m in ("ganhur", "switch") else 4


# ================== PERSISTENCE & BAN MANAGEMENT ==================
def load_users():
    if os.path.exists(USER_FILE):
        with open(USER_FILE, "r") as f:
            for line in f:
                if line.strip(): ALL_USERS.add(int(line.strip()))

def save_user(user_id):
    if user_id not in ALL_USERS:
        ALL_USERS.add(user_id)
        with open(USER_FILE, "a") as f:
            f.write(f"{user_id}\n")

def load_bans():
    if os.path.exists(BAN_FILE):
        with open(BAN_FILE, "r") as f:
            for line in f:
                if line.strip(): BANNED_USERS.add(int(line.strip()))

def save_ban(user_id):
    BANNED_USERS.add(user_id)
    with open(BAN_FILE, "w") as f:
        for uid in BANNED_USERS:
            f.write(f"{uid}\n")

def remove_ban(user_id):
    if user_id in BANNED_USERS:
        BANNED_USERS.remove(user_id)
        with open(BAN_FILE, "w") as f:
            for uid in BANNED_USERS:
                f.write(f"{uid}\n")

def save_watchlist():
    try:
        with open(WATCH_FILE, "w") as f:
            for (mode_key, base) in WATCHERS:
                f.write(f"{mode_key}|{base}\n")
    except Exception as e:
        logger.error(f"Gagal menyimpan watchlist: {e}")

def load_watchlist():
    items = []
    if os.path.exists(WATCH_FILE):
        with open(WATCH_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if "|" in line:
                    mode_key, base = line.split("|", 1)
                    items.append((mode_key, base))
    return items

# ================== NOTIFY ADMIN ==================
async def notify_admin(context: ContextTypes.DEFAULT_TYPE, user, action_type: str, details: str = ""):
    if not ADMIN_ID:
        return

    first_name = user.first_name or ""
    last_name = user.last_name or ""
    full_name = f"{first_name} {last_name}".strip()
    username = f"@{user.username}" if user.username else "Tidak ada username"
    user_id = user.id

    text = (
        f"👤 **Aktivitas Pengguna**\n"
        f"• Aksi: {action_type}\n"
        f"• Nama: {full_name}\n"
        f"• Username: {username}\n"
        f"• ID: `{user_id}`"
    )
    if details:
        text += f"\n• **Detail:** `{details}`"

    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Gagal mengirim notifikasi ke admin: {e}")

# ================== GENERATORS ==================
rata, tdk_rata, vokal = "asweruiozxcvnm", "qtypdfghjklb", "aeiou"

def gen_tamhur(b, letters=string.ascii_lowercase): return list({b[:i] + l + b[i:] for i in range(len(b)+1) for l in letters})
def gen_tamping(b, letters=string.ascii_lowercase): return list({l + b for l in letters} | {b + l for l in letters})
# tamdal = tambah dalam: huruf disisipkan di antara huruf lain, bukan di depan/belakang
def gen_tamdal(b, letters=string.ascii_lowercase): return list({b[:i] + l + b[i:] for i in range(1, len(b)) for l in letters})
def gen_switch(b):
    res = set()
    for i in range(len(b) - 1):
        lst = list(b); lst[i], lst[i+1] = lst[i+1], lst[i]; res.add("".join(lst))
    return list(res)
def gen_uncommon(b): return list({b[:i] + b[i] + b[i:] for i in range(len(b))}) if b else []
def gen_ganhur(b): return list({b[:i] + l + b[i+1:] for i in range(len(b)) for l in string.ascii_lowercase})
def gen_kurhur(b): return list({b[:i] + b[i+1:] for i in range(len(b))}) if len(b) > 1 else []
def gen_canon(b):
    res = {b + 's'}; m = {'i': 'l', 'l': 'i'}
    for i, char in enumerate(b):
        if char in m: res.add(b[:i] + m[char] + b[i+1:])
    return list(res)
def gen_rata(b): return list({b[:i] + l + b[i:] for i in range(len(b)+1) for l in rata})
def gen_tidakrata(b): return list({b[:i] + l + b[i:] for i in range(len(b)+1) for l in tdk_rata})
def gen_vokal(b): return list({b[:i] + l + b[i:] for i in range(len(b)+1) for l in vokal})

GENERATORS = {
    "switch": (gen_switch, "switch"),
    "tamping": (gen_tamping, "tamping"),
    "tamhur": (gen_tamhur, "tamhur"),
    "tamdal": (gen_tamdal, "tamdal"),
    "ganhur": (gen_ganhur, "ganhur"),
    "uncommon": (gen_uncommon, "uncommon"),
    "kurhur": (gen_kurhur, "kurhur"),
    "rata": (gen_rata, "rata"),
    "tidakrata": (gen_tidakrata, "tidak rata"),
    "vokal": (gen_vokal, "vokal"),
}

# Pilihan set huruf untuk mode tamping / tamhur / tamdal
LETTER_SETS = {
    "rata": rata,
    "tidakrata": tdk_rata,
    "vokal": vokal,
    "cadel": "wycl",
}
LETTER_LABELS = {"all": "semua", "rata": "rata", "tidakrata": "tidak rata", "vokal": "vokal", "cadel": "cadel", "uncommon": "uncommon"}
LETTER_MODES = ("tamping", "tamhur", "tamdal")

def resolve_mode(mode_key):
    """'tamping' atau 'tamping.rata' -> (fungsi_generator, label). None kalau tidak valid.
    'tamping.uncommon' = pilihan uncommon di list varian tamping/tamhur/tamdal
    (isinya sama dengan mode uncommon)."""
    if "." in mode_key:
        m, ls = mode_key.split(".", 1)
        if m in LETTER_MODES and ls == "uncommon":
            return GENERATORS["uncommon"]
        if m in LETTER_MODES and ls in LETTER_SETS:
            func, mlabel = GENERATORS[m]
            letters = LETTER_SETS[ls]
            return (lambda b, _f=func, _l=letters: _f(b, _l)), f"{mlabel} {LETTER_LABELS[ls]}"
        return None
    return GENERATORS.get(mode_key)

def is_valid_mode(mode_key):
    return resolve_mode(mode_key) is not None

# ================== CORE LOGIC ==================
async def init_clients():
    if not API_ID or not API_HASH:
        logger.error("❌ API_ID atau API_HASH kosong!")
        return
    for i in range(1, 21):
        session_str = os.getenv(f"SESSION_{i}")
        if not session_str:
            continue
        try:
            c = TelegramClient(StringSession(session_str), int(API_ID), API_HASH)
            await c.connect()
            if await c.is_user_authorized():
                clients.append(c)
                CLIENT_BY_SLOT[i] = c
                client_cooldown[c] = 0
                client_acc[c] = i
                logger.info(f"✅ acc{i} (StringSession) Ready")
            else:
                await c.disconnect()
        except Exception as e:
            logger.debug(f"Gagal memuat SESSION_{i}: {e}")

def get_available_client():
    global client_index
    now = time.time()
    available = [c for c in clients if client_cooldown[c] <= now]
    if not available: return None
    client = available[client_index % len(available)]
    client_index += 1
    return client

def chunk_results(items, chunk_size=15):
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]

def build_pagination_keyboard(current_page, total_pages, target_base, mode_key):
    if total_pages <= 1:
        return None

    buttons = []
    for i in range(total_pages):
        label = f"• {i+1} •" if i == current_page else f"{i+1}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"page_{mode_key}_{target_base}_{i}"))

    return InlineKeyboardMarkup([buttons])

# ================== USERNAME WATCHER (ADMIN ONLY, WEB ONLY) ==================
def build_candidates(mode_key, base, strict=True):
    """strict=True  -> untuk scan/pantau (cek available): ada minimal huruf
                       (base min 4, ganhur/switch min 5, hasil min 5 karakter).
       strict=False -> untuk list varian: tanpa minimal huruf."""
    if strict and len(base) < min_base_len(mode_key):
        return []
    gen_func, _ = resolve_mode(mode_key)
    raw = gen_func(base)
    if mode_key == "uncommon" or mode_key.endswith(".uncommon"):
        raw += gen_canon(base)
    pattern = USERNAME_RE if strict else USERNAME_LOOSE_RE
    result = []
    for u in sorted(set(raw)):
        if u == base:
            continue
        if not pattern.match(u):
            continue
        if u.endswith("_") or "__" in u:
            continue
        result.append(u)
    return result

# ---------- Pantauan lewat web (tanpa akun Telethon) ----------
WEB_CLIENT = None

def get_web_client():
    global WEB_CLIENT
    if WEB_CLIENT is None:
        WEB_CLIENT = httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
        )
    return WEB_CLIENT

async def check_web(u):
    """Cek lewat halaman t.me/<u>.
    Return: 'free' (kelihatannya kosong), 'taken' (ada pemiliknya),
    'limit' (kena rate limit), 'unknown' (gagal / halaman tidak jelas)."""
    try:
        r = await get_web_client().get(f"https://t.me/{u}")
    except Exception:
        return "unknown"
    if r.status_code == 429:
        return "limit"
    if r.status_code != 200:
        return "unknown"
    html = r.text
    if "tgme" not in html:
        return "unknown"
    # username yang dipakai punya blok nama/foto profil, yang kosong tidak
    if "tgme_page_title" in html or "tgme_page_photo" in html:
        return "taken"
    return "free"

async def watch_loop_web(app, mode_key, base):
    """Pantau terus-menerus lewat web. Kirim notif tiap ada yang available,
    tapi pantauan TIDAK berhenti sampai admin /batal."""
    key = (mode_key, base)
    lbl = resolve_mode(mode_key)[1]
    candidates = build_candidates(mode_key, base)
    notified = set()  # username yang sudah dinotif sebagai available
    stop_cmd = f"/batal {mode_key.replace('.', ' ')} {base}"

    if not candidates:
        await app.bot.send_message(
            ADMIN_ID,
            f"❌ Tidak ada variasi valid untuk @{base} ({lbl}). "
            f"Minimal {min_base_len(mode_key)} huruf untuk mode ini."
        )
        WATCHERS.pop(key, None)
        save_watchlist()
        return

    logger.info(f"🌐 Mulai memantau (web) @{base} ({lbl}) - {len(candidates)} variasi")

    try:
        while True:
            for u in candidates:
                state = "unknown"
                for _ in range(3):
                    state = await check_web(u)
                    if state != "limit":
                        break
                    logger.warning("⚠️ t.me rate limit, nunggu 60 detik")
                    await asyncio.sleep(60)

                if state == "free" and u not in notified:
                    # cek ulang sekali biar tidak salah notif
                    await asyncio.sleep(random.uniform(2, 4))
                    if await check_web(u) == "free":
                        notified.add(u)
                        try:
                            await app.bot.send_message(
                                ADMIN_ID,
                                f"🟢 @{u} kelihatannya available!\n"
                                f"Dari pantauan: {lbl} {base}\n"
                                f"(dicek lewat web, belum tentu bisa diambil, coba langsung)\n"
                                f"Pantauan tetap jalan. Stop: {stop_cmd}"
                            )
                        except Exception as e:
                            logger.error(f"Gagal kirim notif available: {e}")
                elif state == "taken" and u in notified:
                    # sudah diambil lagi -> kalau nanti kosong lagi, dinotif ulang
                    notified.discard(u)

                await asyncio.sleep(random.uniform(WATCH_DELAY_MIN, WATCH_DELAY_MAX))

            await asyncio.sleep(WATCH_INTERVAL)
    except asyncio.CancelledError:
        logger.info(f"🛑 Pantauan (web) @{base} ({lbl}) dibatalkan")
        raise
    except Exception as e:
        logger.error(f"Watcher web @{base} ({lbl}) error: {e}")
        WATCHERS.pop(key, None)
        save_watchlist()
        try:
            await app.bot.send_message(ADMIN_ID, f"❌ Pantauan {lbl} {base} berhenti karena error: {e}")
        except Exception:
            pass

async def cek_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cek <username> : tes hasil cek web (dan bandingkan dengan akun kalau ada)."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Format: /cek <username>")
        return
    u = context.args[0].lower().lstrip("@")
    text = f"Web (t.me): {await check_web(u)}"
    if clients:
        c = get_available_client()
        if c:
            try:
                ok = await asyncio.wait_for(c(functions.account.CheckUsernameRequest(u)), timeout=8.0)
                text += f"\nAkun: {'available' if ok else 'tidak available'}"
            except Exception as e:
                text += f"\nAkun: error {type(e).__name__}"
    await update.message.reply_text(text)

async def limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/limit : lihat status cooldown tiap akun Telethon."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not clients:
        await update.message.reply_text("❌ Tidak ada akun Telethon yang aktif (cek SESSION_n / API_ID / API_HASH).")
        return
    now = time.time()
    lines = []
    for c in clients:
        sisa = int(client_cooldown[c] - now)
        acc = client_acc.get(c, "?")
        if sisa <= 0:
            lines.append(f"acc{acc}: ✅ ready")
        else:
            h, rem = divmod(sisa, 3600)
            m, s = divmod(rem, 60)
            lines.append(f"acc{acc}: ⏳ limit {h}j {m}m {s}d lagi")
    await update.message.reply_text("\n".join(lines))

def start_watch(app, mode_key, base):
    key = (mode_key, base)
    task = WATCHERS.get(key)
    if task and not task.done():
        return False
    WATCHERS[key] = asyncio.create_task(watch_loop_web(app, mode_key, base))
    save_watchlist()
    return True

def cancel_watch(mode_key, base):
    task = WATCHERS.pop((mode_key, base), None)
    save_watchlist()
    if task and not task.done():
        task.cancel()
        return True
    return False

def render_watch_list():
    if not WATCHERS:
        return "Belum ada username yang dipantau.", None
    lines = [f"• {m.replace('.', ' ')} {b}" for (m, b) in WATCHERS]
    kb = [
        [InlineKeyboardButton(f"❌ Batal {m.replace('.', ' ')} {b}", callback_data=f"stopwatch_{m}_{b}")]
        for (m, b) in WATCHERS
    ]
    return "Sedang dipantau:\n" + "\n".join(lines), InlineKeyboardMarkup(kb)

def parse_mode_base(args):
    """['tamping','rata','adnan'] -> ('tamping.rata','adnan'); ['tamping','adnan'] -> ('tamping','adnan');
    ['adnan'] -> ('tamhur','adnan')"""
    args = [a.lower().lstrip("@") for a in args if a]
    if not args:
        return None, None
    if len(args) >= 3 and args[0] in LETTER_MODES and (args[1] in LETTER_SETS or args[1] == "uncommon"):
        return f"{args[0]}.{args[1]}", args[2]
    if len(args) >= 2 and is_valid_mode(args[0]):
        return args[0], args[1]
    return "tamhur", args[0]

async def pantau_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    mode_key, base = parse_mode_base(context.args)
    if not base:
        await update.message.reply_text(
            "Format: /pantau <mode> <username>\n"
            "Contoh: /pantau tamping adnan\n"
            "Mode: " + ", ".join(GENERATORS.keys()) + "\n"
            "Pilihan huruf (tamping/tamhur/tamdal): rata, tidakrata, vokal, cadel\n"
            "Contoh: /pantau tamping rata adnan\n"
            "Minimal huruf: ganhur & switch 5, mode lain 4\n"
            "Pantauan lewat web (tanpa akun), jalan terus sampai kamu /batal."
        )
        return

    min_len = min_base_len(mode_key)
    if len(base) < min_len:
        await update.message.reply_text(
            f"❌ @{base} cuma {len(base)} huruf. "
            f"Mode {mode_key.replace('.', ' ')} butuh minimal {min_len} huruf."
        )
        return

    total = len(build_candidates(mode_key, base))
    if total == 0:
        await update.message.reply_text("❌ Tidak ada variasi valid dari input itu.")
        return

    if start_watch(context.application, mode_key, base):
        await update.message.reply_text(
            f"👁️ Mulai memantau {mode_key.replace('.', ' ')} {base} ({total} variasi) lewat web.\n"
            f"Kamu dapat notif tiap ada yang available, pantauan jalan terus sampai kamu stop.\n"
            f"Batalin: /batal {mode_key.replace('.', ' ')} {base}"
        )
    else:
        await update.message.reply_text(f"{mode_key.replace('.', ' ')} {base} sudah dipantau.")

async def batal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if context.args and context.args[0].lower() == "semua":
        n = 0
        for (m, b) in list(WATCHERS.keys()):
            if cancel_watch(m, b):
                n += 1
        await update.message.reply_text(f"✅ {n} pantauan dibatalkan.")
        return

    mode_key, base = parse_mode_base(context.args)
    if not base:
        await update.message.reply_text(
            "Format: /batal <mode> <username>\n"
            "Contoh: /batal tamping adnan\n"
            "Atau /batal semua, atau lihat /daftar"
        )
        return

    if cancel_watch(mode_key, base):
        await update.message.reply_text(f"✅ Pantauan {mode_key.replace('.', ' ')} {base} dibatalkan.")
    else:
        await update.message.reply_text(f"Tidak ada pantauan {mode_key.replace('.', ' ')} {base}.")

async def daftar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    text, markup = render_watch_list()
    await update.message.reply_text(text, reply_markup=markup)

# ================== DAFTAR VARIAN (INLINE) ==================
# Batas karakter isi per halaman (limit Telegram 4096, sisanya buat header).
# Username disambung dengan spasi tanpa \n, jadi base pendek = banyak per halaman,
# base panjang = lebih sedikit per halaman.
VL_CHAR_BUDGET = 500

# Mode yang bisa dipilih lewat tombol di hasil list varian
VL_SWITCH_MODES = ("tamhur", "tamping", "tamdal", "uncommon", "ganhur")

def paginate_variants(items):
    """Bagi varian ke halaman-halaman berdasarkan total karakter, bukan jumlah tetap."""
    pages, cur, cur_len = [], [], 0
    for u in items:
        add = len(u) + 2  # "@" + username + spasi pemisah
        if cur and cur_len + add > VL_CHAR_BUDGET:
            pages.append(cur)
            cur, cur_len = [], 0
        cur.append(u)
        cur_len += add
    if cur:
        pages.append(cur)
    return pages

def render_variant_page(mode_key, base, page):
    """Return (text, markup, jumlah_varian). Tanpa cek available dan tanpa minimal huruf.
    Semua varian disambung dengan spasi (tanpa \\n) biar hemat halaman."""
    items = build_candidates(mode_key, base, strict=False)
    lbl = resolve_mode(mode_key)[1]
    if not items:
        return f"tidak ada varian valid untuk @{base} ({lbl})", None, 0

    pages = paginate_variants(items)
    total_pages = len(pages)
    page = min(max(page, 0), total_pages - 1)
    # teks polos (tanpa Markdown), disambung spasi biasa
    body = " ".join(f"@{u}" for u in pages[page])

    header = f"varian @{base} ({lbl}) ada {len(items)} usn"
    if total_pages > 1:
        header += f"\n{page + 1}/{total_pages}"
    text = header + "\n\n" + body
    return text, variant_keyboard(mode_key, base, page, total_pages), len(items)

def variant_keyboard(mode_key, base, page, total_pages):
    rows = []
    if total_pages > 1:
        def pbtn(label, i):
            data = f"vl_{mode_key}_{base}_{i}"
            if len(data.encode()) > 64:
                return None
            return InlineKeyboardButton(label, callback_data=data)

        if total_pages <= 10:
            btns = [pbtn(f"• {i + 1} •" if i == page else f"{i + 1}", i) for i in range(total_pages)]
            btns = [b for b in btns if b]
            for i in range(0, len(btns), 5):
                rows.append(btns[i:i + 5])
        else:
            nav = [
                pbtn("« 1", 0),
                pbtn("‹", max(page - 1, 0)),
                pbtn(f"{page + 1}/{total_pages}", page),
                pbtn("›", min(page + 1, total_pages - 1)),
                pbtn(f"{total_pages} »", total_pages - 1),
            ]
            rows.append([b for b in nav if b])

    base_mode = mode_key.split(".")[0]
    # 'tamping.uncommon' isinya sama dengan uncommon, jadi ditandai sebagai uncommon
    active_mode = "uncommon" if mode_key.endswith(".uncommon") else base_mode

    # Baris pilihan mode: tamhur / tamping / tamdal / uncommon / ganhur
    mbtns = []
    for m in VL_SWITCH_MODES:
        mdata = f"vl_{m}_{base}_0"
        if len(mdata.encode()) > 64:
            continue
        label = GENERATORS[m][1]
        mbtns.append(InlineKeyboardButton(f"• {label} •" if m == active_mode else label, callback_data=mdata))
    for i in range(0, len(mbtns), 3):
        rows.append(mbtns[i:i + 3])

    # Pilihan set huruf, khusus mode tamping / tamhur / tamdal
    if base_mode in LETTER_MODES and active_mode != "uncommon":
        cur = mode_key.split(".")[1] if "." in mode_key else "all"
        fbtns = []
        for key, label in LETTER_LABELS.items():
            if key == "uncommon":
                continue  # uncommon sudah ada di baris mode
            target = base_mode if key == "all" else f"{base_mode}.{key}"
            data = f"vl_{target}_{base}_0"
            if len(data.encode()) > 64:
                continue
            fbtns.append(InlineKeyboardButton(f"• {label} •" if key == cur else label, callback_data=data))
        for i in range(0, len(fbtns), 2):
            rows.append(fbtns[i:i + 2])

    return InlineKeyboardMarkup(rows) if rows else None

# ================== INLINE HANDLER ==================
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query.strip()
    user = update.inline_query.from_user
    uid = user.id

    if uid in BANNED_USERS:
        return

    save_user(uid)

    if not query:
        results = [
            InlineQueryResultArticle(
                id="info",
                title="⚠️ hi",
                description="iya hi",
                input_message_content=InputTextMessageContent(
                    "@rsunless's"
                )
            ),
            InlineQueryResultArticle(
                id="help",
                title="misal",
                description="anjay, uncommon anjay, tamping anjay, ganhur anjay, dll",
                input_message_content=InputTextMessageContent(
                    "Contoh penggunaan:\n"
                    " @sunless2bot adnan\n"
                    " @sunless2bot uncommon adnan\n"
                    " @sunless2bot tamdal adnan\n"
                    " @sunless2bot tamping rata adnan"
                )
            )
        ]
        await update.inline_query.answer(results, cache_time=1)
        return

    mode_key, base = parse_mode_base(query.replace("@", " ").split())
    if not base:
        return
    mode_label = resolve_mode(mode_key)[1]

    min_len = min_base_len(mode_key)

    if len(base) >= min_len:
        loading_text = f"Klik tombol di bawah untuk mulai scan @{base} ({mode_label})..."
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Mulai Scan", callback_data=f"runlive_{mode_key}_{base}")
        ]])
        results = [
            InlineQueryResultArticle(
                id=f"s{int(time.time())}",
                title=f"Scan @{base} ({mode_label})",
                description=f"Scan username @{base} (username checker)",
                input_message_content=InputTextMessageContent(loading_text),
                reply_markup=keyboard
            )
        ]
    else:
        # Terlalu pendek untuk cek available, list varian di bawah tetap bisa
        results = [
            InlineQueryResultArticle(
                id=f"m{int(time.time())}",
                title=f"Scan tidak tersedia (min {min_len} huruf)",
                description=f"@{base} cuma {len(base)} huruf, scan {mode_label} butuh minimal {min_len}. List varian tetap bisa.",
                input_message_content=InputTextMessageContent(
                    f"Scan {mode_label} butuh username minimal {min_len} huruf. "
                    f"@{base} cuma {len(base)} huruf."
                )
            )
        ]

    # Daftar semua varian (tanpa cek available, tanpa minimal huruf)
    list_text, list_markup, n_items = render_variant_page(mode_key, base, 0)
    if n_items:
        results.append(
            InlineQueryResultArticle(
                id=f"l{int(time.time())}",
                title=f"Varian {base} ({mode_label})",
                description=f"Tampilkan list {n_items} varian username (bukan username checker)",
                input_message_content=InputTextMessageContent(list_text),
                reply_markup=list_markup
            )
        )

    await update.inline_query.answer(results, cache_time=1)

# ================== CALLBACK QUERY HANDLER ==================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data
    inline_msg_id = query.inline_message_id

    if user.id in BANNED_USERS:
        await query.answer("❌ Anda telah dibanned dari bot ini.", show_alert=True)
        return

    # Admin membatalkan pantauan lewat tombol di /daftar
    if data.startswith("stopwatch_"):
        if user.id != ADMIN_ID:
            await query.answer("Khusus admin", show_alert=True)
            return
        _, mode_key, base = data.split("_", 2)
        ok = cancel_watch(mode_key, base)
        await query.answer("Dibatalkan" if ok else "Sudah tidak aktif")
        text, markup = render_watch_list()
        try:
            await query.message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
        return

    # Daftar varian: ganti halaman / ganti set huruf
    if data.startswith("vl_"):
        try:
            _, mode_key, rest = data.split("_", 2)
            base, page_s = rest.rsplit("_", 1)
            page = int(page_s)
        except ValueError:
            await query.answer()
            return
        if not is_valid_mode(mode_key) or not inline_msg_id:
            await query.answer()
            return
        text, markup, _n = render_variant_page(mode_key, base, page)
        try:
            await context.bot.edit_message_text(
                inline_message_id=inline_msg_id,
                text=text,
                reply_markup=markup
            )
        except Exception:
            pass
        await query.answer()
        return

    # Callback untuk Admin Menekan Tombol "Balas Pesan"
    if data.startswith("reply_"):
        target_uid = data.split("_")[1]
        context.user_data["reply_to"] = target_uid
        await query.answer()
        await query.message.reply_text(f"📝 Silakan ketik pesan balasan kamu untuk ID `{target_uid}`:")
        return

    # 1. Trigger Mulai Scan di Channel / Grup
    if data.startswith("runlive_"):
        _, mode_key, base = data.split("_", 2)
        await query.answer("Memulai scan...")

        # NOTIFIKASI BARU Dikirim ke Admin HANYA ketika tombol "Mulai Scan" ditekan
        await notify_admin(context, user, "Eksekusi Scan Username", f"Mode: {mode_key} | Query: @{base}")

        available_clients = [c for c in clients if client_cooldown[c] <= time.time()]
        if not available_clients:
            await context.bot.edit_message_text(
                inline_message_id=inline_msg_id,
                text="❌ acc gua limit"
            )
            return

        _, lbl = resolve_mode(mode_key) or (gen_tamhur, "tamhur")
        # Scan cek available: ada minimal huruf (base 4, ganhur/switch 5)
        candidates = build_candidates(mode_key, base)[:100]
        if not candidates:
            await context.bot.edit_message_text(
                inline_message_id=inline_msg_id,
                text=f"❌ @{base} terlalu pendek untuk scan {lbl}, minimal {min_base_len(mode_key)} huruf"
            )
            return

        found_avail = []
        last_update_time = time.time()

        sem = asyncio.Semaphore(max(1, len(available_clients)))

        async def worker(u):
            nonlocal last_update_time
            async with sem:
                for _ in range(2):
                    c = get_available_client()
                    if not c:
                        await asyncio.sleep(0.2)
                        continue
                    try:
                        await asyncio.sleep(random.uniform(0.2, 0.4))

                        ok = await asyncio.wait_for(
                            c(functions.account.CheckUsernameRequest(u)),
                            timeout=4.0
                        )

                        if ok:
                            res_str = f"🟢 @{u}"
                            found_avail.append(res_str)

                            now = time.time()
                            if now - last_update_time > 3.0:
                                last_update_time = now
                                live_text = (
                                    f"scanning @{base} ({lbl})...\n"
                                    f"ditemukan: {len(found_avail)}\n\n" +
                                    "\n".join(found_avail[:15]) +
                                    ("\n..." if len(found_avail) > 15 else "")
                                )
                                try:
                                    await context.bot.edit_message_text(
                                        inline_message_id=inline_msg_id,
                                        text=live_text
                                    )
                                except Exception:
                                    pass
                            return res_str
                        return None
                    except FloodWaitError as e:
                        logger.warning(f"⚠️ Account terkena FloodWait {e.seconds}s.")
                        client_cooldown[c] = time.time() + e.seconds + 5
                        continue
                    except asyncio.TimeoutError:
                        client_cooldown[c] = time.time() + 10
                        continue
                    except Exception:
                        return None
                return None

        await asyncio.gather(*(worker(u) for u in candidates))

        if not found_avail:
            await context.bot.edit_message_text(
                inline_message_id=inline_msg_id,
                text=f"❌ @{base} ({lbl}) ga ada atau akun gua limit jadi gak nemu"
            )
            return

        pages = chunk_results(found_avail, chunk_size=15)

        SCAN_CACHE[inline_msg_id] = {
            "pages": pages,
            "mode_label": lbl,
            "base": base,
            "mode_key": mode_key
        }

        page_text = (
            f"hasil scan untuk @{base} ({lbl})"
            f" ada {len(found_avail)} usn\n\n" +
            "\n".join(pages[0])
        )

        reply_markup = build_pagination_keyboard(0, len(pages), base, mode_key)

        try:
            await context.bot.edit_message_text(
                inline_message_id=inline_msg_id,
                text=page_text,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Gagal update hasil akhir: {e}")

    # 2. Handler Pindah Halaman
    elif data.startswith("page_"):
        _, mode_key, base, page_idx = data.split("_", 3)
        page_idx = int(page_idx)

        if inline_msg_id not in SCAN_CACHE:
            await query.answer("⚠️ Session scan ini sudah kadaluarsa. Silakan scan ulang.", show_alert=True)
            return

        cache_data = SCAN_CACHE[inline_msg_id]
        pages = cache_data["pages"]
        lbl = cache_data["mode_label"]

        if page_idx >= len(pages):
            await query.answer()
            return

        page_text = (
            f" hasil scan @{base} ({lbl}) ada  {sum(len(p) for p in pages)} usn\n"
            f"{page_idx + 1}/{len(pages)}\n\n" +
            "\n".join(pages[page_idx])
        )

        reply_markup = build_pagination_keyboard(page_idx, len(pages), base, mode_key)

        try:
            await context.bot.edit_message_text(
                inline_message_id=inline_msg_id,
                text=page_text,
                reply_markup=reply_markup
            )
            await query.answer(f"{page_idx + 1}")
        except Exception:
            await query.answer()

# ================== COMMAND HANDLERS & ADMIN BAN/UNBAN ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in BANNED_USERS:
        return
    save_user(user.id)
    await notify_admin(context, user, "Menjalankan /start")
    await update.message.reply_text("P")

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Format salah. Gunakan: `/ban <user_id>`", parse_mode="Markdown")
        return
    try:
        target_id = int(context.args[0])
        save_ban(target_id)
        await update.message.reply_text(f"✅ User `{target_id}` berhasil di-ban.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ User ID harus berupa angka.")

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Format salah. Gunakan: `/unban <user_id>`", parse_mode="Markdown")
        return
    try:
        target_id = int(context.args[0])
        remove_ban(target_id)
        await update.message.reply_text(f"✅ User `{target_id}` berhasil di-unban.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ User ID harus berupa angka.")

# ================== PRIVATE CHAT MESSAGE HANDLER & REPLY SYSTEM ==================
async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg_text = update.message.text

    if user.id in BANNED_USERS:
        return

    # Jika Admin membalas pesan pengguna
    if user.id == ADMIN_ID:
        # A. Admin mengetik balasan setelah menekan tombol [Balas Pesan]
        if "reply_to" in context.user_data:
            target_id = int(context.user_data.pop("reply_to"))
            try:
                await context.bot.send_message(chat_id=target_id, text=f"{msg_text}", parse_mode="Markdown")
                await update.message.reply_text(f"✅ Balasan berhasil dikirim ke `{target_id}`", parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"❌ Gagal mengirim pesan ke user: {e}")
            return

        # B. Admin menggunakan fitur bawaan Telegram Reply pada pesan notifikasi
        if update.message.reply_to_message:
            rep_text = update.message.reply_to_message.text or ""
            if "ID:" in rep_text:
                try:
                    # Ambil User ID dari teks notifikasi
                    target_id = int(rep_text.split("ID:")[1].split()[0].replace("`", ""))
                    await context.bot.send_message(chat_id=target_id, text=f"💬 **Pesan dari Admin:**\n{msg_text}", parse_mode="Markdown")
                    await update.message.reply_text(f"✅ Balasan berhasil dikirim ke `{target_id}`", parse_mode="Markdown")
                    return
                except Exception as e:
                    await update.message.reply_text(f"❌ Gagal memproses balasan: {e}")
                    return

    # Pengguna Biasa Mengirim Pesan PC ke Bot -> Kirim Notifikasi ke Admin
    save_user(user.id)
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    full_name = f"{first_name} {last_name}".strip()
    username = f"@{user.username}" if user.username else "Tidak ada username"

    admin_msg = (
        f"📩 **Pesan Baru Masuk di PC**\n"
        f"• Nama: {full_name}\n"
        f"• Username: {username}\n"
        f"• ID: `{user.id}`\n\n"
        f"💬 **Pesan:**\n{msg_text}"
    )

    reply_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Balas Pesan", callback_data=f"reply_{user.id}")
    ]])

    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_msg, parse_mode="Markdown", reply_markup=reply_kb)
        await update.message.reply_text("...")
    except Exception as e:
        logger.error(f"Gagal: {e}")

# ================== PANTAU REPLY DI GRUP (HASIL INLINE BOT) ==================
async def handle_group_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.reply_to_message:
        return

    replied = msg.reply_to_message
    # hanya tangkap reply ke pesan yang dikirim via bot ini
    if not replied.via_bot or replied.via_bot.id != context.bot.id:
        return

    user = msg.from_user
    if user.id in BANNED_USERS:
        return

    chat = msg.chat
    chat_name = chat.title or chat.username or str(chat.id)
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    username = f"@{user.username}" if user.username else "tanpa username"

    # teks asli pesan inline yang dibalas
    original_text = replied.text or replied.caption or "(bukan teks)"
    reply_text = msg.text or msg.caption or "(bukan teks)"

    notif = (
        f"\U0001f4ac **Reply ke pesan bot (GC)**\n"
        f"\u2022 Grup: {chat_name}\n"
        f"\u2022 Dari: {full_name} ({username})\n"
        f"\u2022 ID: `{user.id}`\n\n"
        f"\U0001f4c4 **Pesan asli:**\n{original_text[:200]}\n\n"
        f"\u21a9\ufe0f **Reply-nya:**\n{reply_text[:500]}"
    )

    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=notif, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Gagal kirim notif reply GC: {e}")

# ================== [TAMBAHAN] AUTO BAN SAAT LEFT CHANNEL ==================
async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if not cmu:
        return

    chat = cmu.chat
    if chat.type != "channel" or chat.id not in AUTOBAN_CHANNELS:
        return

    target = cmu.new_chat_member.user
    if target.is_bot or target.id == ADMIN_ID:
        return

    old = cmu.old_chat_member
    new = cmu.new_chat_member

    # sebelumnya member (atau restricted tapi masih member), sekarang left
    was_member = old.status == ChatMember.MEMBER or (
        old.status == ChatMember.RESTRICTED and getattr(old, "is_member", False)
    )
    left_now = new.status == ChatMember.LEFT
    voluntary = cmu.from_user.id == target.id  # left sendiri, bukan di-kick admin

    if not (was_member and left_now and voluntary):
        return

    if target.id in BANNED_USERS:
        return

    # ban di bot, tanpa notifikasi ke admin
    save_ban(target.id)
    logger.info(f"🚫 Auto-ban {target.id} (left {chat.title or chat.id})")

# ================== POST INIT & MAIN ==================
async def post_init(application):
    logger.info("⚙️ Inisialisasi Telethon sessions (untuk scan & /cek)...")
    await init_clients()
    logger.info(f"📊 Total akun aktif: {len(clients)} akun.")

    # Lanjutkan pantauan (web) yang tersimpan sebelum bot restart.
    # Tidak butuh akun Telethon.
    for (mode_key, base) in load_watchlist():
        if is_valid_mode(mode_key):
            start_watch(application, mode_key, base)
            logger.info(f"♻️ Lanjut memantau {mode_key} {base}")

def main():
    load_bans()
    load_users()

    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN tidak ditemukan di Environment Variable!")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ban", ban_user))
    app.add_handler(CommandHandler("unban", unban_user))
    app.add_handler(CommandHandler("pantau", pantau_cmd))
    app.add_handler(CommandHandler("batal", batal_cmd))
    app.add_handler(CommandHandler("daftar", daftar_cmd))
    app.add_handler(CommandHandler("cek", cek_cmd))
    app.add_handler(CommandHandler("limit", limit_cmd))

    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Handler pesan PM/PC (Private Chat)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_message))

    # Tangkap reply ke pesan inline di grup
    app.add_handler(MessageHandler(
        (filters.ChatType.GROUPS) & filters.TEXT & filters.REPLY & ~filters.COMMAND,
        handle_group_reply
    ))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.REPLY & ~filters.COMMAND, handle_group_reply))

    # [TAMBAHAN] Auto-ban user yang left channel
    app.add_handler(ChatMemberHandler(handle_chat_member, ChatMemberHandler.CHAT_MEMBER))

    logger.info("🚀 Bot berjalan...")
    # [DIUBAH] ditambah allowed_updates supaya update chat_member diterima
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "inline_query", "callback_query", "chat_member"],
    )

if __name__ == "__main__":
    main()
