"""
Telegram Bot → Claude AI → Notion Expense Logger
Uses Notion REST API directly (works with internal integration tokens ntn_...)
"""

import os
import logging
import httpx
import json
from datetime import date, datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_MCP_TOKEN = os.environ["NOTION_MCP_TOKEN"]
NOTION_DB_ID = os.environ.get("DATABASE_ID", "2582a465-3501-80f0-ab9f-000b680aa8ab")

CATEGORIES = ["Food", "Grocery", "Subscription", "Fashion", "Transportation",
               "Other", "Gadgets", "Rent", "Car", "Books", "Travel", "Fitness"]

SYSTEM_PROMPT = f"""You are an expense parsing assistant. The user describes an expense in natural language.
Extract the details and return ONLY a valid JSON object — no explanation, no markdown fences.

JSON fields:
- "description": short merchant or item name (string)
- "amount": dollar amount as a number (e.g. 12.50)
- "category": one of {json.dumps(CATEGORIES)}
- "date": ISO date string YYYY-MM-DD (default today: {date.today().isoformat()} if not specified)
- "notes": any extra context (string, can be empty)

Return ONLY the raw JSON object, nothing else."""

user_histories: dict[int, list] = {}


async def parse_expense_with_claude(user_id: int, user_message: str) -> dict:
    """Use Claude to parse the expense from natural language into structured JSON."""
    history = user_histories.get(user_id, [])
    history.append({"role": "user", "content": user_message})

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "system": SYSTEM_PROMPT,
                "messages": history,
            },
        )

        if response.status_code != 200:
            logger.error(f"Claude API error {response.status_code}: {response.text}")
            raise Exception(f"Claude API error: {response.status_code}")

        data = response.json()
        raw_text = "".join(
            block["text"] for block in data.get("content", [])
            if block.get("type") == "text"
        ).strip().replace("```json", "").replace("```", "")

        logger.info(f"Claude parsed: {raw_text}")
        parsed = json.loads(raw_text)

        history.append({"role": "assistant", "content": raw_text})
        user_histories[user_id] = history[-20:]
        return parsed


async def log_to_notion(expense: dict) -> None:
    """Create a new row in Notion Expense DB using the REST API."""
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            "https://api.notion.com/v1/pages",
            headers={
                "Authorization": f"Bearer {NOTION_MCP_TOKEN}",
                "Content-Type": "application/json",
                "Notion-Version": "2022-06-28",
            },
            json={
                "parent": {"database_id": NOTION_DB_ID},
                "properties": {
                    "Other": {
                        "title": [{"text": {"content": expense.get("description", "Expense")}}]
                    },
                    "Expense": {
                        "number": float(expense.get("amount", 0))
                    },
                    "Category": {
                        "select": {"name": expense.get("category", "Other")}
                    },
                    "Date": {
                        "date": {"start": expense.get("date", date.today().isoformat())}
                    },
                    "Notes": {
                        "rich_text": [{"text": {"content": expense.get("notes", "")}}]
                    },
                },
            },
        )

        if response.status_code != 200:
            logger.error(f"Notion API error {response.status_code}: {response.text}")
            raise Exception(f"Notion error {response.status_code}: {response.text[:200]}")

        logger.info(f"Notion entry created: {response.json().get('url')}")


# ── Telegram handlers ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! I'm your Expense Logger Bot.\n\n"
        "Just tell me what you spent and I'll log it to your Notion database.\n\n"
        "Examples:\n"
        "• Spent $12 at Chipotle for lunch\n"
        "• Paid $3.50 for Uber\n"
        "• Bought groceries at Trader Joe's for $67\n\n"
        "Use /clear to reset conversation history."
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_histories.pop(update.effective_user.id, None)
    await update.message.reply_text("🗑️ History cleared. Fresh start!")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        expense = await parse_expense_with_claude(user_id, text)

        if not expense.get("amount"):
            await update.message.reply_text("🤔 Couldn't find an amount. Try: 'Spent $12 at Chipotle'")
            return

        await log_to_notion(expense)

        amount = expense.get("amount", 0)
        description = expense.get("description", "Expense")
        category = expense.get("category", "Other")
        exp_date = expense.get("date", date.today().isoformat())

        try:
            fmt_date = datetime.strptime(exp_date, "%Y-%m-%d").strftime("%b %-d")
        except Exception:
            fmt_date = exp_date

        await update.message.reply_text(
            f"✅ Logged ${amount:.2f} — {description} ({category}) · {fmt_date}"
        )

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        await update.message.reply_text("🤔 Couldn't understand that. Try: 'Spent $12 at Chipotle'")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"⚠️ Error: {str(e)[:150]}")


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()