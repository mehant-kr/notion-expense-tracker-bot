"""
Telegram Bot → Claude AI → Notion Expense Logger
------------------------------------------------
Setup:
  1. Get a Telegram bot token from @BotFather
  2. Set environment variables (see .env.example)
  3. Run: pip install -r requirements.txt
  4. Run: python bot.py
"""

import os
import json
import logging
import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_DB_ID = "2582a465-3501-80f0-ab9f-000b680aa8ab"

SYSTEM_PROMPT = f"""You are an expense logging assistant connected to the user's Notion database.
The user will describe an expense in natural language via Telegram.

Your job:
1. Parse the expense: amount (USD number), description/merchant, category, date (default today: {__import__('datetime').date.today().isoformat()}), notes.
2. Use the Notion MCP tool to create a new page in data source ID: {NOTION_DB_ID} with:
   - "Other": merchant or short description (the title field)
   - "Expense": dollar amount as a number
   - "Category": one of [Food, Grocery, Subscription, Fashion, Transportation, Other, Gadgets, Rent, Car, Books, Travel, Fitness]
   - "date:Date:start": ISO date (YYYY-MM-DD)
   - "date:Date:is_datetime": 0
   - "Notes": any extra context

After logging, reply concisely (Telegram message style), e.g.:
✅ Logged $12.50 — Chipotle (Food) · Mar 5

If you cannot find an amount, ask the user to clarify.
Keep all replies short and friendly — this is a chat interface."""

# Store per-user conversation history
user_histories: dict[int, list] = {}

async def call_claude_with_notion(user_id: int, user_message: str) -> str:
    """Send message to Claude with Notion MCP and return the response text."""
    history = user_histories.get(user_id, [])
    history.append({"role": "user", "content": user_message})

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "mcp-client-2025-04-04",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "system": SYSTEM_PROMPT,
                "messages": history,
                "mcp_servers": [
                    {
                        "type": "url",
                        "url": "https://mcp.notion.com/mcp",
                        "name": "notion",
                        "authorization_token": os.environ["NOTION_MCP_TOKEN"],
                    }
                ],
            },
        )
        response.raise_for_status()
        data = response.json()

    # Extract text from response
    reply = " ".join(
        block["text"]
        for block in data.get("content", [])
        if block.get("type") == "text"
    ).strip()

    # Keep conversation history (last 10 turns to avoid token bloat)
    history.append({"role": "assistant", "content": reply})
    user_histories[user_id] = history[-20:]

    return reply or "⚠️ Something went wrong. Please try again."


# ── Telegram handlers ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! I'm your *Expense Logger Bot*.\n\n"
        "Just tell me what you spent and I'll log it to your Notion database automatically.\n\n"
        "Examples:\n"
        "• _Spent $12 at Chipotle for lunch_\n"
        "• _Paid $3.50 for Uber_\n"
        "• _Bought groceries at Trader Joe's for $67_\n\n"
        "Use /clear to reset conversation history.",
        parse_mode="Markdown",
    )

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_histories.pop(user_id, None)
    await update.message.reply_text("🗑️ History cleared. Fresh start!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # Show typing indicator
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        reply = await call_claude_with_notion(user_id, text)
        await update.message.reply_text(reply)
    except httpx.HTTPError as e:
        logger.error(f"API error: {e}")
        await update.message.reply_text("⚠️ API error. Please try again in a moment.")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await update.message.reply_text("⚠️ Something went wrong. Please try again.")


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
