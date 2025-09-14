import os, re, datetime as dt, requests, asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# Load secret dari environment
load_dotenv()
BOT_TOKEN     = os.getenv("BOT_TOKEN")
SHEETDB_URL   = os.getenv("SHEETDB_URL")
DRIVE_WEBHOOK = os.getenv("DRIVE_WEBHOOK")  # URL Apps Script Web App

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

def parse_message(text: str):
    """Ambil field dari caption/teks"""
    data = {}
    for k, pat in PATTERNS.items():
        m = re.search(pat, text or "")
        data[k] = m.group(1).strip() if m else ""
    if data.get("teknisi"):
        lines = []
        for line in data["teknisi"].splitlines():
            line = re.sub(r"^\s*\d+[\.\)]\s*", "", line.strip())
            if line: lines.append(line)
        data["teknisi"] = ", ".join(lines)
    return data

# ---------- Upload foto ke Google Drive (Apps Script) ----------
def upload_to_drive(cust_name: str, photo_urls: list[str]) -> tuple[list[str], str]:
    if not photo_urls or not DRIVE_WEBHOOK:
        return photo_urls, ""  # kalau tidak ada webhook, simpan URL asli Telegram
    try:
        payload = {"cust_name": cust_name or "UNKNOWN", "file_urls": photo_urls}
        r = requests.post(DRIVE_WEBHOOK, json=payload, timeout=60)
        r.raise_for_status()
        j = r.json()
        if j.get("ok"):
            links = [f["url"] for f in j.get("files", []) if f.get("ok")]
            return links, j.get("folder_url", "")
    except Exception as e:
        print("Drive upload error:", e)
    return [], ""

# ---------- Simpan ke SheetDB ----------
def save_to_sheetdb(data: dict, from_user: str, photo_urls: list[str]):
    drive_links, folder_url = upload_to_drive(data.get("cust_name",""), photo_urls)
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
        "Photo": "\n".join(drive_links if DRIVE_WEBHOOK else photo_urls),
        "Drive Folder": folder_url if DRIVE_WEBHOOK else "",
    }
    r = requests.post(SHEETDB_URL, json={"data": [row]}, timeout=25)
    return r.status_code, r.text

# ---------- Album manager ----------
ALBUM_WAIT = 2.0
class AlbumState:
    __slots__ = ("photos","captions","chat_id","user","timer_task","finalized")
    def __init__(self, chat_id: int, user: str):
        self.photos, self.captions = [], []
        self.chat_id, self.user = chat_id, user
        self.timer_task, self.finalized = None, False

album_cache = {}

async def schedule_finalize(mgid: str, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(ALBUM_WAIT)
    state = album_cache.get(mgid)
    if not state or state.finalized: return
    state.finalized = True
    caption = next((c for c in state.captions if c), "")
    data = parse_message(caption)
    code, resp = save_to_sheetdb(data, state.user, state.photos)
    msg = f"✅ Disimpan.\nCust: {data['cust_name']} | CID: {data['cid']} | Foto: {len(state.photos)}"
    if code not in (200,201): msg = f"⚠️ Gagal simpan:\n{resp}"
    await context.bot.send_message(chat_id=state.chat_id, text=msg)
    album_cache.pop(mgid, None)

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Kirim album foto + caption untuk disimpan ke Sheet & Drive.")

def get_text(update: Update): 
    return update.message.caption if update.message.caption else (update.message.text or "")

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = get_text(update)
    user = f"{update.effective_user.full_name} (@{update.effective_user.username or ''})"
    chat_id = update.effective_chat.id

    if update.message.photo:
        f = await context.bot.get_file(update.message.photo[-1].file_id)
        url = f.file_path
        mgid = update.message.media_group_id
        if mgid:
            st = album_cache.get(mgid) or AlbumState(chat_id, user)
            album_cache[mgid] = st
            st.photos.append(url)
            st.captions.append(text)
            if st.timer_task and not st.timer_task.done(): st.timer_task.cancel()
            st.timer_task = context.application.create_task(schedule_finalize(mgid, context))
            return
        data = parse_message(text)
        save_to_sheetdb(data, user, [url])
        await update.message.reply_text("✅ Foto tunggal disimpan.")
        return

    data = parse_message(text)
    save_to_sheetdb(data, user, [])
    await update.message.reply_text("✅ Data teks disimpan.")

# ---------- Main ----------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle))
    app.run_polling()

if __name__ == "__main__":
    main()
