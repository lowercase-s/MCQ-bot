import asyncio
import json
import os
import re



from dotenv import load_dotenv

load_dotenv()

from pymongo import MongoClient

MONGO_URI = os.getenv("MONGODB_URI")
client = MongoClient(MONGO_URI)
db = client["mcqbot"]
users_col = db["users"]
feedback_col = db["feedbacks"]

from datetime import datetime

import google.generativeai as genai
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OWNER_ID = 5081349515
GROUP_CHAT_ID = -1002359353655
BATCH_MODE = False
OWNER_QUEUE = []
USER_BATCHES = {}
USER_STATE = {}
USER_MODE = {}
USER_DAILY_USAGE = {}
KNOWN_USERS = {}
MENU_MESSAGE_ID = {}
USER_POLL_COUNT = {}


genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

#=============
#WELCOME TEXT
#=============
WELCOME_TEXT = """👋 <b>Welcome to the MCQ Bot!</b>

Generate multiple-choice questions from any article, notes, or study material.

📚 <b>Available Modes</b>

🇮🇳 <b>UPSC Mode</b>
• Generates statement-based questions in the UPSC Prelims style.

📝 <b>Quiz Mode</b>
• Generates standard four-option multiple-choice questions.

❓ Tap <b>Help</b> to learn how to use the bot and view the usage limits.
"""

#============
#HELP TEXT
#===========
HELP_TEXT = """
❓ <b>MCQ Bot Help</b>

Generate MCQs from your notes, study material or newspaper articles.

<b>📚 Modes</b>

<b>🇮🇳 UPSC Mode</b>
• Statement-based questions in the UPSC Prelims style.

<b>📝 Quiz Mode</b>
• Standard 4 options MCQs.

<b>⚙️ Generation Options</b>

<b>📄 Single Article</b>
• Generate <b>1–10 polls</b> from a single article/note.

<b>📦 Batch Mode</b>
• Queue up to <b>5 articles/notes</b> and generate MCQs together.

<b>⚠️ Limits</b>

• <b>10 articles for 1 user/day</b> (resets at midnight).
• Maximum <b>5 articles</b> per batch.

<b>💡 Tips</b>

• Use one topic per article for the best results.
• Remove unnecessary links or headers before sending.
• Split long documents into smaller sections for better-quality questions.

"""

# =========================
# GEMINI FUNCTION
# =========================

def generate_content(notes: str, mode: str = "upsc", count: int = 2) -> str:
    print(f">>> Calling Gemini API for mode: {mode}, count: {count}")
    
    # Dynamically build the expected structure format blocks based on count
    mcq_formats = []
    for i in range(1, count + 1):
        if mode == "upsc":
            mcq_formats.append(f"""
MCQ{i}:
Consider the following statements:
1. <Statement 1>

2. <Statement 2>

Which of the above is/are correct?

a. Statement 1 only
b. Statement 2 only
c. Both 1 and 2
d. Neither 1 nor 2 (or other appropriate option)

ANSWER: <correct option a/b/c/d>
""")
        else:
            mcq_formats.append(f"""
MCQ{i}:
<Clear, concise question based on the notes>

a. <Option A>
b. <Option B>
c. <Option C>
d. <Option D>

ANSWER: <correct option a/b/c/d>
""")

    format_str = "\n\n".join(mcq_formats)

    if mode == "upsc":
        prompt = f"""
You are an expert UPSC Prelims question setter.

From the given current affairs notes, generate exactly {count} multiple-choice questions in the STRICT format below.

Rules:
- No explanations
- No markdown
- No extra text outside format
- Keep options SHORT (max 12–15 words each)
- Focus on factual accuracy and elimination-based questions
- Questions must be UPSC Prelims level (statement-based, tricky but factual)

FORMAT:

TITLE: <short headline>

SUMMARY:
<Detailed concise summary from the article from UPSC POV. Don't make it too long>

{format_str}

IMPORTANT RULES:
- Generate EXACTLY {count} MCQs (from MCQ1 to MCQ{count}).
- Keep statements factual and UPSC-level tricky
- Avoid long sentences in options
- Do not repeat same answer pattern
- Ensure each MCQ block is clearly separated by a blank line.
IMPORTANT:
- The ENTIRE poll question (including statements and "Which of the above...") must be under 280 characters.
- Maximum 2 statements per question.
- Each statement under 60 characters.
{notes}
"""
    else:
        prompt = f"""
You are an expert quiz question setter.

From the given notes, generate exactly {count} multiple-choice questions in the STRICT format below.

Rules:
- No explanations
- No markdown
- No extra text outside format
- Keep question and options SHORT (options max 12–15 words each)
- Generate standard 4-option multiple choice questions based on the notes

FORMAT:

TITLE: <short headline>

SUMMARY:
<Detailed concise summary from the article. Don't make it too long>

{format_str}

IMPORTANT RULES:
- Generate EXACTLY {count} MCQs (from MCQ1 to MCQ{count}).
- Keep questions factual and clear
- Do not use statement-based logic (like "Consider the following statements" or "Statement 1 only")
- Ensure each MCQ block is clearly separated by a blank line.
- The entire question text (excluding options) must be under 280 characters.
{notes}
"""
    response = model.generate_content(prompt)
    return response.text or ""


# =========================
# COMMANDS
# =========================

async def getid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Chat ID: {update.effective_chat.id}")


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    today = datetime.now().strftime("%d %B %Y")

    msg = f"""
<b>📅 {today}</b>

📌 <b>Instructions:</b>
1️⃣ Attempt the MCQs first  
2️⃣ Try to answer without revealing the summary  
3️⃣ After completing, read the explanation carefully
4️⃣ Tap to reveal the news  

<tg-spoiler>💡 Tip: Treat this like a mini Prelims test. Accuracy matters more than speed.</tg-spoiler>

<b>⚡ Stay consistent. Improve daily.</b>
"""

    sent = await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=msg,
        parse_mode="HTML"
    )

    try:
        await context.bot.pin_chat_message(
            chat_id=GROUP_CHAT_ID,
            message_id=sent.message_id
        )
    except:
        pass


# =========================
# MCQ PARSER
# =========================

def parse_mcq(block: str):
    lines = block.split("\n")

    question_lines = []
    options = []
    answer = None

    reading_question = True

    for line in lines:
        line = line.strip()

        # detect options
        if line.lower().startswith("a.") or line.lower().startswith("a)"):
            reading_question = False
            options.append(("a", line.split(".", 1)[-1].strip()))

        elif line.lower().startswith("b.") or line.lower().startswith("b)"):
            options.append(("b", line.split(".", 1)[-1].strip()))

        elif line.lower().startswith("c.") or line.lower().startswith("c)"):
            options.append(("c", line.split(".", 1)[-1].strip()))

        elif line.lower().startswith("d.") or line.lower().startswith("d)"):
            options.append(("d", line.split(".", 1)[-1].strip()))

        elif "ANSWER" in line:
            answer = line.split(":")[-1].strip().lower()

        else:
            if reading_question:
                question_lines.append(line)

    question = "\n".join(question_lines).strip()

    return question, options, answer

# =========================
# MAIN HANDLER
# =========================
def keyboard_off():
    return ReplyKeyboardMarkup(
        [
            ["📦 Batch Mode: ON"],
            ["📢 Send Updates", "⚙️ Settings"],
            ["📅 Send daily update to group", "👤 User Mode"],
        ],
        resize_keyboard=True,
    )


def keyboard_on():
    return ReplyKeyboardMarkup(
        [
            ["📦 Batch Mode: OFF"],
            ["📢 Send Updates", "⚙️ Settings"],
            ["📅 Send daily update to group", "👤 User Mode"],
        ],
        resize_keyboard=True,
    )

def main_keyboard(is_owner_user_mode=False):
    """Permanent bottom keyboard."""
    buttons = [
        ["🇮🇳 UPSC Mode", "📝 Quiz Mode"],
        ["❓ Help", "💬 Feedback"]
    ]
    if is_owner_user_mode:
        buttons.append(["👑 God Mode"])
    return ReplyKeyboardMarkup(
        buttons,
        resize_keyboard=True
    )

def welcome_inline():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❓ Help", callback_data="help")]
    ])

def help_inline():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])

def submenu_inline(parent: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Batch Mode (Upto 5 articles)", callback_data=f"{parent}_batch")],
        [InlineKeyboardButton("📄 Single Article Mode (Upto 10 polls)", callback_data=f"{parent}_single")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])

def poll_count_inline(mode: str):
    keyboard = []
    # Create 5 rows with 2 options each (1 to 10)
    for i in range(1, 11, 2):
        keyboard.append([
            InlineKeyboardButton(f"{i} Poll", callback_data=f"pollcount_{mode}_{i}"),
            InlineKeyboardButton(f"{i+1} Polls", callback_data=f"pollcount_{mode}_{i+1}")
        ])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back_submenu")])
    return InlineKeyboardMarkup(keyboard)

def mode_keyboard():
    """Bottom keyboard shown after UPSC/Quiz Mode is selected."""
    return ReplyKeyboardMarkup(
        [
            ["📦 Batch Mode (Upto 5 articles)"],
            ["📄 Single Article Mode (Upto 10 polls)"],
            ["⬅️ Back"],
        ],
        resize_keyboard=True
    )

def batch_keyboard():
    """Persistent bottom keyboard during batch collection."""
    return ReplyKeyboardMarkup(
        [
            ["✅ Process Batch"],
            ["❌ Cancel"]
        ],
        resize_keyboard=True
    )

def feedback_keyboard():
    """Persistent keyboard during feedback entry."""
    return ReplyKeyboardMarkup(
        [
            ["❌ Cancel"]
        ],
        resize_keyboard=True
    )

def cancel_inline():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_all")]
    ])

def batch_inline():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Process Batch", callback_data="process_batch")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_all")]
    ])

def single_inline():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_all")]
    ])


def ensure_user(user_id: int):
    """Make sure all per-user dicts have sane defaults."""
    if user_id not in USER_MODE:
        USER_MODE[user_id] = "normal"

    if user_id not in USER_BATCHES:
        USER_BATCHES[user_id] = []

    if user_id not in USER_STATE:
        USER_STATE[user_id] = "main"

    if user_id not in USER_POLL_COUNT:
        USER_POLL_COUNT[user_id] = 2

    today = datetime.now().strftime("%Y-%m-%d")

    if user_id not in USER_DAILY_USAGE:
        USER_DAILY_USAGE[user_id] = {
            "date": today,
            "count": 0
        }

    if USER_DAILY_USAGE[user_id]["date"] != today:
        USER_DAILY_USAGE[user_id]["date"] = today
        USER_DAILY_USAGE[user_id]["count"] = 0


async def edit_menu(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str, markup, parse_mode=None):
    """Silently update the user's menu message in place. Falls back to a new message if the edit fails."""
    msg_id = MENU_MESSAGE_ID.get(user_id)

    if msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=user_id,
                message_id=msg_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=markup
            )
            return
        except Exception as e:
            print("Menu edit failed, sending new message instead:", e)

    sent = await context.bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=markup
    )
    MENU_MESSAGE_ID[user_id] = sent.message_id

async def handle_user(update: Update, context: ContextTypes.DEFAULT_TYPE):

    msg = update.message
    user_id = update.effective_user.id
    text = msg.text
    global USER_BATCHES, USER_MODE, USER_POLL_COUNT

    ensure_user(user_id)
    is_owner_user = (user_id == OWNER_ID and USER_STATE.get(user_id) == "owner_user_mode")

    # =========================
    # Feedback workflow
    # =========================
    if text == "💬 Feedback":
        USER_STATE[user_id] = "feedback"
        await msg.reply_text(
            "📝 Please type and send your feedback below. We really appreciate your suggestions!",
            reply_markup=feedback_keyboard()
        )
        return

    if USER_STATE.get(user_id) == "feedback":
        if text == "❌ Cancel":
            USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"
            await msg.reply_text(
                "Feedback cancelled.",
                reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
            )
            return

        feedback_entry = {
            "user_id": user_id,
            "username": update.effective_user.username,
            "first_name": update.effective_user.first_name,
            "feedback": text,
            "timestamp": datetime.now().isoformat()
        }
        feedback_col.insert_one(feedback_entry)
        
        if os.path.exists("feedback.json"):
            try:
                with open("feedback.json", "r") as f:
                    feedbacks = json.load(f)
            except Exception:
                pass
        feedbacks.append(feedback_entry)
        with open("feedback.json", "w") as f:
            json.dump(feedbacks, f, indent=4)

        USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"
        await msg.reply_text(
            "Thank you for your feedback!",
            reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
        )
        return

    # =========================
    # Bottom keyboard: Help
    # =========================
    # =========================
    # Bottom keyboard: Help
    # =========================
    if text == "❓ Help":
        USER_STATE[user_id] = "help"
        sent = await msg.reply_text(
            HELP_TEXT,
            parse_mode="HTML",
            reply_markup=help_inline()
        )
        MENU_MESSAGE_ID[user_id] = sent.message_id
        return

    # =========================
    # Bottom keyboard: UPSC / Quiz Mode
    # =========================
    if text == "🇮🇳 UPSC Mode":
        USER_STATE[user_id] = "upsc"
        await msg.reply_text(
            "🇮🇳 UPSC Mode selected\n\nChoose option:",
            reply_markup=mode_keyboard()
        )
        return

    if text == "📝 Quiz Mode":
        USER_STATE[user_id] = "quiz"
        await msg.reply_text(
            "📝 Quiz Mode selected\n\nChoose option:",
            reply_markup=mode_keyboard()
        )
        return

    # =========================
    # Bottom keyboard: Batch / Single / Back (shown after UPSC/Quiz Mode)
    # =========================
    if text == "📦 Batch Mode (Upto 5 articles)":
        state = USER_STATE.get(user_id, "")
        parent = "upsc" if "upsc" in state else "quiz"
        USER_STATE[user_id] = f"{parent}_batch"
        USER_MODE[user_id] = "batch"
        USER_BATCHES[user_id] = []
        await msg.reply_text(
            "✅ Batch Mode selected\n\nSend up to 5 articles, then tap '✅ Process Batch'.",
            reply_markup=batch_keyboard()
        )
        return

    if text == "📄 Single Article Mode (Upto 10 polls)":
        state = USER_STATE.get(user_id, "")
        parent = "upsc" if "upsc" in state else "quiz"
        USER_STATE[user_id] = f"{parent}_poll_select"
        await msg.reply_text(
            "🔢 How many polls (questions) would you like to generate? (Select from 1 to 10)",
            reply_markup=poll_count_inline(parent)
        )
        return

    if text == "⬅️ Back":
        USER_MODE[user_id] = "normal"
        USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"
        USER_BATCHES[user_id] = []
        await msg.reply_text(
            "Choose option:",
            reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
        )
        return

    # =========================
    # Collecting batch articles
    # =========================
    if USER_MODE[user_id] == "batch":
        if text == "❌ Cancel":
            USER_MODE[user_id] = "normal"
            USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"
            USER_BATCHES[user_id] = []
            await msg.reply_text("❌ Cancelled.", reply_markup=main_keyboard(is_owner_user_mode=is_owner_user))
            return

        if text == "✅ Process Batch":
            state = USER_STATE.get(user_id, "")
            parent = "upsc" if "upsc" in state else "quiz"
            count = len(USER_BATCHES.get(user_id, []))
            if count == 0:
                await msg.reply_text("❌ No articles were added yet.")
                return

            USER_MODE[user_id] = "normal"
            await msg.reply_text(f"🚀 Processing {count} article(s)...")

            for article in USER_BATCHES[user_id]:
                await process_user_article(article, context, user_id, mode=parent)

            USER_DAILY_USAGE[user_id]["count"] += count
            USER_BATCHES[user_id].clear()

            remaining = 10 - USER_DAILY_USAGE[user_id]["count"]
            USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"

            await msg.reply_text(
                f"✅ Batch completed!\n\n"
                f"📊 Today's usage: {USER_DAILY_USAGE[user_id]['count']}/10 articles\n"
                f"📌 Remaining today: {remaining}",
                reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
            )
            return

        if len(USER_BATCHES[user_id]) >= 5:
            USER_MODE[user_id] = "normal"
            USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"
            USER_BATCHES[user_id] = []
            await msg.reply_text(
                "❌ Maximum 5 articles allowed. Process cancelled.",
                reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
            )
            return

        if USER_DAILY_USAGE[user_id]["count"] >= 10:
            await msg.reply_text(
                "❌ Daily limit reached.\n\nYou can generate only 10 articles per day."
            )
            return

        USER_BATCHES[user_id].append(text)

        remaining = 10 - (
            USER_DAILY_USAGE[user_id]["count"] +
            len(USER_BATCHES[user_id])
        )

        await msg.reply_text(
            f"📥 Added to batch ({len(USER_BATCHES[user_id])}/5)\n"
            f"Daily articles remaining: {remaining}",
            reply_markup=batch_keyboard()
        )
        return

    # =========================
    # Single article submitted
    # =========================
    if USER_MODE[user_id] == "single":

        state = USER_STATE.get(user_id, "")
        parent = "upsc" if "upsc" in state else "quiz"

        if USER_DAILY_USAGE[user_id]["count"] >= 10:
            await msg.reply_text(
                "❌ Daily limit reached.\n\nYou can generate only 10 articles per day."
            )
            return

        await process_user_article(text, context, user_id, mode=parent)
        USER_DAILY_USAGE[user_id]["count"] += 1

        USER_MODE[user_id] = "normal"
        USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"

        remaining = 10 - USER_DAILY_USAGE[user_id]["count"]

        await msg.reply_text(
            f"✅ Done!\n📌 Remaining today: {remaining}\n\nChoose option:",
            reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
        )
        return

    await msg.reply_text("Use the buttons below 👇")


async def receive_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # Get the message (works for both new and edited messages)
    msg = update.message or update.edited_message
    if not msg or not msg.text:
        return
    
    user_id = update.effective_user.id
    ensure_user(user_id)

    is_owner_user = (user_id == OWNER_ID and USER_STATE.get(user_id) == "owner_user_mode")

# =========================
# USER MODE (or Owner in User Mode)
# =========================
    if user_id != OWNER_ID or is_owner_user:
        if user_id == OWNER_ID and msg.text == "👑 God Mode":
            USER_STATE[OWNER_ID] = "main"
            await msg.reply_text(
                "Welcome back to Owner Mode!",
                reply_markup=keyboard_off()
            )
            return
        await handle_user(update, context)
        return

    # =========================
    # OWNER MODE
    # =========================

    # Ignore messages sent by bots (including this bot)
    if update.effective_user.is_bot:
        return

    global BATCH_MODE, OWNER_QUEUE, USER_BATCHES, USER_MODE

    if msg.text == "👤 User Mode":
        USER_STATE[OWNER_ID] = "owner_user_mode"
        await msg.reply_text(
            "Switched to User Mode. You can now use the bot like a normal user.",
            reply_markup=main_keyboard(is_owner_user_mode=True)
        )
        return

    if msg.text == "📅 Send daily update to group":
        await begin(update, context)
        await msg.reply_text("✅ Daily update sent to the group.")
        return

    if msg.text == "📢 Send Updates":

        for user in KNOWN_USERS.values():

            if user["id"] == OWNER_ID:
                continue

            try:
                await context.bot.send_message(
                    chat_id=user["id"],
                    text=(
                        "🎉 Bot Updated!\n\n"
                        "The bot has new features and your keyboard has been refreshed."
                    ),
                    reply_markup=main_keyboard()
                )
            except Exception as e:
                print(f"Couldn't send update to {user['id']}: {e}")

        await msg.reply_text("✅ Updates sent to all users.")

        return

    # Toggle Batch Mode
    if msg.text == "📦 Batch Mode: ON":
        BATCH_MODE = True
        OWNER_QUEUE.clear()

        await msg.reply_text(
            "Batch Mode ON\n\nSend your articles.",
            reply_markup=keyboard_on()
        )
        return

    if msg.text == "📦 Batch Mode: OFF":
        BATCH_MODE = False

        if not OWNER_QUEUE:
            await msg.reply_text(
                "No articles were added.\n\nBatch mode is now OFF.",
                reply_markup=keyboard_off()
            )
            return
        await msg.reply_text(
            f"🚀 Processing {len(OWNER_QUEUE)} article(s)...",

        )

        for article in OWNER_QUEUE:
            await process_article(article, context)

        OWNER_QUEUE.clear()

        await msg.reply_text(
            "Batch completed!",
            reply_markup=keyboard_off()
        )

        return

    # Don't process articles unless Batch Mode is ON
    if not BATCH_MODE:
        await msg.reply_text(
            "❌ Batch Mode is OFF.\nPress '📦 Batch Mode: ON' first."
        )
        return
    OWNER_QUEUE.append(msg.text)

    await msg.reply_text(
        f"📥 Added to batch ({len(OWNER_QUEUE)} article(s))"
    )

    return


from telegram import ReplyKeyboardMarkup

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    USER_STATE[user_id] = "main"   # ✅ correct here

    user = update.effective_user

    KNOWN_USERS[user.id] = {
        "id": user.id,
        "username": user.username,
        "first_name": user.first_name
    }

    save_users()

    if user.id == OWNER_ID:
        await update.message.reply_text(
            "Welcome!",
            reply_markup=keyboard_off()
        )
        return

    sent = await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode="HTML",
        reply_markup=welcome_inline()
    )
    MENU_MESSAGE_ID[user_id] = sent.message_id

    await update.message.reply_text(
        "👇 Use the buttons below to choose a mode.",
        reply_markup=main_keyboard()
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    ensure_user(user_id)
    MENU_MESSAGE_ID[user_id] = query.message.message_id
    is_owner_user = (user_id == OWNER_ID and USER_STATE.get(user_id) == "owner_user_mode")

    # =========================
    # Help / Back to welcome
    # =========================
    if data == "help":
        USER_STATE[user_id] = "help"
        await query.edit_message_text(
            HELP_TEXT,
            parse_mode="HTML",
            reply_markup=help_inline()
        )
        return

    if data == "back_main":
        USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"
        USER_MODE[user_id] = "normal"
        await query.edit_message_text(
            WELCOME_TEXT,
            parse_mode="HTML",
            reply_markup=welcome_inline()
        )
        return

    # =========================
    # UPSC / Quiz Mode chosen via inline button (e.g. from a future entry point)
    # =========================
    if data in ("upsc", "quiz"):
        USER_STATE[user_id] = f"owner_user_mode_{data}" if is_owner_user else data
        label = "🇮🇳 UPSC Mode" if data == "upsc" else "📝 Quiz Mode"
        await query.edit_message_text(
            f"{label} selected\n\nChoose option:",
            reply_markup=submenu_inline(data)
        )
        return

    # =========================
    # Back to the UPSC/Quiz submenu
    # =========================
    if data == "back_submenu":
        state = USER_STATE.get(user_id, "")
        parent = "upsc" if "upsc" in state else "quiz"
        label = "🇮🇳 UPSC Mode" if parent == "upsc" else "📝 Quiz Mode"
        USER_STATE[user_id] = f"owner_user_mode_{parent}" if is_owner_user else parent
        USER_MODE[user_id] = "normal"
        await query.edit_message_text(
            f"{label} selected\n\nChoose option:",
            reply_markup=submenu_inline(parent)
        )
        return

    # =========================
    # Batch Mode selected
    # =========================
    if data in ("upsc_batch", "quiz_batch"):
        parent = data.split("_")[0]
        USER_STATE[user_id] = f"{parent}_batch"
        USER_MODE[user_id] = "batch"
        USER_BATCHES[user_id] = []
        await query.edit_message_text(
            "✅ Batch Mode ON\n\nSend up to 5 articles, then tap '✅ Process Batch'."
        )
        await context.bot.send_message(
            chat_id=user_id,
            text="Use the buttons below to control the batch:",
            reply_markup=batch_keyboard()
        )
        return

    # =========================
    # Single Article Mode (Upto 10 polls) selected
    # =========================
    if data in ("upsc_single", "quiz_single"):
        parent = data.split("_")[0]
        USER_STATE[user_id] = f"{parent}_poll_select"
        await query.edit_message_text(
            "🔢 How many polls (questions) would you like to generate? (Select from 1 to 10)",
            reply_markup=poll_count_inline(parent)
        )
        return

    # =========================
    # Poll Count chosen (e.g. pollcount_upsc_5)
    # =========================
    if data.startswith("pollcount_"):
        parts = data.split("_")
        parent = parts[1]
        count = int(parts[2])

        USER_POLL_COUNT[user_id] = count
        USER_STATE[user_id] = f"{parent}_single"
        USER_MODE[user_id] = "single"

        await query.edit_message_text(
            f"📄 Single Article Mode (Upto 10 polls) ({count} polls selected).\n\nSend one article to generate {count} questions.",
            reply_markup=single_inline()
        )
        return

    # =========================
    # Process the batch
    # =========================
    if data == "process_batch":
        state = USER_STATE.get(user_id, "")
        parent = "upsc" if "upsc" in state else "quiz"

        count = len(USER_BATCHES.get(user_id, []))

        if count == 0:
            await query.answer("❌ No articles were added yet.", show_alert=True)
            return

        USER_MODE[user_id] = "normal"

        await context.bot.send_message(
            chat_id=user_id,
            text=f"🚀 Processing {count} article(s)..."
        )

        for article in USER_BATCHES[user_id]:
            await process_user_article(article, context, user_id, mode=parent)

        USER_DAILY_USAGE[user_id]["count"] += count
        USER_BATCHES[user_id].clear()

        remaining = 10 - USER_DAILY_USAGE[user_id]["count"]
        USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"

        await query.edit_message_text(f"✅ Batch completed!")

        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"📊 Today's usage: {USER_DAILY_USAGE[user_id]['count']}/10 articles\n"
                f"📌 Remaining today: {remaining}\n\n"
                f"Choose option:"
            ),
            reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
        )
        return

    # =========================
    # Cancel everything
    # =========================
    if data == "cancel_all":
        USER_MODE[user_id] = "normal"
        USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"
        USER_BATCHES[user_id] = []

        await query.edit_message_text("❌ Cancelled.")

        await context.bot.send_message(
            chat_id=user_id,
            text="Choose option:",
            reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
        )
        return


async def process_article(notes: str, context: ContextTypes.DEFAULT_TYPE):
    processing = await context.bot.send_message(
        chat_id=OWNER_ID,
        text="Processing..."
    )
    try:
        # Default Owner group updates to 2 UPSC questions
        result = await asyncio.to_thread(generate_content, notes, "upsc", 2)

        # =========================
        # SPLIT OUTPUT
        # =========================

        result = result.replace("\r", "")

        title = ""
        summary = ""
        mcq1_block = ""
        mcq2_block = ""

        try:
            if "TITLE:" in result and "SUMMARY:" in result:
                title = result.split("TITLE:", 1)[1].split("SUMMARY:", 1)[0].strip()
                rest = result.split("SUMMARY:", 1)[1]
            else:
                rest = result

            if "MCQ1:" in rest:
                summary = rest.split("MCQ1:", 1)[0].strip()
                mcqs = rest.split("MCQ1:", 1)[1]

                if "MCQ2:" in mcqs:
                    mcq1_block = mcqs.split("MCQ2:", 1)[0].strip()
                    mcq2_block = mcqs.split("MCQ2:", 1)[1].strip()
                else:
                    mcq1_block = mcqs.strip()

        except Exception as e:
            print("Parsing error:", e)
            print("RAW OUTPUT:\n", result)

        # =========================
        # SEND SUMMARY
        # =========================

        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=f"📰 <b>The news:</b> {title}\n\n<tg-spoiler>{summary}</tg-spoiler>",
            parse_mode="HTML"
        )

        # =========================
        # MCQ 1
        # =========================

        q1, opt1, ans1 = parse_mcq(mcq1_block)

        # Telegram limit
        q1 = q1[:300]

        options_text = [o[1][:100] for o in opt1]

        correct_index = next((i for i, o in enumerate(opt1) if o[0] == ans1), 0)

        await context.bot.send_poll(
            chat_id=GROUP_CHAT_ID,
            question=q1,
            options=options_text,
            type="quiz",
            is_anonymous=False,
            correct_option_id=correct_index
        )

        # =========================
        # MCQ 2
        # =========================

        q2, opt2, ans2 = parse_mcq(mcq2_block)

        # Telegram limit
        q2 = q2[:300]

        options_text2 = [o[1][:100] for o in opt2]

        correct_index2 = next((i for i, o in enumerate(opt2) if o[0] == ans2), 0)

        await context.bot.send_poll(
            chat_id=GROUP_CHAT_ID,
            question=q2,
            options=options_text2,
            type="quiz",
            is_anonymous=False,
            correct_option_id=correct_index2
        )

        await processing.edit_text("✅ Done")

    except Exception as e:
        await processing.edit_text(f"❌ Error: {e}")

#===========================
#Article processing for user
#===========================
async def process_user_article(notes: str, context: ContextTypes.DEFAULT_TYPE, user_id: int, mode: str = "upsc"):
    await context.bot.send_message(
        chat_id=user_id,
        text="⏳ Processing article..."
    )

    ensure_user(user_id)
    count = USER_POLL_COUNT.get(user_id, 2)

    # Generate with Gemini using specific mode and dynamic count
    result = await asyncio.to_thread(generate_content, notes, mode, count)

    result = result.replace("\r", "")

    if "SUMMARY:" in result:
        rest = result.split("SUMMARY:", 1)[1]
    else:
        rest = result

    # Robust regex splitting by "MCQ\d+:"
    blocks = re.split(r'(?i)MCQ\d+\s*:', rest)
    mcq_blocks = blocks[1:][:count]  # Filter the generated blocks up to the selected count

    sent_any = False
    for idx, block in enumerate(mcq_blocks):
        q, opt, ans = parse_mcq(block)
        
        if not q or not opt:
            continue

        await context.bot.send_poll(
            chat_id=user_id,
            question=q[:300],
            options=[o[1][:100] for o in opt],
            type="quiz",
            is_anonymous=False,
            correct_option_id=next((i for i, o in enumerate(opt) if o[0] == ans), 0)
        )
        sent_any = True

    if not sent_any:
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Could not parse any questions. Please try again with simpler text."
        )

#===============================
#JSON FILE for storing user data
#==============================
def load_users():
    global KNOWN_USERS
    KNOWN_USERS = {}
    for user in users_col.find():
        KNOWN_USERS[user["id"]] = user


def save_users():
    for user in KNOWN_USERS.values():
        users_col.update_one(
            {"id": user["id"]},
            {"$set": user},
            upsert=True
        )

# =========================
# MAIN
# =========================

def main():

    load_users()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("getid", getid))
    app.add_handler(CommandHandler("begin", begin))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_notes)
)
    app.add_handler(CommandHandler("start", start))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
