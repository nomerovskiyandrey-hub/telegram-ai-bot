import os
import asyncio
import sqlite3
import json
import unicodedata
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CallbackQueryHandler

KYIV_TZ = ZoneInfo("Europe/Kyiv")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DOCTOR_ID = 262491197
DB_PATH = os.environ.get("DB_PATH", "bot.db")

flood_control = {}
urgent_cooldown = {}
last_report_key = None

anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# ─────────────────────────────────────────────
# 🗄️ БАЗА ДАННЫХ
# ─────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS patients (
        chat_id INTEGER PRIMARY KEY,
        name TEXT DEFAULT '',
        history TEXT DEFAULT '[]',
        long_memory TEXT DEFAULT ''
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS daily_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS blacklist (
        chat_id INTEGER PRIMARY KEY
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS doctor_memory (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        memory TEXT DEFAULT ''
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS relay (
        doctor_msg_id INTEGER PRIMARY KEY,
        chat_id INTEGER,
        business_id TEXT
    )""")
    # Міграція: додати long_memory якщо не існує
    try:
        c.execute("ALTER TABLE patients ADD COLUMN long_memory TEXT DEFAULT ''")
    except Exception:
        pass
    conn.commit()
    conn.close()

def get_patient(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, history, long_memory FROM patients WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "name": row[0],
            "history": json.loads(row[1]),
            "long_memory": row[2] or ""
        }
    return {"name": "", "history": [], "long_memory": ""}

def save_patient(chat_id, data):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO patients (chat_id, name, history, long_memory) VALUES (?,?,?,?)",
              (chat_id, data["name"], json.dumps(data["history"]), data.get("long_memory", "")))
    conn.commit()
    conn.close()

def get_doctor_memory():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT memory FROM doctor_memory WHERE id=1")
    row = c.fetchone()
    conn.close()
    return row[0] if row else ""

def save_doctor_memory(memory: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO doctor_memory (id, memory) VALUES (1, ?)", (memory,))
    conn.commit()
    conn.close()

def add_request(text):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO daily_requests (text, created_at) VALUES (?,?)",
              (text, datetime.now(KYIV_TZ).strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()

def get_requests():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT text FROM daily_requests ORDER BY id")
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows

def clear_requests():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM daily_requests")
    conn.commit()
    conn.close()

def is_blacklisted(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM blacklist WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

def add_blacklist(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO blacklist (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────
# 🤖 ПРОМПТИ
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """Ти — розумний персональний асистент лікаря ортопеда-травматолога Андрія Ігоровича Номеровського.

## ГОЛОВНЕ ПРАВИЛО
Спочатку мовчки класифікуй хто пише (не показуй класифікацію) і відповідай відповідно:

### 🏥 ПАЦІЄНТ (медичне питання, запис, симптоми, операція):
- Представляйся: "Асистент лікаря Андрія Ігоровича Номеровського"
- Медичний тон, тепло, по суті
- Запис пропонуй м'яко і доречно: одним коротким реченням в кінці, коли є скарга, знімки чи питання про операцію. Не повторюй, якщо вже пропонував у цій розмові, якщо пацієнт уже записаний або просто дякує
- Ординаторська — це тільки орієнтир адреси. НІКОЛИ не кажи пацієнту заходити в ординаторську чи чекати там — лікаря може не бути на місці. Кажи, що Андрій Ігорович вийде і зустріне
- Якщо пацієнт пише, що вже прийшов / на місці / чекає — відповідай тільки: "Андрій Ігорович підійде і зустріне вас за пару хвилин." Не відправляй його нікуди і не давай адрес

### 👥 ДРУГ/ЗНАЙОМИЙ (неформальне спілкування, особисті теми):
- НЕ представляйся як асистент
- Відповідай невимушено від імені Андрія
- "Привіт! Зараз зайнятий, передам що писав"

### 👔 КОЛЕГА (медична термінологія, робочі питання):
- Коротко, професійно
- "Андрій на операції, звільниться після 13:00"

## ТИПОВИЙ РОЗКЛАД АНДРІЯ
Пн-Пт:
- 08:30-13:00 — робота в лікарні (операції, прийом)
- 13:00-15:00 — вільний або особисті справи
- 15:00-19:00 — приватна клініка або зустрічі
- після 21:00 — вдома, не турбувати

Субота-Неділя: вихідні, відповідає рідко

## ЛІКАР
Андрій Ігорович Номеровський — ортопед-травматолог, м. Одеса.
Спеціалізація: ендопротезування кульшового та колінного суглобів, артроскопія, лікування переломів та травм.

## ДЕ ПРИЙМАЄ
**МКЛ №11** (державна)
вул. Нестеренка, 5а — 8 корпус, 1 поверх, ліве крило, ординаторська
Прийом: пн, ср, пт — перша половина дня
Консультація: безкоштовно (потрібне направлення від сімейного лікаря)

**Клініка Onemed** (приватна)
вул. Якова Бреуса, 26/2, Одеса
Прийом: ср, пт — друга половина дня
Ендопротезування: від 150 000 грн

## ЗАПИС
Телефон: 0673283276
Або через цей чат

## ПІДГОТОВКА ДО ОПЕРАЦІЇ

### Пам'ятка перед операцією:
- Легка їжа в день перед операцією, останній прийом до 20:00
- За 3-4 дні виключити: фрукти, овочі, соки, бобові, газовані напої, м'ясні та молочні продукти у великій кількості, хлібобулочні вироби
- В день операції — їжа та напої заборонені
- Очисна клізма увечері перед операцією (при необхідності)
- Куріння в день операції заборонено
- Зняти лак з нігтів, косметику, прикраси, годинник, знімні зубні протези
- Душ напередодні або вранці перед операцією
- Взяти з собою: халат або спортивний костюм, тапочки, туалетні приналежності, рушник, ложку, чашку
- Планова операція не проводиться в дні місячних
- Після операції може знадобитись бандаж, еластичні колготки — приміряти заздалегідь
- Повідомити лікаря про будь-які зміни здоров'я напередодні

### Аналізи перед операцією:
⚠️ Точний перелік — тільки після консультації з Андрієм Ігоровичем: 0673283276

Орієнтовний список:
- Загальний клінічний аналіз крові та сечі
- Коагулограма, глюкоза, печінковий комплекс
- Група крові
- Обстеження на сифіліс, ВІЛ
- R-обстеження суглобів
- ЕХО/КС
- Дуплексне сканування судин нижніх кінцівок
- ФГДС
- Обстеження на вірусні гепатити
- Консультація суміжних фахівців
- Висновок сімейного лікаря
- Посів з носа, консультація стоматолога

## РЕАБІЛІТАЦІЯ ПІСЛЯ ОПЕРАЦІЙ
Важливо: не давай індивідуальних дозволів без огляду лікаря. Формулюй як загальні орієнтири.

### Після артроскопії колінного суглоба:
- 1-3 день: спокій, лід 10-15 хв, підняте положення ноги
- 3-7 день: легкі рухи, ізометрія квадрицепса
- 2-4 тиждень: ЛФК, ходьба без перевантаження
- Після пластики зв'язок режим значно обмеженіший

### Після пластики ПКС:
- 1-2 тижні: контроль болю, ходьба з милицями
- 2-6 тижнів: поступове збільшення рухів, ЛФК
- 6-12 тижнів: зміцнення м'язів, велотренажер за дозволом
- Біг і спорт — не раніше 4-6 місяців

### Після ендопротезування кульшового суглоба:
- 1-7 день: ходьба з ходунками, профілактика тромбозів
- 2-6 тижнів: поступове збільшення ходьби, ЛФК
- Обмеження: не схрещувати ноги, не сідати дуже низько

### Після ендопротезування колінного суглоба:
- 1-7 день: рання ходьба з опорою, розгинання коліна
- 2-6 тижнів: ЛФК, збільшення згинання
- 6-12 тижнів: зміцнення м'язів

### Після остеосинтезу верхньої кінцівки:
- Перші дні: підняте положення руки, рухи пальцями
- 1-3 тижні: рухи в дозволених суглобах
- Після контрольного рентгену: поступове розширення рухів

### Після остеосинтезу нижньої кінцівки:
- Ходьба тільки з опорою і дозволеним навантаженням
- Контрольний рентген через 4-6 тижнів

### Після операцій на плечі:
- 1-3 тижні: фіксація в ортезі, рухи кистю та ліктем
- 4-8 тижнів: поступове збільшення рухів

### Після операцій на стопі:
- Навантаження обмежене 4-6 тижнів
- Взуття/ортез не знімати без дозволу

### Тривожні симптоми:
ЕКСТРЕНО (103 + повідом лікаря): задишка, біль у грудях, різка кровотеча, оніміння кінцівки
ТЕРМІНОВО (0673283276): температура >37.5, гній з рани, наростаючий біль, почервоніння шва

## ПРАВИЛА
- Не ставиш діагнози, не призначаєш лікування
- Складні випадки: "Андрій Ігорович розгляне особисто. Телефон: 0673283276"
- Спам або тільки емодзі — ігноруй
- Мова відповіді = мова пацієнта (укр/рос)
- Запам'ятовуй ім'я і звертайся по імені
- Тон: теплий, природний, без зайвих слів"""

# ─────────────────────────────────────────────
# 🛠️ УТИЛІТИ
# ─────────────────────────────────────────────

def is_urgent(text):
    urgent_words = [
        "терміново", "срочно", "дуже боляче", "очень больно",
        "не можу ходити", "не могу ходить", "швидка", "скорая",
        "кровотеча", "кровотечение", "гній", "гной",
        "оніміла", "онемела", "похолола", "температура",
        "задишка", "одышка", "біль у грудях", "боль в груди",
        "невідкладно"
    ]
    clean = text.lower().strip()
    if len(clean) < 3:
        return False
    return any(word in clean for word in urgent_words)

def is_spam(text):
    spam_words = ["реклама", "купити", "продам", "казино", "заробіток",
                  "крипто", "bitcoin", "заработок", "розіграш"]
    return any(word in text.lower() for word in spam_words)

def is_flood(chat_id):
    now = datetime.now(KYIV_TZ).timestamp()
    if chat_id not in flood_control:
        flood_control[chat_id] = []
    flood_control[chat_id] = [t for t in flood_control[chat_id] if now - t < 60]
    flood_control[chat_id].append(now)
    return len(flood_control[chat_id]) > 5

def check_urgent_cooldown(chat_id):
    now = datetime.now(KYIV_TZ).timestamp()
    last = urgent_cooldown.get(chat_id, 0)
    if now - last < 600:
        return True
    urgent_cooldown[chat_id] = now
    return False

def is_only_emoji(text):
    for char in text.strip():
        cat = unicodedata.category(char)
        if cat not in ('So', 'Sm', 'Sk', 'Sc', 'Zs') and not char.isspace():
            return False
    return True

async def get_contact_info(message):
    user = getattr(message, "from_user", None)
    if not user:
        return "контакт невідомий"
    if user.username:
        return f"@{user.username}"
    full_name = " ".join(filter(None, [user.first_name, user.last_name]))
    return f"{full_name or 'Telegram user'} | tg://user?id={user.id}"

# ─────────────────────────────────────────────
# 🎙️ ГОЛОСОВІ ПОВІДОМЛЕННЯ
# ─────────────────────────────────────────────

async def transcribe_voice(file_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Транскрибує голосове повідомлення через Claude."""
    import base64
    audio_b64 = base64.b64encode(file_bytes).decode()
    response = await anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Транскрибуй це аудіо повідомлення дослівно. Поверни тільки текст без коментарів."
                },
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": audio_b64
                    }
                }
            ]
        }]
    )
    return response.content[0].text.strip()

# ─────────────────────────────────────────────
# 📊 ЗВІТИ
# ─────────────────────────────────────────────

async def send_daily_report(app):
    now = datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M")
    requests = get_requests()
    if not requests:
        await app.bot.send_message(chat_id=DOCTOR_ID, text=f"📊 Звіт {now}\nЗвернень не було.")
        return
    report = f"📊 Звіт {now} — {len(requests)} звернень:\n\n"
    for i, req in enumerate(requests, 1):
        report += f"{i}. {req}\n"
    await app.bot.send_message(chat_id=DOCTOR_ID, text=report[:4000])
    clear_requests()

# ─────────────────────────────────────────────
# ⏰ ПЛАНУВАЛЬНИК
# ─────────────────────────────────────────────

async def schedule_reports(app):
    global last_report_key
    while True:
        now = datetime.now(KYIV_TZ)
        date_str = now.strftime("%Y-%m-%d")
        for hour, name in ((8, "morning"), (20, "evening")):
            if now.hour == hour and now.minute == 0:
                key = f"{date_str}_report_{name}"
                if last_report_key != key:
                    await send_daily_report(app)
                    last_report_key = key
        await asyncio.sleep(30)

# ─────────────────────────────────────────────
# 🔁 ВІДПОВІДІ ЛІКАРЯ ПАЦІЄНТАМ ЧЕРЕЗ БОТА
# ─────────────────────────────────────────────

DOCTOR_PAUSE_SEC = 3600       # після ручної відповіді лікаря бот мовчить у цьому чаті 1 год
PHOTO_ACK_COOLDOWN = 600      # одне "отримали знімки" на 10 хв
DEBOUNCE_SEC = 8              # збираємо кілька повідомлень підряд в одну відповідь

doctor_active = {}            # chat_id -> timestamp останньої відповіді лікаря
photo_ack = {}                # chat_id -> timestamp останнього підтвердження фото
seen_media_groups = set()
record_offer = {}             # chat_id -> коли востаннє показали кнопку запису
pending = {}                  # chat_id -> {"texts": [...], "message": msg, "task": task}

def _now():
    return datetime.now(KYIV_TZ).timestamp()

def mark_doctor_active(chat_id):
    doctor_active[chat_id] = _now()
    p = pending.pop(chat_id, None)
    if p and p.get("task"):
        p["task"].cancel()

def is_doctor_active(chat_id):
    return _now() - doctor_active.get(chat_id, 0) < DOCTOR_PAUSE_SEC

def save_relay(doctor_msg_id, chat_id, business_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO relay VALUES (?, ?, ?)", (doctor_msg_id, chat_id, business_id))
    conn.commit()
    conn.close()

def get_relay(doctor_msg_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT chat_id, business_id FROM relay WHERE doctor_msg_id=?", (doctor_msg_id,)).fetchone()
    conn.close()
    return row

def add_history(chat_id, role, text):
    patient = get_patient(chat_id)
    patient["history"].append({"role": role, "content": text})
    patient["history"] = patient["history"][-30:]
    save_patient(chat_id, patient)

async def notify_doctor(bot, chat_id, business_id, text):
    """Картка лікарю; відповідь реплаєм на неї піде пацієнту."""
    try:
        msg = await bot.send_message(chat_id=DOCTOR_ID, text=text[:4000])
        save_relay(msg.message_id, chat_id, business_id)
    except Exception as e:
        print(f"notify_doctor failed: {e}")

async def send_to_patient(bot, chat_id, business_id, text, reply_markup=None):
    return await bot.send_message(
        chat_id=chat_id, text=text,
        reply_markup=reply_markup if not business_id else None,
        business_connection_id=business_id,
    )

async def process_doctor_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Лікар відповідає реплаєм на картку пацієнта — бот пересилає відповідь пацієнту."""
    message = update.message or update.business_message
    reply_to = message.reply_to_message if message else None
    relay = get_relay(reply_to.message_id) if reply_to else None
    if not relay:
        await context.bot.send_message(
            chat_id=DOCTOR_ID,
            text="Щоб відповісти пацієнту — свайпни вліво по його картці чи фото і напиши або надиктуй відповідь."
        )
        return
    chat_id, business_id = relay
    try:
        await send_to_patient(context.bot, chat_id, business_id, text)
    except Exception as e:
        await context.bot.send_message(chat_id=DOCTOR_ID, text=f"⚠️ Не вдалось надіслати: {e}")
        return
    mark_doctor_active(chat_id)
    add_history(chat_id, "assistant", text)
    name = get_patient(chat_id).get("name") or chat_id
    await context.bot.send_message(chat_id=DOCTOR_ID, text=f"✅ Надіслано: {name}")

# ─────────────────────────────────────────────
# 🚶 ПАЦІЄНТ ПРИЙШОВ
# ─────────────────────────────────────────────

ARRIVAL_PATTERNS = [
    "я прийшов", "я прийшла", "я прийшли", "ми прийшли", "вже прийшов", "вже прийшла", "вже на місці",
    "я на місці", "ми на місці", "я тут", "ми тут", "я під", "я біля", "чекаю біля", "чекаю під", "чекаю в коридорі",
    "я пришел", "я пришла", "мы пришли", "уже пришел", "уже пришла", "я на месте", "мы на месте", "уже на месте",
    "я под", "я возле", "я у кабинета", "жду у", "жду возле", "жду под", "жду в коридоре", "я в коридоре", "я в коридорі",
    "я приїхав", "я приїхала", "я приехал", "я приехала", "мы приехали", "ми приїхали",
]

def is_arrival(text):
    t = " " + text.lower().replace("ё", "е") + " "
    return any(p in t for p in ARRIVAL_PATTERNS)

def arrival_reply(text):
    ru = any(ch in text.lower() for ch in "ыэё") or any(w in text.lower() for w in ("пришел", "пришла", "приехал", "жду", "месте"))
    return ("Андрей Игоревич подойдёт и встретит вас через пару минут." if ru
            else "Андрій Ігорович підійде і зустріне вас за пару хвилин.")

# ─────────────────────────────────────────────
# 🏥 ОБРОБКА ПОВІДОМЛЕНЬ ПАЦІЄНТІВ
# ─────────────────────────────────────────────

async def queue_patient_text(context, message, text: str):
    """Фільтри + склеювання кількох повідомлень поспіль в одну відповідь."""
    chat_id = message.chat.id
    if is_blacklisted(chat_id) or is_only_emoji(text):
        return
    if is_spam(text):
        add_blacklist(chat_id)
        return
    if is_flood(chat_id):
        return

    business_id = getattr(message, "business_connection_id", None)
    patient = get_patient(chat_id)
    name = patient.get("name") or "Пацієнт"
    contact = await get_contact_info(message)

    if is_arrival(text):
        p = pending.pop(chat_id, None)
        if p and p.get("task"):
            p["task"].cancel()
        reply = arrival_reply(text)
        if not is_doctor_active(chat_id):
            await send_to_patient(context.bot, chat_id, business_id, reply)
            add_history(chat_id, "user", text)
            add_history(chat_id, "assistant", reply)
        await notify_doctor(context.bot, chat_id, business_id,
                            f"🚶 ПАЦІЄНТ ПРИЙШОВ — {name} ({contact})\n«{text}»\n\nВідповіли: «{reply}»")
        add_request(f"🚶 Прийшов: {name} ({contact})")
        return

    if is_doctor_active(chat_id):
        add_history(chat_id, "user", text)
        await notify_doctor(context.bot, chat_id, business_id,
                            f"💬 {name} ({contact}):\n{text}\n\n🤫 Бот мовчить — ти зараз ведеш цей чат. ↩️ Реплай = відповідь пацієнту")
        return

    p = pending.setdefault(chat_id, {"texts": []})
    p["texts"].append(text)
    p["message"] = message
    if p.get("task"):
        p["task"].cancel()
    p["task"] = asyncio.create_task(_flush_patient(chat_id, context))

async def _flush_patient(chat_id, context):
    try:
        await asyncio.sleep(DEBOUNCE_SEC)
    except asyncio.CancelledError:
        return
    p = pending.pop(chat_id, None)
    if not p or not p["texts"]:
        return
    try:
        await process_patient_message(None, context, p["message"], "\n".join(p["texts"]))
    except Exception as e:
        print(f"process_patient_message failed for {chat_id}: {e}")

async def process_patient_message(update, context: ContextTypes.DEFAULT_TYPE, message, text: str):
    chat_id = message.chat.id
    business_id = getattr(message, "business_connection_id", None)
    patient = get_patient(chat_id)
    contact = await get_contact_info(message)

    urgent = is_urgent(text) and not check_urgent_cooldown(chat_id)

    system = SYSTEM_PROMPT
    if patient.get("long_memory"):
        system += f"\n\n## ПАМ'ЯТЬ ПРО ЦЬОГО ПАЦІЄНТА\n{patient['long_memory']}"

    patient["history"].append({"role": "user", "content": text})

    try:
        response = await anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=system,
            messages=_clean_history(patient["history"][-10:])
        )
        reply = response.content[0].text
    except Exception as e:
        await context.bot.send_message(chat_id=DOCTOR_ID, text=f"⚠️ Помилка Claude для чату {chat_id}: {e}")
        reply = "Дякуємо, повідомлення отримали. Андрій Ігорович перегляне і зв'яжеться з вами."

    patient["history"].append({"role": "assistant", "content": reply})
    patient["history"] = patient["history"][-30:]

    if not patient.get("name"):
        words = text.split()
        for i, word in enumerate(words):
            if word.lower() in ["мене", "я", "меня"] and i + 1 < len(words):
                patient["name"] = words[i + 1].strip(",.!").capitalize()
                break

    if len(patient["history"]) % 10 == 0:
        try:
            mem_response = await anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": f"З цього діалогу витягни ключові медичні факти про пацієнта (діагноз, операції, скарги, ім'я). Коротко, до 10 рядків:\n\n{json.dumps(patient['history'][-10:], ensure_ascii=False)}"
                }]
            )
            patient["long_memory"] = mem_response.content[0].text
        except Exception:
            pass

    save_patient(chat_id, patient)

    # Кнопку "Записатись" показуємо не частіше ніж раз на добу в чаті
    buttons = [InlineKeyboardButton("📍 Адреса", callback_data="address")]
    if _now() - record_offer.get(chat_id, 0) > 24 * 3600:
        record_offer[chat_id] = _now()
        buttons.insert(0, InlineKeyboardButton("📅 Записатись", callback_data="record"))
    keyboard = InlineKeyboardMarkup([buttons])
    await send_to_patient(context.bot, chat_id, business_id, reply, keyboard)

    name = patient.get("name") or "Пацієнт"
    head = "🚨 ТЕРМІНОВО!\n" if urgent else ""
    await notify_doctor(context.bot, chat_id, business_id,
                        f"{head}💬 {name} ({contact}):\n{text}\n\n🤖 Бот відповів:\n{reply[:500]}\n\n↩️ Реплай = твоя відповідь пацієнту")
    add_request(f"👤 {name} ({contact}): {text[:60]}")

def _clean_history(history):
    """Anthropic вимагає чергування ролей і старт з user — склеюємо підряд однакові."""
    out = []
    for m in history:
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n" + m["content"]}
        else:
            out.append(dict(m))
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out

def is_owner_in_patient_chat(message):
    """Андрій сам пише пацієнту зі свого акаунта (Telegram Business)."""
    return (message.from_user and message.from_user.id == DOCTOR_ID and message.chat.id != DOCTOR_ID)

# ─────────────────────────────────────────────
# 📨 ХЕНДЛЕРИ
# ─────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.business_message
    if not message or not message.text or not message.from_user:
        return
    if message.from_user.is_bot or message.from_user.id == context.bot.id:
        return

    if is_owner_in_patient_chat(message):
        mark_doctor_active(message.chat.id)
        add_history(message.chat.id, "assistant", message.text)
        return

    if message.chat.id == DOCTOR_ID:
        await process_doctor_message(update, context, message.text)
        return

    await queue_patient_text(context, message, message.text)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.business_message
    if not message or not message.from_user:
        return
    voice = message.voice or message.audio
    if not voice:
        return
    chat_id = message.chat.id

    if is_owner_in_patient_chat(message):
        mark_doctor_active(chat_id)
        return
    if chat_id != DOCTOR_ID and is_blacklisted(chat_id):
        return

    try:
        file = await context.bot.get_file(voice.file_id)
        file_bytes = await file.download_as_bytearray()
        text = await transcribe_voice(bytes(file_bytes))
    except Exception as e:
        if chat_id == DOCTOR_ID:
            await context.bot.send_message(chat_id=DOCTOR_ID, text=f"⚠️ Не вдалось розпізнати голосове: {e}")
        else:
            await notify_doctor(context.bot, chat_id, getattr(message, "business_connection_id", None),
                                f"🎙️ Голосове від {get_patient(chat_id).get('name') or 'пацієнта'} ({await get_contact_info(message)}) — не розпізнано, послухай сам")
        return

    if chat_id == DOCTOR_ID:
        await context.bot.send_message(chat_id=DOCTOR_ID, text=f"🎙️ {text}")
        await process_doctor_message(update, context, text)
    else:
        await queue_patient_text(context, message, text)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.business_message
    if not message:
        return
    chat_id = message.chat.id
    if is_owner_in_patient_chat(message):
        mark_doctor_active(chat_id)
        return
    if is_blacklisted(chat_id) or chat_id == DOCTOR_ID:
        return

    business_id = getattr(message, "business_connection_id", None)
    patient = get_patient(chat_id)
    name = patient.get("name", "")
    contact = await get_contact_info(message)

    # Одне підтвердження на альбом і не частіше ніж раз на 10 хв
    group = message.media_group_id
    first_of_group = not group or group not in seen_media_groups
    if group:
        if len(seen_media_groups) > 1000:
            seen_media_groups.clear()
        seen_media_groups.add(group)
    need_ack = (first_of_group and not is_doctor_active(chat_id)
                and _now() - photo_ack.get(chat_id, 0) > PHOTO_ACK_COOLDOWN)
    if need_ack:
        photo_ack[chat_id] = _now()
        greeting = f"{name}, д" if name else "Д"
        ack = f"{greeting}якуємо, знімки отримали. Андрій Ігорович перегляне і напише вам.\n\nЯкщо терміново — 0673283276"
        await send_to_patient(context.bot, chat_id, business_id, ack)
        add_history(chat_id, "user", "[надіслав фото/документи]" + (f" {message.caption}" if message.caption else ""))
        add_history(chat_id, "assistant", ack)

    try:
        fwd = await context.bot.forward_message(chat_id=DOCTOR_ID, from_chat_id=chat_id, message_id=message.message_id)
        save_relay(fwd.message_id, chat_id, business_id)
    except Exception:
        try:
            cp = await context.bot.send_photo(chat_id=DOCTOR_ID, photo=message.photo[-1].file_id,
                                              caption=f"📷 {name or 'Пацієнт'} ({contact})\n↩️ Реплай = відповідь пацієнту")
            save_relay(cp.message_id, chat_id, business_id)
        except Exception:
            await notify_doctor(context.bot, chat_id, business_id, f"📷 {name or chat_id} ({contact}) надіслав фото")
    if first_of_group:
        add_request(f"📷 Фото від {name or chat_id} ({contact})")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "record":
        await query.message.reply_text("Для запису зателефонуйте: 0673283276\nАбо напишіть зручний час — я передам лікарю.")
    elif query.data == "address":
        await query.message.reply_text(
            "📍 МКЛ №11: вул. Нестеренка, 5а — 8 корпус, 1 поверх, ліве крило\n\n📍 Onemed: вул. Якова Бреуса, 26/2"
        )

# ─────────────────────────────────────────────
# 🚀 ЗАПУСК
# ─────────────────────────────────────────────

async def post_init(app):
    asyncio.create_task(schedule_reports(app))

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling(allowed_updates=["message", "business_message", "callback_query"])

if __name__ == "__main__":
    main()
