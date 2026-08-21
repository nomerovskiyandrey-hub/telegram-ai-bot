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

KYIV_TZ = ZoneInfo("Europe/Kiev")

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
- Завжди пропонуй записатись

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

DOCTOR_SYSTEM_PROMPT = """Ти — особистий AI-асистент Андрія Номеровського, лікаря ортопеда-травматолога з Одеси.

## ХТО ТАКИЙ АНДРІЙ
- Ортопед-травматолог, спеціалізація: ендопротезування кульшового та колінного суглобів
- Працює в МКЛ №11 (державна) і клініці Onemed (приватна)
- Мета: 10-13 ендопротезів/місяць, дохід 8-10k$/міс
- Зараз дохід ~2-3k$/міс
- Двоє дітей: Міша (5 років) і Святослав (7 місяців)
- 6 місяців без шкідливих звичок

## 3 ЯКОРІ ДНЯ
- 🌅 Ранок: 10 хв руху + вода + 1 ціль дня
- 💰 День: фіксувати кожну витрату
- ❤️ Вечір: одна тепла дія для дружини

## СТИЛЬ
- Коротко і по суті — Андрій цінує час
- Мотивуй через результат і гроші
- Звертайся на "ти"
- Відповідай мовою на якій пише (укр або рос)
- Допомагай з: плануванням дня, написанням сторіс для Instagram, аналізом ситуацій, медичними питаннями по спеціальності

## ДОВГА ПАМ'ЯТЬ
{long_memory}"""

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
# 🧠 ДОВГА ПАМ'ЯТЬ ЛІКАРЯ
# ─────────────────────────────────────────────

async def update_doctor_memory(new_message: str, reply: str, current_memory: str) -> str:
    """Оновлює довгу пам'ять після кожного діалогу з лікарем."""
    response = await anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": f"""Поточна пам'ять:
{current_memory}

Новий діалог:
Андрій: {new_message}
Асистент: {reply}

Онови пам'ять — додай важливі факти, рішення, плани. Видали застарілу інформацію.
Формат: короткі пункти. Максимум 30 рядків. Тільки факти про Андрія."""
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
# ⏰ ЩОДЕННІ ПОВІДОМЛЕННЯ
# ─────────────────────────────────────────────

async def send_morning_question(app):
    await app.bot.send_message(
        chat_id=DOCTOR_ID,
        text=(
            "🌅 Андрій, доброго ранку.\n\n"
            "🎯 *Ціль дня* — одне найважливіше:\n\n"
            "📋 *Задачі:*\n"
            "— Сьогодні:\n"
            "— Завтра:\n\n"
            "📱 *Сторис на сьогодні* — яка тема?\n\n"
            "💡 Нагадую: *10-13 ендопротезів/місяць.* Що сьогодні наближає тебе до неї?"
        ),
        parse_mode="Markdown"
    )

async def send_evening_checkin(app):
    await app.bot.send_message(
        chat_id=DOCTOR_ID,
        text=(
            "📋 Підсумок дня. По-швидкому:\n\n"
            "😊 *Як ти?* _(виснажений / нормально / добре / вогонь)_\n"
            "🧠 *Стрес?* _(спокій / помірний / напружено / перегрів)_\n\n"
            "✅ *Чекліст:*\n"
            "— Рух + вода вранці → так/ні\n"
            "— Витрати фіксував → так/ні\n"
            "— Побув із сім'єю без телефону → так/ні\n\n"
            "🦴 *Пацієнтів / операцій:*\n"
            "💰 *Дохід (₴):*\n"
            "💸 *Витрати (₴):*\n"
            "⚖️ Баланс = _порахую сам_"
        ),
        parse_mode="Markdown"
    )

async def send_night_question(app):
    await app.bot.send_message(
        chat_id=DOCTOR_ID,
        text=(
            "🌙 Перед сном — три питання:\n\n"
            "💡 *Головний інсайт дня* — що зрозумів?\n\n"
            "📋 *Завтра* — що найважливіше?\n\n"
            "✨ *Що сьогодні було твоїм — навіть маленьким?*"
        ),
        parse_mode="Markdown"
    )

# ─────────────────────────────────────────────
# ⏰ ПЛАНУВАЛЬНИК
# ─────────────────────────────────────────────

async def schedule_reports(app):
    global last_report_key
    while True:
        now = datetime.now(KYIV_TZ)
        date_str = now.strftime("%Y-%m-%d")

        if now.hour == 7 and now.minute == 0:
            key = f"{date_str}_morning"
            if last_report_key != key:
                await send_morning_question(app)
                last_report_key = key

        if now.hour == 8 and now.minute == 0:
            key = f"{date_str}_report_morning"
            if last_report_key != key:
                await send_daily_report(app)
                last_report_key = key

        if now.hour == 19 and now.minute == 0:
            key = f"{date_str}_checkin"
            if last_report_key != key:
                await send_evening_checkin(app)
                last_report_key = key

        if now.hour == 20 and now.minute == 0:
            key = f"{date_str}_report_evening"
            if last_report_key != key:
                await send_daily_report(app)
                last_report_key = key

        if now.hour == 21 and now.minute == 0:
            key = f"{date_str}_night"
            if last_report_key != key:
                await send_night_question(app)
                last_report_key = key

        await asyncio.sleep(30)

# ─────────────────────────────────────────────
# 👨‍⚕️ ОБРОБКА ПОВІДОМЛЕНЬ ЛІКАРЯ
# ─────────────────────────────────────────────

async def process_doctor_message(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Відповідає лікарю як особистий асистент з довгою пам'яттю."""
    current_memory = get_doctor_memory()

    system = DOCTOR_SYSTEM_PROMPT.format(
        long_memory=current_memory if current_memory else "Поки що порожньо — пам'ять заповнюється з часом."
    )

    try:
        response = await anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=system,
            messages=[{"role": "user", "content": text}]
        )
        reply = response.content[0].text
    except Exception as e:
        await context.bot.send_message(chat_id=DOCTOR_ID, text=f"⚠️ Помилка Claude: {e}")
        return

    await context.bot.send_message(chat_id=DOCTOR_ID, text=reply)

    # Оновлюємо довгу пам'ять асинхронно
    try:
        new_memory = await update_doctor_memory(text, reply, current_memory)
        save_doctor_memory(new_memory)
    except Exception:
        pass

# ─────────────────────────────────────────────
# 🏥 ОБРОБКА ПОВІДОМЛЕНЬ ПАЦІЄНТІВ
# ─────────────────────────────────────────────

async def process_patient_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message, text: str):
    chat_id = message.chat.id

    if is_blacklisted(chat_id):
        return
    if is_only_emoji(text):
        return
    if is_spam(text):
        add_blacklist(chat_id)
        return
    if is_flood(chat_id):
        return

    patient = get_patient(chat_id)

    # Термінове повідомлення лікарю
    if is_urgent(text) and not check_urgent_cooldown(chat_id):
        contact = await get_contact_info(message)
        name = patient.get('name') or 'Невідомий'
        await context.bot.send_message(
            chat_id=DOCTOR_ID,
            text=f"🚨 ТЕРМІНОВО!\nПацієнт: {name}\nКонтакт: {contact}\nПише: {text}"
        )

    # Системний промпт з довгою пам'яттю пацієнта
    system = SYSTEM_PROMPT
    if patient.get("long_memory"):
        system += f"\n\n## ПАМ'ЯТЬ ПРО ЦЬОГО ПАЦІЄНТА\n{patient['long_memory']}"

    patient["history"].append({"role": "user", "content": text})

    try:
        response = await anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=system,
            messages=patient["history"][-10:]
        )
        reply = response.content[0].text
    except Exception as e:
        await context.bot.send_message(
            chat_id=DOCTOR_ID,
            text=f"⚠️ Помилка Claude для чату {chat_id}: {e}"
        )
        reply = "Дякую, повідомлення отримали. Андрій Ігорович перегляне і зв'яжеться з вами."

    patient["history"].append({"role": "assistant", "content": reply})
    patient["history"] = patient["history"][-30:]

    # Витягуємо ім'я пацієнта
    if not patient.get("name"):
        words = text.split()
        for i, word in enumerate(words):
            if word.lower() in ["мене", "я", "меня"] and i + 1 < len(words):
                patient["name"] = words[i + 1].capitalize()
                break

    # Оновлюємо довгу пам'ять пацієнта кожні 10 повідомлень
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

    keyboard = [[
        InlineKeyboardButton("📅 Записатись", callback_data="record"),
        InlineKeyboardButton("📍 Адреса", callback_data="address"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    business_id = getattr(message, 'business_connection_id', None)
    await context.bot.send_message(
        chat_id=chat_id,
        text=reply,
        reply_markup=reply_markup if not business_id else None,
        business_connection_id=business_id
    )

    contact = await get_contact_info(message)
    name = patient.get("name") or str(chat_id)
    add_request(f"👤 {name} ({contact}): {text[:60]}")

# ─────────────────────────────────────────────
# 📨 ХЕНДЛЕРИ
# ─────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.business_message
    if not message or not message.text:
        return
    if message.from_user and message.from_user.is_bot:
        return

    chat_id = message.chat.id
    text = message.text

    # Лікар пише — відповідаємо як особистий асистент
    if chat_id == DOCTOR_ID:
        await process_doctor_message(update, context, text)
        return

    # Пацієнт пише
    await process_patient_message(update, context, message, text)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробляє голосові повідомлення."""
    message = update.message or update.business_message
    if not message:
        return

    chat_id = message.chat.id
    voice = message.voice or message.audio

    if not voice:
        return

    # Повідомлення що обробляємо
    processing_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="🎙️ Обробляю голосове повідомлення..."
    )

    try:
        # Завантажуємо файл
        file = await context.bot.get_file(voice.file_id)
        file_bytes = await file.download_as_bytearray()

        # Транскрибуємо
        transcribed_text = await transcribe_voice(bytes(file_bytes))

        # Видаляємо повідомлення "обробляю"
        await processing_msg.delete()

        if chat_id == DOCTOR_ID:
            # Лікарю показуємо транскрипцію і відповідаємо
            await context.bot.send_message(
                chat_id=DOCTOR_ID,
                text=f"🎙️ _Розпізнано:_ {transcribed_text}",
                parse_mode="Markdown"
            )
            await process_doctor_message(update, context, transcribed_text)
        else:
            # Пацієнту — обробляємо як текст
            if not is_blacklisted(chat_id) and not is_spam(transcribed_text):
                await process_patient_message(update, context, message, transcribed_text)

    except Exception as e:
        await processing_msg.delete()
        if chat_id == DOCTOR_ID:
            await context.bot.send_message(
                chat_id=DOCTOR_ID,
                text=f"⚠️ Не вдалось розпізнати голосове: {e}"
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Вибачте, не вдалось розпізнати голосове повідомлення. Напишіть текстом, будь ласка."
            )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.business_message
    if not message:
        return
    chat_id = message.chat.id
    if is_blacklisted(chat_id) or chat_id == DOCTOR_ID:
        return

    patient = get_patient(chat_id)
    name = patient.get("name", "")
    greeting = f"{name}, д" if name else "Д"
    business_id = getattr(message, 'business_connection_id', None)

    keyboard = [[
        InlineKeyboardButton("📅 Записатись", callback_data="record"),
        InlineKeyboardButton("📍 Адреса", callback_data="address"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"{greeting}якую отримали ваші знімки. Андрій Ігорович розгляне і зв'яжеться найближчим часом.\n\nЯкщо терміново — зателефонуйте: 0673283276",
        reply_markup=reply_markup if not business_id else None,
        business_connection_id=business_id
    )

    try:
        await context.bot.forward_message(
            chat_id=DOCTOR_ID,
            from_chat_id=chat_id,
            message_id=message.message_id
        )
    except Exception:
        contact = await get_contact_info(message)
        await context.bot.send_message(
            chat_id=DOCTOR_ID,
            text=f"📷 {name or chat_id} ({contact}) надіслав фото — переслати вручну"
        )

    contact = await get_contact_info(message)
    add_request(f"📷 Фото від {name or chat_id} ({contact})")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "record":
        await query.message.reply_text(
            "Для запису зателефонуйте: 0673283276\nАбо напишіть зручний час — я передам лікарю."
        )
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
