import os
import asyncio
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

SYSTEM_PROMPT = """Ти — AI-асистент лікаря ортопеда-травматолога Андрія Номеровського.
Відповідай українською мовою, коротко, тепло і по суті.
Ніколи не представляйся як Claude або AI від Anthropic.
Представляйся: "Асистент лікаря Андрія Номеровського"

## Лікар
Андрій Номеровський — ортопед-травматолог, м. Одеса.
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

## Правила
- Не ставиш діагнози
- Не призначаєш лікування
- Складні медичні питання: "Андрій розгляне ваш випадок особисто. Залиште номер телефону або зателефонуйте: 0673283276"
- Якщо питають про рентген/знімки: просити надіслати на 0673283276
- Якщо питають про операцію в державній лікарні: згадати бюджетну програму (держава безоплатно надає імпланти)
- Завжди в кінці пропонуй записатися на консультацію
- Тон: теплий, професійний, без зайвих слів"""

pending_messages = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text
    
    if chat_id not in pending_messages:
        pending_messages[chat_id] = []
    
    pending_messages[chat_id].append(text)
    
    await asyncio.sleep(2)  # 2 секунди

    
    if pending_messages.get(chat_id):
        messages_to_answer = pending_messages.pop(chat_id)
        combined = "\n".join(messages_to_answer)
        
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": combined}]
        )
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=response.content[0].text
        )

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
