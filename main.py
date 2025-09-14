import os, re, io, time, base64, unicodedata
import datetime as dt
import asyncio
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, CallbackContext, filters
)
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError, RetryAfter

# ================== CONFIG ==================
load_dotenv()
BOT_TOKEN    = os.getenv("BOT_TOKEN")
SHEETDB_URL  = os.getenv("SHEETDB_URL")
GAS_URL      = os.getenv("GAS_URL")      # Apps Script Web App (/exec)
GAS_KEY      = os.getenv("GAS_KEY")      # KEY yg sama dgn di Apps Script
ALBUM_WAIT   = 2.0                       # detik debounce album
HTTP_TIMEOUT = 240                       # timeout untuk GET/POST besar
SLEEP_BETWEEN = 0.4                      # jeda kecil antar upload
# ============================================

# ---------- HTTP session global (retry/backoff) ----------
session = requests.Session()
retries = Retry(
    total=5,
    backoff_factor=0.8,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=frozenset(["GET", "POST"])
)
session.mount("https://", HTTPAdapter(max_retries=retries))
session.mount("http://",  HTTPAdapter(max_retries=retries))

# ---------- Regex parser ----------
PATTERNS = {
    "tanggal":   r"(?i)Tanggal\s*[:\-]\s*([^\n]+)",
    "jenis":     r"(?i)Jenis\s*Pekerjaan\s*[:\-]\s*([^\n]+)",
    "area":      r"(?i)Area\s*[:\-]\s*([^\n]+)",
    "teknisi":   r"(?i)Nama\s*Teknisi\s*[:\-]\s*((?:.|\n)+?)(?=\n[A-Z ]|$|Data Pelanggan)",
    "cust_name": r"(?i)Cust\s*Name\s*[:\-]\s*([^\n]+)",
    "cid":       r"(?i)\bCID\s*[:\-]\s*([^\n]+)",
    "odp":       r"(?i)\bODP\s*[:\-]\s*([^\n]+)",
    "sn_ont":    r"(?i)SN\s*ONT\s*[:\-]\s*([^\n]+)",
    "mac_ont":   r"(?i)MAC\s*ONT\s*[:\-]\s*([^\n]+)",
    "tx":        r"(?i)\bTX\s*[:\-]\s*([^\n]+)",
    "rx":        r"(?i)\bRX\s*[:\-]\s*([^\n]+)",
    "download":  r"(?i)Download\s*[:\-]\s*([^\n]+)",
    "upload":    r"(?i)Upload\s*[:\-]\s*([^\n]+)",
    "note":      r"(?i)Note\s*[:\-]\s*([^\n]+)",
}

def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", (name or "")).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^A-Za-z0-9 _\-\.\(\)]", "", name).strip()
    return name or "Photo"

def parse_message(text: str):
    data = {}
    for k, pat in PATTERNS.items():
        m = re.search(pat, text or "")
        data[k] = m.group(1).strip() if m else ""
    if data.get("teknisi"):
        lines = []
        for line in data["teknisi"].splitlines():
            line = re.sub(r"^\s*\d+[\.\)]\s*", "", line.strip())
            if line:
                lines.append(line)
        data["teknisi"] = ", ".join(lines)
    return data

def save_to_sheetdb(data: dict, from_user: str, photo_urls: list[str]):
    row = {
        "Timestamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Tanggal": data["tanggal"],
        "Jenis Pekerjaan": data["jenis"],
        "Area": data["area"],
        "Teknisi": data["teknisi"],
        "Cust Name": data["cust_name"],
        "CID": data["cid"],
        "ODP": data["odp"],
        "SN ONT": data["sn_ont"],
        "MAC ONT": data["mac_ont"],
        "TX": data["tx"],
        "RX": data["rx"],
        "Download": data["download"],
        "Upload": data["upload"],
        "Note": data["note"],
        "FromUser": from_user,
        "Photo": "\n".join(photo_urls) if photo_urls else "",
    }
    r = session.post(SHEETDB_URL, json={"data": [row]}, timeout=HTTP_TIMEOUT)
    return r.status_code, r.text

# ---------- Apps Script uploader (tanpa kompres) ----------
def upload_to_gas_retry(raw: bytes, filename: str, mime: str = "image/jpeg", cust: str | None = None) -> str:
    if not GAS_URL or not GAS_KEY:
        raise RuntimeError("GAS_URL / GAS_KEY belum diset di Secrets Replit.")
    payload = {
        "key": GAS_KEY,
        "name": filename,
        "mime": mime,
        "file": base64.b64encode(raw).decode("ascii"),
    }
    if cust:
        payload["cust"] = cust
    last_err = None
    for attempt in range(1, 5):
        try:
            r = session.post(GAS_URL, data=payload, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            js = r.json()
            if not js.get("ok"):
                raise RuntimeError(f"GAS error: {js}")
            return js["webViewLink"]
        except Exception as e:
            last_err = e
            time.sleep(1.2 * attempt)
    raise RuntimeError(f"Upload gagal setelah retry: {last_err}")

# ---------- Telegram file helpers ----------
async def get_tg_bytes(bot, file_id: str) -> bytes:
    last = None
    for attempt in range(1, 4):
        try:
            tgfile = await bot.get_file(file_id)
            resp = session.get(tgfile.file_path, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            last = e
            await asyncio.sleep(0.8 * attempt)
    raise last

# ---------- Album manager ----------
class AlbumState:
    __slots__ = ("file_ids", "captions", "chat_id", "user", "finalized")
    def __init__(self, chat_id: int, user: str):
        self.file_ids: list[str] = []
        self.captions: list[str] = []
        self.chat_id = chat_id
        self.user = user
        self.finalized = False

album_cache: dict[str, AlbumState] = {}

# ---------- Helper kirim pesan dengan retry ----------
async def safe_send(context: CallbackContext, chat_id: int, text: str, tries: int = 4):
    delay = 1.5
    for _ in range(tries):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
            return
        except RetryAfter as e:
            await asyncio.sleep(max(1.0, float(getattr(e, "retry_after", delay))))
        except (TimedOut, NetworkError):
            await asyncio.sleep(delay); delay *= 1.7
        except Exception as e:
            print("[safe_send] fatal:", e)
            break

# ---------- Job: finalize album ----------
async def finalize_album_job(context: CallbackContext):
    mgid = context.job.name
    state = album_cache.get(mgid)
    if not state or state.finalized:
        return
    state.finalized = True

    caption = next((c for c in state.captions if c), "")
    data = parse_message(caption)
    cust = sanitize_filename(data.get("cust_name") or "Photo")

    links = []
    for i, file_id in enumerate(state.file_ids, start=1):
        try:
            raw = await get_tg_bytes(context.bot, file_id)
            fname = f"{cust}_{i:02d}.jpg"
            link = upload_to_gas_retry(raw, fname, mime="image/jpeg", cust=cust)
            links.append(link)
            await asyncio.sleep(SLEEP_BETWEEN)
        except Exception as e:
            links.append(f"(upload gagal: {e})")

    code, resp_text = save_to_sheetdb(data, state.user, links)
    msg = (f"✅ Data tersimpan ke Spreadsheet & Drive.\n"
           f"Cust: {data['cust_name']} | CID: {data['cid']}\n"
           f"ODP: {data['odp']} | RX: {data['rx']}\n"
           f"Foto: {len(links)} file")
    if code not in (200, 201):
        msg = f"⚠️ Gagal simpan ke Sheet:\n{resp_text}"

    await safe_send(context, state.chat_id, msg)
    album_cache.pop(mgid, None)

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Kirim *ALBUM* (beberapa foto sekali kirim) + 1 caption.\n"
        "Nama file mengikuti `Cust Name` di caption.\n"
        "Butuh gambar *asli/HD*? kirim sebagai *Dokumen* (bukan Photo).",
        parse_mode="Markdown"
    )

def get_text(update: Update) -> str:
    return update.message.caption if update.message and update.message.caption else (update.message.text or "")

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = get_text(update)
    user = f"{update.effective_user.full_name} (@{update.effective_user.username or ''})"
    chat_id = update.effective_chat.id

    # ====== DOKUMEN (gambar asli/HD) ======
    if update.message.document:
        doc = update.message.document
        mime = (doc.mime_type or "").lower()
        if mime.startswith("image/"):
            data = parse_message(text)
            cust = sanitize_filename(data.get("cust_name") or "Photo")

            if doc.file_size and doc.file_size > 45 * 1024 * 1024:
                await update.message.reply_text("⚠️ File >45MB. Bagi jadi beberapa dokumen (batas Apps Script ~50MB).")
                return

            try:
                raw = await get_tg_bytes(context.bot, doc.file_id)
                ext = os.path.splitext(doc.file_name or "")[1] or ".jpg"
                fname = f"{cust}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
                link = upload_to_gas_retry(raw, fname, mime=mime, cust=cust)
                code, resp_text = save_to_sheetdb(data, user, [link])
                if code in (200, 201):
                    await update.message.reply_text(f"✅ Dokumen asli tersimpan ke Drive & Spreadsheet.\nCust: {data['cust_name']}")
                else:
                    await update.message.reply_text(f"⚠️ Gagal simpan ke Sheet:\n{resp_text}")
            except Exception as e:
                await update.message.reply_text(f"⚠️ Gagal upload dokumen: {e}")
            return

    # ====== FOTO ======
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        mgid = update.message.media_group_id

        if mgid:
            st = album_cache.get(mgid)
            if not st:
                st = AlbumState(chat_id=chat_id, user=user)
                album_cache[mgid] = st
            st.file_ids.append(file_id)
            st.captions.append(text)

            jq = context.job_queue
            if jq:
                for job in jq.get_jobs_by_name(mgid):
                    job.schedule_removal()
                jq.run_once(finalize_album_job, when=ALBUM_WAIT, name=mgid)
            else:
                await update.message.reply_text("⚠️ JobQueue belum aktif. Instal: python-telegram-bot[job-queue]==20.8")
            return

        # ---- single photo ----
        data = parse_message(text)
        cust = sanitize_filename(data.get("cust_name") or "Photo")
        try:
            raw = await get_tg_bytes(context.bot, file_id)
            fname = f"{cust}_01.jpg"
            link = upload_to_gas_retry(raw, fname, mime="image/jpeg", cust=cust)
            code, resp_text = save_to_sheetdb(data, user, [link])
            if code in (200, 201):
                await update.message.reply_text(
                    f"✅ Data tersimpan ke Spreadsheet & Drive.\n"
                    f"Cust: {data['cust_name']} | CID: {data['cid']}\n"
                    f"ODP: {data['odp']} | RX: {data['rx']}\n"
                    f"Foto: 1 file"
                )
            else:
                await update.message.reply_text(f"⚠️ Gagal simpan ke Sheet:\n{resp_text}")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Gagal upload: {e}")
        return

    # ====== TEKS SAJA ======
    data = parse_message(text)
    code, resp_text = save_to_sheetdb(data, user, [])
    if code in (200, 201):
        await update.message.reply_text(
            f"✅ Data tersimpan ke Spreadsheet.\nCust: {data['cust_name']} | CID: {data['cid']}\nFoto: 0"
        )
    else:
        await update.message.reply_text(f"⚠️ Gagal simpan:\n{resp_text}")

# ---------- Heartbeat (self-ping) ----------
async def heartbeat(context: CallbackContext):
    if not PUBLIC_URL:
        return
    try:
        await asyncio.to_thread(lambda: session.get(PUBLIC_URL, timeout=10))
    except Exception:
        pass

def run_bot():
    req = HTTPXRequest(  # timeout longgar untuk API Telegram
        connect_timeout=30.0, read_timeout=90.0, write_timeout=90.0, pool_timeout=30.0
    )
    app = ApplicationBuilder().token(BOT_TOKEN).request(req).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.ALL, handle))

    if app.job_queue:
        app.job_queue.run_repeating(heartbeat, interval=120, first=5)
    else:
        print("[warn] JobQueue not available → heartbeat dimatikan")

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
        print("[error]", repr(context.error))
    app.add_error_handler(on_error)

    print("[bot] starting polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    run_bot()
