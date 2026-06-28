import asyncio
import json
import os
import re
from datetime import datetime
from dotenv import load_dotenv
import httpx  # Added for calling the Grok API

load_dotenv()

from pymongo import MongoClient

# MongoDB Connection
MONGO_URI = os.getenv("MONGODB_URI")
client = MongoClient(MONGO_URI)
db = client["mcqbot"]
users_col = db["users"]
feedback_col = db["feedbacks"]

# Import the new google-genai library
from google import genai
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
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
GROQ_API_KEY = os.getenv("GROQ_API_KEY")  # rename for clarity
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")  # check console.groq.com for current models
OWNER_ID = 5081349515
GROUP_CHAT_ID = os.getenv("CHAT_ID")
BATCH_MODE = False
OWNER_QUEUE = []
USER_BATCHES = {}
USER_STATE = {}
USER_MODE = {}
USER_DAILY_USAGE = {}
KNOWN_USERS = {}
MENU_MESSAGE_ID = {}
USER_POLL_COUNT = {}

# Set up the new GenAI Client. 
# We use 'ai_client' because 'client' is already used above for MongoClient.
ai_client = genai.Client(api_key=GEMINI_API_KEY)

#=============
#WELCOME TEXT
#=============
WELCOME_TEXT = """👋 <b>Welcome to the MCQ Bot!</b>

Provide any <b>topic</b>, <b>article</b>, <b>notes</b>, or <b>study material</b>, and I'll generate an MCQ test for you.

📚 <b>Available Modes</b>

📋 <b>Statement Mode</b>
• Generates statement-based MCQs in the UPSC Prelims style.

📝 <b>Quiz Mode</b>
• Basic 4 options quiz based MCQs.

❓ Tap <b>Help</b> to learn how to use the bot and view the usage limits.
"""

#============
#HELP TEXT
#===========
HELP_TEXT = """
❓ <b>MCQ Bot Help</b>

This bot can generate MCQs from any <b>topic</b>, <b>notes</b>, <b>study material</b> or <b>newspaper article</b> that you provide.

<b>📚 Modes</b>

<b>📋 Statement Mode</b>
• This will generate statement-based MCQs in the UPSC Prelims style.

<b>📝 Quiz Mode</b>
• Standard 4 options MCQs.

<b>⚙️ Generation Options</b>

<b>📄 Single Topic</b>
• Generate <b>1–10 MCQs</b> from a single topic/article/note that you provide.

<b>📦 Multiple Topics</b>
• Send up to <b>5 topics/articles/notes</b> and generate 5-10 MCQs at once.

<b>⚠️ Limits</b>

• <b>10 topics/notes/articles for 1 user/day</b> (resets at midnight).
• Maximum <b>5 topics/notes/articles</b> at once.

<b>💡 Tips</b>

• Use one topic per article for the best results.
• Remove unnecessary links or headers before sending.
• Split long documents into smaller sections for better-quality questions.

"""

# =========================
# CONTENT GENERATOR
# =========================

def generate_content(notes: str, mode: str = "upsc", count: int = 2) -> str:
    print(f">>> Preparing prompt for mode: {mode}, count: {count}")
    
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

    # --- Step 1: Try Grok API ---
    if GROQ_API_KEY:
        try:
            print(f">>> Attempting Groq API Call using model: {GROQ_MODEL}...")
            headers = {
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "user", "content": prompt}
                ]
            }

            with httpx.Client(timeout=60.0) as http_client:
                response = http_client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers, json=payload
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                if content:
                    print(">>> Groq API completed successfully.")
                    return content
        except Exception as groq_error:
            print(f">>> Groq API failed (Limit reached or error): {groq_error}")
            print(">>> Fallback: Switching to Gemini API...")
    else:
        print(">>> GROQ_API_KEY is not defined. Defaulting directly to Gemini...")

    # --- Step 2: Fallback to Gemini API if Grok failed or is unavailable ---
    response = ai_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
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


async def send_sunday_update_func(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return

    msg = (
        "📢 <b>Sunday Update</b>\n\n"
        "Today is <b>Sunday</b>, so there will be <b>no questions or quizzes</b> today.\n\n"
        "Take the day to relax, revise what you've already learned, or simply enjoy your weekend.\n\n"
        "New questions will resume tomorrow. Have a great Sunday! 🌿"
    )

    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=msg,
        parse_mode="HTML"
    )


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
# MAIN KEYBOARD HANDLERS
# =========================
def keyboard_off():
    return ReplyKeyboardMarkup(
        [
            ["📦 Batch Mode: ON"],
            ["📢 Updates", "⚙️ Settings"],
            ["👤 User Mode"],
        ],
        resize_keyboard=True,
    )


def keyboard_on():
    return ReplyKeyboardMarkup(
        [
            ["📦 Batch Mode: OFF"],
            ["📢 Updates", "⚙️ Settings"],
            ["👤 User Mode"],
        ],
        resize_keyboard=True,
    )

def updates_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📢 Send Updates", "📅 Send daily update to group"],
            ["Send sunday update", "✍️ Custom updates"],
            ["⬅️ Back"]
        ],
        resize_keyboard=True,
    )

def main_keyboard(is_owner_user_mode=False):
    buttons = [
        ["📋 Statement Mode", "📝 Quiz Mode"],
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
        [InlineKeyboardButton("📦 Multiple Topics (Upto 5 topics)", callback_data=f"{parent}_batch")],
        [InlineKeyboardButton("📄 Single Topic", callback_data=f"{parent}_single")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_main")]
    ])

def poll_count_inline(mode: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("5", callback_data=f"pollcount_{mode}_5"),
            InlineKeyboardButton("10", callback_data=f"pollcount_{mode}_10")
        ]
    ])

def mode_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📦 Multiple Topics (Upto 5 topics)"],
            ["📄 Single Topic"],
            ["⬅️ Back"],
        ],
        resize_keyboard=True
    )

def batch_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["✅ Process Batch"],
            ["❌ Cancel"]
        ],
        resize_keyboard=True
    )

def single_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["❌ Cancel"]
        ],
        resize_keyboard=True
    )

def feedback_keyboard():
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
        
        # Saves exclusively to MongoDB Atlas (No local file writing to avoid data loss on container reset)
        feedback_col.insert_one(feedback_entry)

        USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"
        await msg.reply_text(
            "Thank you for your feedback!",
            reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
        )
        return

    # =========================
    # Global Cancel Handler (for Single/Batch/Poll-Select/etc.)
    # =========================
    if text == "❌ Cancel":
        USER_MODE[user_id] = "normal"
        USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"
        USER_BATCHES[user_id] = []
        await msg.reply_text(
            "❌ Cancelled.",
            reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
        )
        return

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
    if text == "📋 Statement Mode":
        USER_STATE[user_id] = "upsc"
        await msg.reply_text(
            "📋 Statement Mode selected\n\nChoose option:",
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
    # Bottom keyboard options
    # =========================
    if text == "📦 Multiple Topics (Upto 5 topics)":
        state = USER_STATE.get(user_id, "")
        parent = "upsc" if "upsc" in state else "quiz"
        USER_STATE[user_id] = f"{parent}_batch"
        USER_MODE[user_id] = "batch"
        USER_BATCHES[user_id] = []
        await msg.reply_text(
            "✅ Multiple Topics selected\n\nSend up to 5 topics/notes/articles, then tap '✅ Process Batch'.",
            reply_markup=batch_keyboard()
        )
        return

    if text == "📄 Single Topic":
        state = USER_STATE.get(user_id, "")
        parent = "upsc" if "upsc" in state else "quiz"
        USER_STATE[user_id] = f"{parent}_poll_select"
        await msg.reply_text(
            "📄 Single Topic Mode",
            reply_markup=single_keyboard()
        )
        await msg.reply_text(
            "🔢 How many MCQs would you like to generate?",
            reply_markup=poll_count_inline(parent)
        )
        return

    if text == "⬅️ Back" and USER_MODE[user_id] != "single":
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
            await msg.reply_text("❌ Daily limit reached.\n\nYou can generate only 10 articles per day.")
            return

        USER_BATCHES[user_id].append(text)

        remaining = 10 - (USER_DAILY_USAGE[user_id]["count"] + len(USER_BATCHES[user_id]))

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

        if text == "❌ Cancel":
            USER_MODE[user_id] = "normal"
            USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"
            await msg.reply_text(
                "❌ Cancelled.",
                reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
            )
            return

        if text == "⬅️ Back":
            USER_MODE[user_id] = "normal"
            USER_STATE[user_id] = f"owner_user_mode_{parent}" if is_owner_user else parent
            await msg.reply_text(
                "Returned to selection:",
                reply_markup=mode_keyboard()
            )
            return

        if USER_DAILY_USAGE[user_id]["count"] >= 10:
            await msg.reply_text("❌ Daily limit reached.\n\nYou can generate only 10 articles per day.")
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
    msg = update.message or update.edited_message
    if not msg or not msg.text:
        return
    
    user_id = update.effective_user.id
    ensure_user(user_id)

    is_owner_user = (user_id == OWNER_ID and USER_STATE.get(user_id) == "owner_user_mode")

    if user_id != OWNER_ID or is_owner_user:
        if user_id == OWNER_ID and msg.text == "👑 God Mode":
            USER_STATE[OWNER_ID] = "main"
            await msg.reply_text("Welcome back to Owner Mode!", reply_markup=keyboard_off())
            return
        await handle_user(update, context)
        return

    if update.effective_user.is_bot:
        return

    global BATCH_MODE, OWNER_QUEUE, USER_BATCHES, USER_MODE

    # =========================
    # Owner Custom Updates Handler
    # =========================
    if USER_STATE.get(OWNER_ID) == "owner_custom_update":
        if msg.text == "❌ Cancel":
            USER_STATE[OWNER_ID] = "main"
            await msg.reply_text("Custom update cancelled.", reply_markup=updates_keyboard())
            return

        broadcast_text = msg.text
        success_count = 0
        fail_count = 0

        # Broadcast custom update to all known users with a forced layout reset sequence
        for user in KNOWN_USERS.values():
            if user["id"] == OWNER_ID:
                continue
            try:
                # 1. Force remove old cached keyboard
                temp_msg = await context.bot.send_message(
                    chat_id=user["id"],
                    text="🔄 Updating menu layout...",
                    reply_markup=ReplyKeyboardRemove()
                )
                # 2. Deliver custom update text and push the new keyboard markup
                await context.bot.send_message(
                    chat_id=user["id"],
                    text=broadcast_text,
                    parse_mode="HTML",
                    reply_markup=main_keyboard()
                )
                # 3. Silently delete the temporary status message to keep the chat tidy
                try:
                    await context.bot.delete_message(chat_id=user["id"], message_id=temp_msg.message_id)
                except Exception as e:
                    print(f"Failed to delete custom broadcast temp message for {user['id']}: {e}")
                
                success_count += 1
            except Exception as e:
                print(f"Couldn't send custom update to {user['id']}: {e}")
                fail_count += 1

        USER_STATE[OWNER_ID] = "main"
        await msg.reply_text(
            f"✅ Broadcast completed!\n\n"
            f"Sent successfully to: {success_count} users\n"
            f"Failed: {fail_count} users",
            reply_markup=updates_keyboard()
        )
        return

    # =========================
    # Owner Menu Actions
    # =========================
    if msg.text == "👤 User Mode":
        USER_STATE[OWNER_ID] = "owner_user_mode"
        await msg.reply_text(
            "Switched to User Mode. You can now use the bot like a normal user.",
            reply_markup=main_keyboard(is_owner_user_mode=True)
        )
        return

    if msg.text in ("📢 Updates", "Updates"):
        await msg.reply_text("Choose an update action:", reply_markup=updates_keyboard())
        return

    if msg.text == "⬅️ Back":
        await msg.reply_text("Returned to Owner Panel.", reply_markup=keyboard_on() if BATCH_MODE else keyboard_off())
        return

    if msg.text == "📅 Send daily update to group":
        await begin(update, context)
        await msg.reply_text("✅ Daily update sent to the group.")
        return

    if msg.text == "Send sunday update":
        await send_sunday_update_func(update, context)
        await msg.reply_text("✅ Sunday update sent to the group.")
        return

    if msg.text == "📢 Send Updates":
        for user in KNOWN_USERS.values():
            if user["id"] == OWNER_ID:
                continue
            try:
                # 1. Force remove old cached keyboard
                temp_msg = await context.bot.send_message(
                    chat_id=user["id"],
                    text="🔄 Updating menu layout...",
                    reply_markup=ReplyKeyboardRemove()
                )
                # 2. Push notification with the updated keyboard layout
                await context.bot.send_message(
                    chat_id=user["id"],
                    text=(
                        "🎉 Bot Updated!\n\n"
                        "The bot has new features and your keyboard has been refreshed."
                    ),
                    reply_markup=main_keyboard()
                )
                # 3. Silently delete the temporary status message to keep the chat tidy
                try:
                    await context.bot.delete_message(chat_id=user["id"], message_id=temp_msg.message_id)
                except Exception as e:
                    print(f"Failed to delete broadcast temp message for {user['id']}: {e}")
            except Exception as e:
                print(f"Couldn't send update to {user['id']}: {e}")

        await msg.reply_text("✅ Updates sent to all users.")
        return

    if msg.text in ("Custom updates", "✍️ Custom updates"):
        USER_STATE[OWNER_ID] = "owner_custom_update"
        await msg.reply_text(
            "Please send the custom message you want to broadcast to all users.\n\nType ❌ Cancel to cancel.",
            reply_markup=feedback_keyboard()
        )
        return

    if msg.text == "📦 Batch Mode: ON":
        BATCH_MODE = True
        OWNER_QUEUE.clear()
        await msg.reply_text("Batch Mode ON\n\nSend your articles.", reply_markup=keyboard_on())
        return

    if msg.text == "📦 Batch Mode: OFF":
        BATCH_MODE = False
        if not OWNER_QUEUE:
            await msg.reply_text("No articles were added.\n\nBatch mode is now OFF.", reply_markup=keyboard_off())
            return
        await msg.reply_text(f"🚀 Processing {len(OWNER_QUEUE)} article(s)...")

        for article in OWNER_QUEUE:
            await process_article(article, context)

        OWNER_QUEUE.clear()
        await msg.reply_text("Batch completed!", reply_markup=keyboard_off())
        return

    if not BATCH_MODE:
        await msg.reply_text("❌ Batch Mode is OFF.\nPress '📦 Batch Mode: ON' first.")
        return
        
    OWNER_QUEUE.append(msg.text)
    await msg.reply_text(f"📥 Added to batch ({len(OWNER_QUEUE)} article(s))")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    USER_STATE[user_id] = "main"

    user = update.effective_user
    KNOWN_USERS[user.id] = {
        "id": user.id,
        "username": user.username,
        "first_name": user.first_name
    }
    save_users()

    if user.id == OWNER_ID:
        await update.message.reply_text("Welcome!", reply_markup=keyboard_off())
        return

    # 1. Force Telegram client to discard old keyboard cache on user's device
    temp_msg = await update.message.reply_text(
        "🔄 Syncing main menu interface...",
        reply_markup=ReplyKeyboardRemove()
    )

    # 2. Render welcome text and draw fresh, updated keyboard buttons
    sent = await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )
    MENU_MESSAGE_ID[user_id] = sent.message_id

    # 3. Silently delete the temporary status message to keep the chat history tidy
    try:
        await context.bot.delete_message(chat_id=user_id, message_id=temp_msg.message_id)
    except Exception as e:
        print("Failed to delete temp message in start:", e)

    


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    ensure_user(user_id)
    MENU_MESSAGE_ID[user_id] = query.message.message_id
    is_owner_user = (user_id == OWNER_ID and USER_STATE.get(user_id) == "owner_user_mode")

    if data == "help":
        USER_STATE[user_id] = "help"
        await query.edit_message_text(HELP_TEXT, parse_mode="HTML", reply_markup=help_inline())
        return

    if data == "back_main":
        USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"
        USER_MODE[user_id] = "normal"
        await query.edit_message_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=welcome_inline())
        return

    if data in ("upsc", "quiz"):
        USER_STATE[user_id] = f"owner_user_mode_{data}" if is_owner_user else data
        label = "📋 Statement Mode" if data == "upsc" else "📝 Quiz Mode"
        await query.edit_message_text(f"{label} selected\n\nChoose option:", reply_markup=submenu_inline(data))
        return

    if data == "back_submenu":
        state = USER_STATE.get(user_id, "")
        parent = "upsc" if "upsc" in state else "quiz"
        label = "📋 Statement Mode" if parent == "upsc" else "📝 Quiz Mode"
        USER_STATE[user_id] = f"owner_user_mode_{parent}" if is_owner_user else parent
        USER_MODE[user_id] = "normal"
        await query.edit_message_text(f"{label} selected\n\nChoose option:", reply_markup=submenu_inline(parent))
        return

    if data in ("upsc_batch", "quiz_batch"):
        parent = data.split("_")[0]
        USER_STATE[user_id] = f"{parent}_batch"
        USER_MODE[user_id] = "batch"
        USER_BATCHES[user_id] = []
        await query.edit_message_text("✅ Batch Mode ON\n\nSend up to 5 articles, then tap '✅ Process Batch'.")
        await context.bot.send_message(
            chat_id=user_id,
            text="Use the buttons below to control the batch:",
            reply_markup=batch_keyboard()
        )
        return

    if data in ("upsc_single", "quiz_single"):
        parent = data.split("_")[0]
        USER_STATE[user_id] = f"{parent}_poll_select"
        await query.edit_message_text(
            "🔢 How many MCQs would you like to generate?",
            reply_markup=poll_count_inline(parent)
        )
        # Update reply keyboard to only have Cancel button
        await context.bot.send_message(
            chat_id=user_id,
            text="📄 Single Topic Mode selected.",
            reply_markup=single_keyboard()
        )
        return

    if data.startswith("pollcount_"):
        parts = data.split("_")
        parent = parts[1]
        count = int(parts[2])

        USER_POLL_COUNT[user_id] = count
        USER_STATE[user_id] = f"{parent}_single"
        USER_MODE[user_id] = "single"

        await query.edit_message_text(
            f"📄 Single Topic ({count} polls selected).\n\nSend one article to generate {count} questions.",
            reply_markup=single_inline()
        )
        await context.bot.send_message(
            chat_id=user_id,
            text="Please send your article/topic now. Use the button below to cancel.",
            reply_markup=single_keyboard()
        )
        return

    if data == "process_batch":
        state = USER_STATE.get(user_id, "")
        parent = "upsc" if "upsc" in state else "quiz"
        count = len(USER_BATCHES.get(user_id, []))

        if count == 0:
            await query.answer("❌ No articles were added yet.", show_alert=True)
            return

        USER_MODE[user_id] = "normal"
        await context.bot.send_message(chat_id=user_id, text=f"🚀 Processing {count} article(s)...")

        for article in USER_BATCHES[user_id]:
            await process_user_article(article, context, user_id, mode=parent)

        USER_DAILY_USAGE[user_id]["count"] += count
        USER_BATCHES[user_id].clear()

        remaining = 10 - USER_DAILY_USAGE[user_id]["count"]
        USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"

        await query.edit_message_text(f"✅ Batch completed!")
        await context.bot.send_message(
            chat_id=user_id,
            text=f"📊 Today's usage: {USER_DAILY_USAGE[user_id]['count']}/10 articles\n📌 Remaining today: {remaining}\n\nChoose option:",
            reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
        )
        return

    if data == "cancel_all":
        USER_MODE[user_id] = "normal"
        USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"
        USER_BATCHES[user_id] = []
        await query.edit_message_text("❌ Cancelled.")
        await context.bot.send_message(chat_id=user_id, text="Choose option:", reply_markup=main_keyboard(is_owner_user_mode=is_owner_user))
        return


async def process_article(notes: str, context: ContextTypes.DEFAULT_TYPE):
    processing = await context.bot.send_message(chat_id=OWNER_ID, text="Processing...")
    try:
        result = await asyncio.to_thread(generate_content, notes, "upsc", 2)
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

        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=f"📰 <b>The news:</b> {title}\n\n<tg-spoiler>{summary}</tg-spoiler>",
            parse_mode="HTML"
        )

        # MCQ 1
        q1, opt1, ans1 = parse_mcq(mcq1_block)
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

        # MCQ 2
        q2, opt2, ans2 = parse_mcq(mcq2_block)
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


async def process_user_article(notes: str, context: ContextTypes.DEFAULT_TYPE, user_id: int, mode: str = "upsc"):
    await context.bot.send_message(chat_id=user_id, text="⏳ Processing article...")
    ensure_user(user_id)
    count = USER_POLL_COUNT.get(user_id, 2)

    result = await asyncio.to_thread(generate_content, notes, mode, count)
    result = result.replace("\r", "")

    if "SUMMARY:" in result:
        rest = result.split("SUMMARY:", 1)[1]
    else:
        rest = result

    blocks = re.split(r'(?i)MCQ\d+\s*:', rest)
    mcq_blocks = blocks[1:][:count]

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
        await context.bot.send_message(chat_id=user_id, text="❌ Could not parse any questions. Please try again with simpler text.")


#===========================
# JSON MIGRATION & USER SYNC
#===========================
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


def migrate_json_data():
    """Migrates users.json and feedbacks.json to MongoDB Atlas on startup if they exist."""
    # 1. Migrate users.json
    if os.path.exists("users.json"):
        print("[MIGRATION] Found users.json. Syncing with MongoDB...")
        try:
            with open("users.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            
            count = 0
            if isinstance(data, dict):
                for uid_str, user_details in data.items():
                    try:
                        uid = int(uid_str)
                        user_details["id"] = uid
                        users_col.update_one({"id": uid}, {"$set": user_details}, upsert=True)
                        count += 1
                    except ValueError:
                        pass
            elif isinstance(data, list):
                for user_details in data:
                    uid = user_details.get("id")
                    if uid:
                        users_col.update_one({"id": int(uid)}, {"$set": user_details}, upsert=True)
                        count += 1
            print(f"[MIGRATION] Successfully moved {count} users to MongoDB!")
            os.rename("users.json", "users.json.migrated") # Prevent re-running next startup
        except Exception as e:
            print(f"[MIGRATION ERROR] Failed to migrate users.json: {e}")

    # 2. Migrate feedbacks.json (or feedback.json)
    for fb_file in ["feedbacks.json", "feedback.json"]:
        if os.path.exists(fb_file):
            print(f"[MIGRATION] Found {fb_file}. Syncing with MongoDB...")
            try:
                with open(fb_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                count = 0
                if isinstance(data, list):
                    for fb_entry in data:
                        # Prevent duplicate entry creation
                        exists = feedback_col.find_one({
                            "user_id": fb_entry.get("user_id"),
                            "feedback": fb_entry.get("feedback"),
                            "timestamp": fb_entry.get("timestamp")
                        })
                        if not exists:
                            feedback_col.insert_one(fb_entry)
                            count += 1
                print(f"[MIGRATION] Successfully moved {count} feedbacks from {fb_file} to MongoDB!")
                os.rename(fb_file, f"{fb_file}.migrated")
            except Exception as e:
                print(f"[MIGRATION ERROR] Failed to migrate {fb_file}: {e}")

# =========================
# MAIN (WEBHOOK & POLLING AUTO-SWITCH)
# =========================

def main():
    # Attempt to migrate any uploaded JSON data first
    migrate_json_data()

    load_users()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("getid", getid))
    app.add_handler(CommandHandler("begin", begin))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_notes))
    app.add_handler(CommandHandler("start", start))

    # Port configured automatically via Render’s environment configuration
    PORT = int(os.environ.get("PORT", 8000))
    WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL") # Injected automatically by Render

    if WEBHOOK_URL:
        # WEBHOOK MODE: Runs on Render
        WEBHOOK_URL = WEBHOOK_URL.rstrip('/')
        print(f"Starting bot in Webhook mode on port {PORT} with URL {WEBHOOK_URL}/{BOT_TOKEN}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
        )
    else:
        # POLLING MODE: Safe fallback for local offline testing
        print("Starting bot in Polling mode...")
        app.run_polling()


if __name__ == "__main__":
    main()
