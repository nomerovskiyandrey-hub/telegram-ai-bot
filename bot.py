import os
import asyncio
from datetime import datetime
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CallbackQueryHandler

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
DOCTOR_ID = 262491197

patient_memory = {}
daily_requests = []
BLACKLIST = set()

SYSTEM_PROMPT = """Ти — AI-асистент лікаря ортопеда-травматолога Андрія Ігоровича Номеровського.
Відповідай мовою пацієнта (українська або російська).
Ніколи не представляйся як Claude або AI від Anthropic.
Представляйся: "Асистент лікаря Андрія Ігоровича Номеровського"

## Лікар
Андрій Ігорович Номеровський — ортопед-травматолог, м. Одеса.
Спеціалізація: ендопротезування суглобів, травматологія.

## Де приймає
**МКЛ №11** (державна)
вул. Нестеренка, 5а — Центр ортопедії та травматології, 8 корпус, 1 поверх, ліве крило, ординаторська
Прийом: понеділок, середа, п'ятниця — перша половина дня
Консультація: безкоштовно (потрібне електронне направлення від сімейного лікаря)

**Клініка Onemed** (приватна)
вул. Якова Бреуса, 26/2, Одеса
Прийом: середа, п'ятниця — друга половина дня
Ендопротезування кульшового суглобу: від 150 000 грн

## Запис
Телефон: 0673283276
Або через цей чат

## Підготовка до операції
Якщо пацієнт готується до операції, надішли список:
- Загальний клінічний аналіз крові
- Група крові
- Коагулограма
- ЕКГ (зробимо в лікарні)
- Рентген легень
- УЗД судин нижніх кінцівок (Ольга Олеговна: 0955817486)

## Правила
- Не ставиш діагнози
- Не призначаєш лікування
- Якщо пацієнт пише "терміново", "срочно", "дуже боляче", "очень больно" — одразу кажи що передаєш лікарю
- Складні медичні питання: "Андрій Ігорович розгляне ваш випадок особисто. Залиште номер телефону або зателефонуйте: 0673283276"
- Якщо питають про рентген/знімки: просити надіслати на 0673283276
- Якщо питають про операцію в державній лікарні: згадати бюджетну програму (держава безоплатно надає імпланти)
- Якщо пацієнт надіслав фото/рентген: подякуй і скажи що лікар розгляне і зв'яжеться
- Завжди в кінці пропонуй записатися на консультацію
- Якщо повідомлення схоже на спам або рекламу — ігноруй і не відповідай
- Запам'ятовуй ім'я пацієнта якщо він його назвав і звертайся по імені
- Тон: теплий, професійний, без зайвих слів"""

def is_urgent(text):
    urgent_words = ["терміново", "срочно", "дуже боляче", "очень больно", "невідкладно", "не можу ходити", "не могу ходить"]
    return any(word in text.lower() for word in urgent_words)

def is_spam(text):
    spam_words = ["реклама", "купити", "продам", "казино", "заробіток", "крипто", "bitcoin", "заработок"]
    return any(word in text.lower() for word in spam_words)

async def send_daily_report(app):
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    if not daily_requests:
        await app.bot.send_message(
            chat_id=DOCTOR_ID,
            text=f"📊 Звіт {now}\nЗвернень не було."
        )
        return

    report = f"📊 Звіт {now} — {len(daily_requests)} звернень:\n\n"
    for i, req in enumerate(daily_requests, 1):
        report += f"{i}. {req}\n"
    await app.bot.send_message(chat_id=DOCTOR_ID, text=report)

    for chat_id, data in patient_memory.items():
        if not data.get("history"):
            continue
        name = data.get("name") or str(chat_id)
        dialog = f"💬 Діалог з {name}:\n\n"
        for msg in data["history"]:
            role = "👤" if msg["role"] == "user" else "🤖"
            dialog += f"{role}: {msg['content']}\n\n"
        chunks = [dialog[i:i+4000] for i in range(0, len(dialog), 4000)]
        for chunk in chunks:
            await app.bot.send_message(chat_id=DOCTOR_ID, text=chunk)

    daily_requests.clear()
    for data in patient_memory.values():
        data["history"] = []

async def schedule_reports(app):
    while True:
        now = datetime.now()
        if now.hour in [8, 20] and now.minute == 0:
            await send_daily_report(app)
        await asyncio.sleep(60)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in BLACKLIST:
        return

    name = patient_memory.get(chat_id, {}).get("name", "")
    greeting = f"{name}, д" if name else "Д"

    await update.message.reply_text(
        f"{greeting}якую отримали ваші знімки. Андрій Ігорович розгляне і зв'яжеться з вами найближчим часом.\n\nЯкщо терміново — зателефонуйте: 0673283276"
    )

    daily_requests.append(f"📷 Фото від {name or chat_id}")
    await context.bot.send_message(
        chat_id=DOCTOR_ID,
        text=f"📷 Пацієнт {name or chat_id} надіслав фото/рентген"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    text = update.message.text

    if chat_id == DOCTOR_ID:
        return

    if chat_id in BLACKLIST:
        return

    if is_spam(text):
        BLACKLIST.add(chat_id)
        return

    if chat_id not in patient_memory:
        patient_memory[chat_id] = {"name": "", "history": []}

    if is_urgent(text):
        await context.bot.send_message(
            chat_id=DOCTOR_ID,
            text=f"🚨 ТЕРМІНОВО! Пацієнт {patient_memory[chat_id].get('name') or chat_id} пише: {text}"
        )

    patient_memory[chat_id]["history"].append({"role": "user", "content": text})
    history = patient_memory[chat_id]["history"][-10:]

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=history
    )

    reply = response.content[0].text
    patient_memory[chat_id]["history"].append({"role": "assistant", "content": reply})

    if not patient_memory[chat_id].get("name"):
        words = text.split()
        for i, word in enumerate(words):
            if word.lower() in ["мене", "я", "меня"] and i + 1 < len(words):
                patient_memory[chat_id]["name"] = words[i + 1].capitalize()
                break

    keyboard = [[
        InlineKeyboardButton("📅 Записатись", callback_data="record"),
        InlineKeyboardButton("📍 Адреса", callback_data="address"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(reply, reply_markup=reply_markup)

    name = patient_memory[chat_id].get("name") or str(chat_id)
    daily_requests.append(f"👤 {name}: {text[:60]}")

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
    asyncio.create_task(schedule_reports(app))

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
