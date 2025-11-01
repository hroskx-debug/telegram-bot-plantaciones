"""
Telegram bot para recordar fechas de plantación y cosecha.

Integrado con base de datos MySQL (JohnnyGreen/HostGator).

Características:
- /start y /ayuda: muestran instrucciones.
- /listar: muestra posturas con fechas de siembra y cosecha.
- /zonahoraria <IANA>: cambia la zona horaria del usuario (por defecto America/Mexico_City).
- Recordatorios automáticos a las 08:00 (hora local del usuario) 14, 7, 1 y 0 días antes.

Dependencias:
  pip install python-telegram-bot==21.6 APScheduler==3.10.4 pytz==2024.1 python-dateutil==2.9.0.post0 PyMySQL==1.1.1

Variables de entorno:
  BOT_TOKEN, MYSQL_HOST, MYSQL_PORT, MYSQL_DB, MYSQL_USER, MYSQL_PASSWORD, DEFAULT_TZ
"""

import os
from datetime import datetime, timedelta, time, date
from dateutil import tz
import pymysql
from pymysql.cursors import DictCursor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_DB = os.getenv("MYSQL_DB", "johnnygreen")
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
DEFAULT_TZ = os.getenv("DEFAULT_TZ", "America/Mexico_City")
REMIND_DAYS = [14, 7, 1, 0]

# ===================== DB CONNECTION =====================
def db():
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        cursorclass=DictCursor,
        autocommit=True,
        charset="utf8mb4",
    )

def init_db():
    con = db()
    with con.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id BIGINT PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                tz VARCHAR(64) NOT NULL DEFAULT 'America/Mexico_City'
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_reminders_enviados (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                registro_id BIGINT NOT NULL,
                tipo ENUM('siembra','cosecha') NOT NULL,
                dias_antes INT NOT NULL,
                fecha_objetivo DATE NOT NULL,
                enviado_en DATETIME NOT NULL,
                KEY (registro_id, tipo, dias_antes, fecha_objetivo)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
    con.close()

# ===================== UTILIDADES =====================
def get_user_tz(user_id: int, chat_id: int) -> str:
    con = db()
    with con.cursor() as cur:
        cur.execute("SELECT tz FROM bot_users WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        if row:
            return row["tz"]
        cur.execute(
            "INSERT INTO bot_users(user_id, chat_id, tz) VALUES(%s,%s,%s)",
            (user_id, chat_id, DEFAULT_TZ),
        )
        return DEFAULT_TZ

def set_user_tz(user_id: int, chat_id: int, tzname: str):
    con = db()
    with con.cursor() as cur:
        cur.execute("""
            INSERT INTO bot_users(user_id, chat_id, tz)
            VALUES(%s,%s,%s)
            ON DUPLICATE KEY UPDATE chat_id=VALUES(chat_id), tz=VALUES(tz)
        """, (user_id, chat_id, tzname))

def already_notified(registro_id: int, tipo: str, dias_antes: int, fecha_obj: date) -> bool:
    con = db()
    with con.cursor() as cur:
        cur.execute("""
            SELECT 1 FROM bot_reminders_enviados
            WHERE registro_id=%s AND tipo=%s AND dias_antes=%s AND fecha_objetivo=%s
        """, (registro_id, tipo, dias_antes, fecha_obj))
        return cur.fetchone() is not None

def mark_notified(registro_id: int, tipo: str, dias_antes: int, fecha_obj: date):
    con = db()
    with con.cursor() as cur:
        cur.execute("""
            INSERT INTO bot_reminders_enviados(registro_id, tipo, dias_antes, fecha_objetivo, enviado_en)
            VALUES (%s,%s,%s,%s,NOW())
        """, (registro_id, tipo, dias_antes, fecha_obj))

# ===================== CONSULTAS =====================
POSTURAS_QUERY = """
SELECT p.id,
       p.nombre AS postura,
       p.fecha_plantacion AS fecha_siembra,
       p.fecha_cosecha,
       c.nombre AS cultivo,
       v.nombre AS variedad
FROM posturas p
LEFT JOIN cultivo c ON c.id = p.id_cultivo
LEFT JOIN variedades v ON v.id = p.id_variedad
WHERE (p.fecha_plantacion IS NOT NULL OR p.fecha_cosecha IS NOT NULL)
"""

def fetch_posturas():
    con = db()
    with con.cursor() as cur:
        cur.execute(POSTURAS_QUERY)
        return cur.fetchall()

# ===================== HANDLERS =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    tzname = get_user_tz(user.id, chat.id)
    text = (
        "👋 Hola, soy tu bot de recordatorios de siembra/cosecha 🌱\n\n"
        "Comandos disponibles:\n"
        "• /listar — Ver próximas siembras/cosechas\n"
        "• /zonahoraria <IANA> — Ejemplo: America/Mexico_City\n\n"
        f"Zona horaria actual: <b>{tzname}</b> (recordatorios 08:00)"
    )
    await update.message.reply_html(text)

async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "<b>Ayuda</b>\n\n"
        "Este bot revisa automáticamente las fechas de tu tabla <code>posturas</code>.\n"
        "Te recordará 14, 7, 1 y 0 días antes de las fechas de siembra o cosecha.\n"
        "Usa /zonahoraria para ajustar tu hora local."
    )

async def cmd_zonahoraria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not context.args:
        await update.message.reply_text("Uso: /zonahoraria <IANA_tz> p. ej. America/Mexico_City")
        return
    tzname = context.args[0]
    if tz.gettz(tzname) is None:
        await update.message.reply_text("❌ Zona horaria inválida.")
        return
    set_user_tz(user.id, chat.id, tzname)
    await update.message.reply_text(f"✅ Zona horaria guardada: {tzname}")

def fmt_row(r):
    parts = [f"ID {r['id']} — <b>{r['postura']}</b>"]
    if r.get("cultivo"): parts.append(f"cultivo: {r['cultivo']}")
    if r.get("variedad"): parts.append(f"variedad: {r['variedad']}")
    if r.get("fecha_siembra"): parts.append(f"siembra: {r['fecha_siembra']}")
    if r.get("fecha_cosecha"): parts.append(f"cosecha: {r['fecha_cosecha']}")
    return " — ".join(parts)

async def cmd_listar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = fetch_posturas()
    if not rows:
        await update.message.reply_text("No encontré posturas con fechas registradas.")
        return
    rows.sort(key=lambda r: min(
        [d for d in [r.get("fecha_siembra"), r.get("fecha_cosecha")] if d] or [date(9999,12,31)]
    ))
    texto = "\n".join(fmt_row(r) for r in rows[:50])
    await update.message.reply_html(texto)

# ===================== RECORDATORIOS =====================
async def enviar_recordatorio(context: ContextTypes.DEFAULT_TYPE, chat_id: int, texto: str):
    try:
        await context.bot.send_message(chat_id=chat_id, text=texto, parse_mode=ParseMode.HTML)
    except Exception as e:
        print("Error enviando recordatorio:", e)

async def tarea_diaria(context: ContextTypes.DEFAULT_TYPE):
    con = db()
    with con.cursor() as cur:
        cur.execute("SELECT user_id, chat_id, tz FROM bot_users")
        usuarios = cur.fetchall()

    posturas = fetch_posturas()
    for u in usuarios:
        tzinfo = tz.gettz(u["tz"]) or tz.gettz(DEFAULT_TZ)
        hoy_local = datetime.now(tzinfo).date()
        for r in posturas:
            for tipo, campo in [("siembra", "fecha_siembra"), ("cosecha", "fecha_cosecha")]:
                if not r.get(campo): 
                    continue
                try:
                    f = datetime.fromisoformat(str(r[campo])).date()
                except Exception:
                    continue
                delta = (f - hoy_local).days
                if delta in REMIND_DAYS and not already_notified(r["id"], tipo, delta, f):
                    emoji = "📅" if tipo == "siembra" else "🌾"
                    texto = (
                        f"{emoji} <b>{tipo.capitalize()}</b> de <b>{r['postura']}</b>"
                        + (f" (cultivo: {r['cultivo']})" if r.get('cultivo') else "")
                        + (f" — {f.isoformat()} — faltan {delta} días." if delta>0 else " — ¡Es hoy! 🌱")
                    )
                    await enviar_recordatorio(context, u["chat_id"], texto)
                    mark_notified(r["id"], tipo, delta, f)

async def programar_job_diario(app: Application):
    scheduler = AsyncIOScheduler(timezone=tz.gettz(DEFAULT_TZ))
    scheduler.add_job(tarea_diaria, CronTrigger(hour=8, minute=0), args=[app.bot])
    scheduler.start()
    print("⏰ Job diario 08:00 programado (hora base MX).")

# ===================== MAIN =====================
def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Debes definir BOT_TOKEN en variables de entorno.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["ayuda", "help"], cmd_ayuda))
    app.add_handler(CommandHandler("zonahoraria", cmd_zonahoraria))
    app.add_handler(CommandHandler("listar", cmd_listar))
    app.post_init(programar_job_diario)
    return app

if __name__ == "__main__":
    application = build_app()
    print("🤖 Bot (MySQL) corriendo en modo polling…")
    application.run_polling(allowed_updates=["message", "edited_message"])
