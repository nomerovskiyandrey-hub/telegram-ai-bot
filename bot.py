import os
import asyncio
import sqlite3
import json
from datetime import datetime
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CallbackQueryHandler

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DOCTOR_ID = 262491197

flood_control = {}
urgent_cooldown = {}

# ===== БАЗА ДАНИХ =====
def init_db():
    conn = sqlite3.connect("/tmp/bot.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS patients (
        chat_id INTEGER PRIMARY KEY,
        name TEXT DEFAULT '',
        history TEXT DEFAULT '[]'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS daily_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS blacklist (
        chat_id INTEGER PRIMARY KEY
    )""")
    conn.commit()
    conn.close()

def get_patient(chat_id):
    conn = sqlite3.connect("/tmp/bot.db")
    c = conn.cursor()
    c.execute("SELECT name, history FROM patients WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"name": row[0], "history": json.loads(row[1])}
    return {"name": "", "history": []}

def save_patient(chat_id, data):
    conn = sqlite3.connect("/tmp/bot.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO patients (chat_id, name, history) VALUES (?,?,?)",
              (chat_id, data["name"], json.dumps(data["history"])))
    conn.commit()
    conn.close()

def add_request(text):
    conn = sqlite3.connect("/tmp/bot.db")
    c = conn.cursor()
    c.execute("INSERT INTO daily_requests (text, created_at) VALUES (?,?)",
              (text, datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit()
    conn.close()

def get_requests():
    conn = sqlite3.connect("/tmp/bot.db")
    c = conn.cursor()
    c.execute("SELECT text FROM daily_requests ORDER BY id")
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows

def clear_requests():
    conn = sqlite3.connect("/tmp/bot.db")
    c = conn.cursor()
    c.execute("DELETE FROM daily_requests")
    conn.commit()
    conn.close()

def is_blacklisted(chat_id):
    conn = sqlite3.connect("/tmp/bot.db")
    c = conn.cursor()
    c.execute("SELECT 1 FROM blacklist WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

def add_blacklist(chat_id):
    conn = sqlite3.connect("/tmp/bot.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO blacklist (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

# ===== ПРОМПТ =====
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
- Повідомити лікаря про будь-які зміни здоров'я напередодні (лихоманка, нежить, висипання)

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
Важливо: не давай індивідуальних дозволів без огляду лікаря. Завжди уточнюй яка операція, дата, кінцівка. Формулюй як загальні орієнтири.

### Після артроскопії колінного суглоба:
- 1-3 день: спокій, лід 10-15 хв, підняте положення ноги
- 3-7 день: легкі рухи, ізометрія квадрицепса
- 2-4 тиждень: ЛФК, ходьба без перевантаження
- Після пластики зв'язок режим значно обмеженіший

### Після ендопротезування кульшового суглоба:
- 1-7 день: ходьба з ходунками, профілактика тромбозів
- 2-6 тижнів: поступове збільшення ходьби, ЛФК
- Обмеження: не схрещувати ноги, не сідати дуже низько

### Після ендопротезування колінного суглоба:
- 1-7 день: рання ходьба з опорою, розгинання коліна
- 2-6 тижнів: ЛФК, збільшення згинання
- 6-12 тижнів: зміцнення м'язів

### Після остеосинтезу перелому:
- Навантаження тільки з дозволу лікаря
- Контрольний рентген через 4-6 тижнів
- Не знімати іммобілізацію без дозволу

### Тривожні симптоми після операції:
ЕКСТРЕНО (103 + повідом лікаря):
- Задишка, біль у грудях
- Різка кровотеча
- Похолодання/оніміння кінцівки

ТЕРМІНОВО (зв'язатись з лікарем 0673283276):
- Температура вище 37.5°C
- Виділення з рани
- Наростаючий біль або набряк
- Почервоніння навколо шва

## ПРАВИЛА
- Не ставиш діагнози
- Не призначаєш лікування
- Складні випадки: "Андрій Ігорович розгляне особисто. Телефон: 0673283276"
- Спам або тільки емодзі — ігноруй
- Мова відповіді = мова пацієнта (укр/рос)
- Запам'ятовуй ім'я і звертайся по імені
- Тон: теплий, природний, без зайвих слів"""

# ===== УТИЛІТИ =====
def is_urgent(text):
    urgent_words = ["терміново", "срочно", "дуже боляче", "очень больно", "невідкладно", "не можу ходити", "не могу ходить", "швидка", "скорая"]
    clean = text.lower().strip()
    if len(clean) < 3:
        return False
    return any(word in clean for word in urgent_words)

def is_spam(text):
    spam_words = ["реклама", "купити", "продам", "казино", "заробіток", "крипто", "bitcoin", "заработок", "розіграш"]
    return any(word in text.lower() for word in spam_words)

def is_flood(chat_id):
    now = datetime.now().timestamp()
    if chat_id not in flood_control:
        flood_control[chat_id] = []
    flood_control[chat_id] = [t for t in flood_control[chat_id] if now - t < 60]
    flood_control[chat_id].append(now)
    return len(flood_control[chat_id]) > 5

def check_urgent_cooldown(chat_id):
    now = datetime.now().timestamp()
    last = urgent_cooldown.get(chat_id, 0)
    if now - last < 600:
        return True
    urgent_cooldown[chat_id] = now
    return False

def is_only_emoji(text):
    import unicodedata
    for char in text.strip():
        cat = unicodedata.category(char)
        if cat not in ('So', 'Sm', 'Sk', 'Sc', 'Zs') and not char.isspace():
            return False
    return True

async def get_contact_info(message):
    user = getattr(message, 'from_user', None)
    if not user:
        return "контакт невідомий"
    if user.username:
        return f"@{user.username}"
    name = " ".join(filter(None, [user.first_name, user.last_name]))
    return f"{name or 'Telegram user'} | tg://user?id={user.id}"

# ===== ЗВІТИ =====
async def send_daily_report(app):
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    requests = get_requests()
    if not requests:
        await app.bot.send_message(chat_id=DOCTOR_ID, text=f"📊 Звіт {now}\nЗвернень не було.")
        return
    report = f"📊 Звіт {now} — {len(requests)} звернень:\n\n"
    for i, req in enumerate(requests, 1):
        report += f"{i}. {req}\n"
    await app.bot.send_message(chat_id=DOCTOR_ID, text=report[:4000])
    clear_requests()

async def schedule_reports(app):
    while True:
        now = datetime.now()
        if now.hour in [8, 20] and now.minute == 0:
            await send_daily_report(app)
        await asyncio.sleep(60)

# ===== ОБРОБКА ПОВІДОМЛЕНЬ =====
async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message):
    chat_id = message.chat.id
    text = message.text

    if chat_id == DOCTOR_ID:
        return
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

    if is_urgent(text) and not check_urgent_cooldown(chat_id):
        contact = await get_contact_info(message)
        name = patient.get('name') or 'Невідомий'
        await context.bot.send_message(
            chat_id=DOCTOR_ID,
            text=f"🚨 ТЕРМІНОВО!\nПацієнт: {name}\nКонтакт: {contact}\nПише: {text}"
        )

    patient["history"].append({"role": "user", "content": text})
    history = patient["history"][-10:]

    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=history
        )
        reply = response.content[0].text
    except Exception as e:
        await context.bot.send_message(
            chat_id=DOCTOR_ID,
            text=f"⚠️ Помилка Claude для чату {chat_id}: {e}"
        )
        reply = "Дякую, повідомлення отримали. Андрій Ігорович перегляне і зв'яжеться з вами."

    patient["history"].append({"role": "assistant", "content": reply})

    if not patient.get("name"):
        words = text.split()
        for i, word in enumerate(words):
            if word.lower() in ["мене", "я", "меня"] and i + 1 < len(words):
                patient["name"] = words[i + 1].capitalize()
                break

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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.business_message
    if not message or not message.text:
        return
    if message.from_user and message.from_user.is_bot:
        return
    await process_message(update, context, message)

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

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"{greeting}якую отримали ваші знімки. Андрій Ігорович розгляне і зв'яжеться найближчим часом.\n\nЯкщо терміново — зателефонуйте: 0673283276",
        business_connection_id=business_id
    )

    # Пересилаємо фото лікарю
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
            text=f"📷 {name or chat_id} ({contact}) надіслав фото"
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

async def post_init(app):
    init_db()
    asyncio.create_task(schedule_reports(app))

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling(allowed_updates=["message", "business_message", "callback_query"])

if __name__ == "__main__":
    main()
