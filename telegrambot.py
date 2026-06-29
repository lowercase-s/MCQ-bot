import asyncio
import json
import os
import re
from datetime import datetime, timezone, timedelta
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
leaderboard_col = db["leaderboard"]  # Leaderboard collection
polls_col = db["polls"]              # Tracks correct options for non-anonymous quizzes

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
    PollAnswerHandler,  # Handler to receive user responses on non-anonymous polls
)

# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY")  # Cerebras key
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")  # Cerebras Model
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
OWNER_USER_MODE_ACTIVE = False  # Track user mode context securely

SUBJECT_LIST = [
    "🇮🇳 Indian History", "🦏 Assam History", "🌍 World History", 
    "🏛 Polity", "🌍 Geography", "💰 Economics", 
    "🧪 Science & Tech", "🌿 Environment", "🎨 Art & Culture", 
    "🌐 International Relations", "🖥️ Current Affairs", "🦏 Assam Specific"
]

# Set up the new GenAI Client. 
# We use 'ai_client' because 'client' is already used above for MongoClient.
ai_client = genai.Client(api_key=GEMINI_API_KEY)

#=============
#WELCOME TEXT
#=============
WELCOME_TEXT = """👋 <b>Welcome to the MCQ Bot!</b>

Provide any <b>topic</b>, <b>article</b>, <b>notes</b>, or <b>study material</b>, and I'll generate an MCQ test for you.

📚 <b>Available Modes</b>

📋 <b>Statement Based MCQs</b>
• Generates statement-based MCQs in the UPSC Prelims style.

📝 <b>Regular MCQs</b>
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

<b>📋 Statement Based MCQs</b>
• This will generate statement-based MCQs in the UPSC Prelims style.

<b>📝 Regular MCQs</b>
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
# LEADERBOARD HELPER FUNCTIONS
# =========================

def get_or_create_leaderboard_user(user_id: int, username: str):
    user = leaderboard_col.find_one({"_id": user_id})
    if not user:
        user = {
            "_id": user_id,
            "username": username or f"User {user_id}",
            "points": 0,
            "attempted": 0,
            "correct": 0,
            "wrong": 0,
            "daily_challenges_completed_count": 0,
            "highest_challenge_streak": 0,
            "current_challenge_streak": 0,
            "last_challenge_attempt_date": None,
            "daily_challenges_data": {}
        }
        leaderboard_col.insert_one(user)
    return user

def update_leaderboard(user_id: int, username: str, is_correct: bool):
    # Ensure a document exists
    get_or_create_leaderboard_user(user_id, username)
    
    inc_fields = {
        "attempted": 1
    }
    if is_correct:
        inc_fields["correct"] = 1
        inc_fields["points"] = 10
    else:
        inc_fields["wrong"] = 1
        
    leaderboard_col.update_one(
        {"_id": user_id},
        {
            "$set": {"username": username or f"User {user_id}"},
            "$inc": inc_fields
        }
    )

def get_top_leaderboard(limit=10):
    return list(leaderboard_col.find().sort("points", -1).limit(limit))

def get_user_score(user_id: int):
    user = leaderboard_col.find_one({"_id": user_id})
    if not user:
        return None
    
    points = user.get("points", 0)
    rank = leaderboard_col.count_documents({"points": {"$gt": points}}) + 1
    user["rank"] = rank
    return user


# =========================
# CONTENT GENERATOR
# =========================

def generate_content(notes: str, mode: str = "upsc", count: int = 2, user_id: int = None) -> str:
    print(f">>> Preparing prompt for mode: {mode}, count: {count}")
    
    # Subject constraint & dynamic Current affairs rules
    current_date_str = datetime.now().strftime("%B %Y")
    clean_subject = re.sub(r'[^\w\s&]', '', notes).strip()
    
    SUBJECT_LIST_CLEAN = [
        "Indian History", "Assam History", "World History", "Polity", "Geography", "Economics", 
        "Science & Tech", "Environment", "Art & Culture", "International Relations", "Current Affairs",
        "Assam Specific", "Daily Challenge Mix"
    ]
    
    subject_rule = ""
    if "Assam Specific" in clean_subject:
        subject_rule = (
            "SUBJECT CONSTRAINTS: Generate challenging, standard, and highly educational questions "
            "strictly focused on Assam Specific topics. Ensure a balanced, diverse mix covering:\n"
            "1. Latest Assam current affairs (incorporate current 2026 data),\n"
            "2. Assam government schemes,\n"
            "3. Assam geography,\n"
            "4. Assam polity and administration,\n"
            "5. Assam economy,\n"
            "6. Assam culture and festivals,\n"
            "7. Assam history,\n"
            "8. Assam wildlife and national parks,\n"
            "9. Assam awards and personalities,\n"
            "10. Important Assam organizations and institutions.\n"
            "Maintain high factual density and educational value."
        )
    elif "Daily Challenge Mix" in clean_subject:
        subject_rule = (
            "SUBJECT CONSTRAINTS: Generate a perfectly balanced, diverse mix of questions "
            "covering multiple different subject categories such as History, Polity, Geography, "
            "Economics, Science & Tech, Environment, Art & Culture, International Relations, and Current Affairs."
        )
    elif clean_subject in SUBJECT_LIST_CLEAN:
        subject_rule = f"SUBJECT CONSTRAINTS: Generate questions ONLY from the selected subject category '{clean_subject}'."
        if "Current Affairs" in clean_subject:
            subject_rule += f" Ensure all news, policies, events, and reports are from the most recent months leading up to {current_date_str}. Do not use old historical current affairs."
    
    # Prevent repeating questions already completed by the user
    past_questions_str = "None"
    if user_id:
        leaderboard_user = leaderboard_col.find_one({"_id": user_id})
        if leaderboard_user and "attempted_polls" in leaderboard_user:
            attempted_poll_ids = leaderboard_user["attempted_polls"]
            if attempted_poll_ids:
                past_polls = list(polls_col.find({"_id": {"$in": attempted_poll_ids}}))
                past_questions = [p["question"] for p in past_polls if "question" in p]
                if past_questions:
                    # Feed the last 15 questions to prevent concept collision
                    past_questions_str = ", ".join([f'"{q}"' for q in past_questions[-15:]])
                    
    avoid_repeating_rule = f"AVOID REPEATING COMPLETED QUESTIONS: Do NOT generate questions that are identical or highly similar in core concepts/facts to any of the following questions previously attempted by this user: [{past_questions_str}]."

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

You will be provided with a text, article, or general topic. 
CRITICAL RULE FOR SHORT INPUTS: If the provided input is extremely short (such as a single word or generic topic like 'Mars'), leverage your vast internal pre-trained knowledge base to craft challenging, standard, and highly educational UPSC Prelims-level questions about the topic. Do not restrict yourself to only the words in the input.

From this topic/notes, generate exactly {count} multiple-choice questions in the STRICT format below.

Rules:
- No explanations, no markdown, no extra text outside the format.
- Focus on high-level factual density, analytical traps, and elimination-based structures.
- CRITICAL - QUESTION FRAMING: Do NOT write telegraphic, compressed, note-taking, or stubby phrases (e.g., 'largest Mars volcano', 'First Mars Rover', 'Mars distance from the sun'). Instead, write fully realized, grammatically perfect, and formal complete sentences. 
  * Example of BAD framing: "largest Mars volcano" or "Mars atmosphere gas"
  * Example of GOOD framing: "With reference to Martian geology, consider the following statements:" or "With reference to the geological features of the solar system, consider the following statements about Olympus Mons:"
- Keep options SHORT (max 12–15 words each; standard UPSC answers like "Statement 1 only").
- Keep statements concise but highly factual, standard, and deep.
- {subject_rule}
- {avoid_repeating_rule}

FORMAT:

TITLE: <A professional, high-quality newspaper headline written strictly in the authoritative, editorial style of prestigious publications like 'The Hindu', 'The Indian Express', or 'Press Information Bureau (PIB)'. It must be engaging, informative, and grammatically impeccable. Avoid generic or informal wording, and do not use quotes or markdown. Example: "Government Unveils New Sovereign Green Bond Framework to Boost Clean Energy Funding" or "Supreme Court Expands Scope of Article 21 to Include Right against Climate Change Impacts".>

SUMMARY:
<Write a single, highly dense, continuous paragraph (approximately 100–120 words) summarizing the topic from a strict UPSC perspective. Do NOT use bullet points, sub-headers, or lists. It must seamlessly integrate:
1. Core definition and context (what it is, launch year, target group/objectives).
2. Functional details, mechanisms, or associated global/national initiatives.
3. Key factual milestones (important dates, agency/WHO declarations, or critical statistics).
4. Current relevance or future significance.

Example of the target summary format:
"National Immunisation Day (NID), or Pulse Polio Immunisation Day, is a nationwide campaign launched in 1995 to eradicate polio by administering two drops of Oral Polio Vaccine (OPV) to all children below five years, irrespective of their vaccination status. It is part of the Global Polio Eradication Initiative (1988) and involves booth-based vaccination, door-to-door campaigns, transit vaccination, and Acute Flaccid Paralysis (AFP) surveillance. India reported its last wild polio case in 2011 and was declared polio-free by the WHO in 2014. NID remains crucial for maintaining India's polio-free status through high immunisation coverage and continuous surveillance.">

{format_str}

IMPORTANT RULES:
- Generate EXACTLY {count} MCQs (from MCQ1 to MCQ{count}).
- Keep statements factual, standard, and UPSC-level tricky.
- Do not repeat the same answer pattern or the same facts across multiple questions.
- Ensure each MCQ block is clearly separated by a blank line.
- The ENTIRE poll question text (including statements and "Which of the above...") must be strictly under 250 characters (Telegram limit). Ensure complete, elegant sentences that safely fit this limit.
- Maximum 2 statements per question.
- Each statement must be concise but rich in specific information (e.g., numerical facts, geographical names, names of missions/treaties).

{notes}
"""
    else:
        prompt = f"""
You are an expert quiz question setter specializing in high-level trivia, academic, and competitive exams.

You will be provided with a text, article, or general topic. 
CRITICAL RULE FOR SHORT INPUTS: If the provided input is extremely short (such as a single word or generic topic like 'Mars'), leverage your vast internal pre-trained knowledge base to craft challenging, standard, and highly educational quiz questions about the topic. Do not restrict yourself to only the words in the input.

From this topic/notes, generate exactly {count} multiple-choice questions in the STRICT format below.

Rules:
- No explanations, no markdown, no extra text outside format.
- Keep options SHORT (options max 10–12 words each)
- Generate standard 4-option multiple choice questions.
- CRITICAL - QUESTION FRAMING: Do NOT write telegraphic, compressed, note-taking, or stubby phrases (e.g., 'largest Mars volcano', 'First Mars Rover', 'Mars distance from the sun'). Instead, write fully realized, grammatically perfect, and formal complete sentences. 
  * Example of BAD framing: "largest Mars volcano" or "Mars atmosphere gas"
  * Example of GOOD framing: "What is the name of the largest volcano in the solar system, which is located on Mars?" or "The 'Martian year' (the time it takes Mars to orbit the Sun) is approximately how many Earth days?" or "Which mission was the first to successfully land a rover on the surface of Mars?"
- Do not use statement-based logic (like "Consider the following statements" or "Statement 1 only") in this mode.
- {subject_rule}
- {avoid_repeating_rule}

FORMAT:

TITLE: <A professional, high-quality news headline written strictly in the authoritative, editorial style of prestigious publications like 'The Hindu', 'The Indian Express', or 'Science Daily'. It must be engaging, informative, and grammatically impeccable. Avoid generic or informal wording, and do not use quotes or markdown. Example: "NASA's James Webb Telescope Discovers Atmospheres on Trappist-1 Exoplanets" or "RBI Keeps Repo Rate Unchanged at 6.5% Citing Persistent Food Inflation Concerns".>

SUMMARY:
<Write a single, highly dense, continuous paragraph (approximately 80–100 words) summarizing the core topic. Do NOT use bullet points, sub-headers, or lists. It must seamlessly integrate:
1. Main definition or core theme of the notes.
2. Essential factual details or mechanisms.
3. Broad significance or current status.>

{format_str}

IMPORTANT RULES:
- Generate EXACTLY {count} MCQs (from MCQ1 to MCQ{count}).
- Keep questions factual, clear, informative, and challenging.
- Do not repeat same concepts or patterns across different questions.
- Ensure each MCQ block is clearly separated by a blank line.
- The entire question text (excluding options) must be strictly under 250 characters (Telegram limit) to prevent truncation, but it MUST be a complete, elegant, and grammatically correct sentence.

{notes}
"""

    # --- Step 1: Try Cerebras API (First tier) ---
    if CEREBRAS_API_KEY:
        try:
            print(f">>> Attempting Cerebras API Call using model: {CEREBRAS_MODEL}...")
            headers = {
                "Authorization": f"Bearer {CEREBRAS_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": CEREBRAS_MODEL,
                "messages": [
                    {"role": "user", "content": prompt}
                ]
            }

            with httpx.Client(timeout=60.0) as http_client:
                response = http_client.post(
                    "https://api.cerebras.ai/v1/chat/completions",
                    headers=headers, json=payload
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                if content:
                    print(">>> Cerebras API completed successfully.")
                    return content
        except Exception as cerebras_error:
            print(f">>> Cerebras API failed (Limit reached or error): {cerebras_error}")
            print(">>> Fallback: Switching to Gemini API...")
    else:
        print(">>> CEREBRAS_API_KEY is not defined. Defaulting directly to Gemini...")

    # --- Step 2: Try Gemini API (Second tier) ---
    if GEMINI_API_KEY:
        try:
            print(">>> Attempting Gemini API Call...")
            response = ai_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            content = response.text or ""
            if content:
                print(">>> Gemini API completed successfully.")
                return content
        except Exception as gemini_error:
            print(f">>> Gemini API failed: {gemini_error}")
            print(">>> Fallback: Switching to Groq API...")
    else:
        print(">>> GEMINI_API_KEY is not defined. Defaulting directly to Groq...")

    # --- Step 3: Fallback to Groq API (Third tier) if both failed ---
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
            print(f">>> Groq API failed: {groq_error}")
    else:
        print(">>> GROQ_API_KEY is not defined. No fallback API endpoints available.")

    return ""


# =========================
# COMMANDS & BANNER DISPATCH
# =========================

async def getid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Chat ID: {update.effective_chat.id}")


async def send_daily_banner(context: ContextTypes.DEFAULT_TYPE):
    """Dispatches the daily header details to the official channel."""
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
    except Exception as e:
        print(f"Failed to pin daily banner message: {e}")


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await send_daily_banner(context)
    await update.message.reply_text("✅ Daily banner sent and pinned.")


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


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays top 10 users ranked by points."""
    top_users = get_top_leaderboard(10)
    if not top_users:
        await update.message.reply_text("🏆 <b>Leaderboard</b>\n\nNo records yet!", parse_mode="HTML")
        return
    
    text = "🏆 <b>Leaderboard</b>\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for idx, u in enumerate(top_users):
        name = u.get("username") or f"User {u['_id']}"
        points = u.get("points", 0)
        if idx < 3:
            text += f"{medals[idx]} {name} — {points} pts\n"
        else:
            text += f"{idx + 1}. {name} — {points} pts\n"
            
    await update.message.reply_text(text, parse_mode="HTML")


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tracks responses to non-anonymous polls to update points securely."""
    poll_answer = update.poll_answer
    poll_id = poll_answer.poll_id
    user = poll_answer.user
    
    # Check if this is a registered poll
    poll_data = polls_col.find_one({"_id": poll_id})
    if not poll_data:
        return
        
    correct_option_id = poll_data.get("correct_option_id")
    selected_option_ids = poll_answer.option_ids
    
    # Selected options will be empty if a vote is retracted
    if not selected_option_ids:
        return
        
    is_correct = (selected_option_ids[0] == correct_option_id)
    username = user.username or user.first_name
    
    update_leaderboard(user.id, username, is_correct)

    # Save poll_id to this user's records to prevent duplicate questions in Subject Quizzes
    leaderboard_col.update_one(
        {"_id": user.id},
        {"$addToSet": {"attempted_polls": poll_id}}
    )

    # If this is a Daily Challenge poll, update today's score
    if poll_data.get("is_daily_challenge"):
        poll_date = poll_data.get("date")
        if poll_date:
            field_prefix = f"daily_challenges_data.{poll_date}"
            
            # Determine increments
            inc_fields = {
                f"{field_prefix}.attempted": 1
            }
            if is_correct:
                inc_fields[f"{field_prefix}.correct"] = 1
            else:
                inc_fields[f"{field_prefix}.wrong"] = 1
                
            leaderboard_col.update_one(
                {"_id": user.id},
                {"$inc": inc_fields}
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
# KEYBOARD HANDLERS
# =========================

def owner_main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📰 Send Daily MCQs"],
            ["📢 Updates", "⚙️ Settings"],
            ["👤 User Mode"],
        ],
        resize_keyboard=True,
    )

def owner_batch_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["✅ Process & Post MCQs"],
            ["❌ Cancel"]
        ],
        resize_keyboard=True,
    )

def updates_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📢 Send Updates"],
            ["Send sunday update", "✍️ Custom updates"],
            ["⬅️ Back"]
        ],
        resize_keyboard=True,
    )

def main_keyboard(is_owner_user_mode=False):
    buttons = [
        ["🧠 Custom Quiz", "📖 Subject-wise Quiz"],
        ["🎯 Daily Challenge", "🏅 My Score"],
        ["💬 Feedback", "❓ Help"]
    ]
    if is_owner_user_mode:
        buttons.append(["👑 God Mode"])
    return ReplyKeyboardMarkup(
        buttons,
        resize_keyboard=True
    )

def custom_quiz_format_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📋 Statement Based MCQs", "📝 Regular MCQs"],
            ["⬅️ Back"]
        ],
        resize_keyboard=True
    )

def subject_type_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📋 Statement-based MCQs"],
            ["📝 Regular MCQs"],
            ["⬅️ Back"]
        ],
        resize_keyboard=True
    )

def subject_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🇮🇳 Indian History", "🦏 Assam History"],
            ["🌍 World History", "🏛 Polity"],
            ["🌍 Geography", "💰 Economics"],
            ["🧪 Science & Tech", "🌿 Environment"],
            ["🎨 Art & Culture", "🌐 International Relations"],
            ["🖥️ Current Affairs", "🦏 Assam Specific"],
            ["⬅️ Back"]
        ],
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


# =========================
# DAILY CHALLENGE UNIFIED FLOW
# =========================

async def trigger_daily_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE, msg, user_id: int):
    """Executes the daily challenge initiation, tracking streaks, scores and locks."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    leaderboard_user = leaderboard_col.find_one({"_id": user_id})
    
    daily_data = leaderboard_user.get("daily_challenges_data", {}) if leaderboard_user else {}
    today_score = daily_data.get(today_str)
    
    is_owner_user = (user_id == OWNER_ID and OWNER_USER_MODE_ACTIVE)
    
    if today_score:
        correct = today_score.get("correct", 0)
        wrong = today_score.get("wrong", 0)
        attempted = today_score.get("attempted", 0)
        accuracy = (correct / attempted) * 100 if attempted > 0 else 0.0
        
        await msg.reply_text(
            f"🎯 <b>Daily Challenge Completed!</b>\n\n"
            f"You have already attempted today's challenge.\n\n"
            f"<b>Today's Score:</b>\n"
            f"✅ Correct: {correct}/10\n"
            f"❌ Wrong: {wrong}\n"
            f"🎯 Accuracy: {accuracy:.2f}%",
            parse_mode="HTML",
            reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
        )
        return

    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    
    current_streak = 1
    highest_streak = 0
    completed_count = 0
    
    if leaderboard_user:
        completed_count = leaderboard_user.get("daily_challenges_completed_count", 0) + 1
        highest_streak = leaderboard_user.get("highest_challenge_streak", 0)
        last_attempt = leaderboard_user.get("last_challenge_attempt_date")
        
        if last_attempt == yesterday_str:
            current_streak = leaderboard_user.get("current_challenge_streak", 0) + 1
        elif last_attempt == today_str:
            current_streak = leaderboard_user.get("current_challenge_streak", 1)
        else:
            current_streak = 1
            
        if current_streak > highest_streak:
            highest_streak = current_streak
    else:
        # Auto-provision a score entry
        username_fallback = update.effective_user.username or update.effective_user.first_name
        get_or_create_leaderboard_user(user_id, username_fallback)
        completed_count = 1
        current_streak = 1
        highest_streak = 1
        
    leaderboard_col.update_one(
        {"_id": user_id},
        {
            "$set": {
                "last_challenge_attempt_date": today_str,
                "current_challenge_streak": current_streak,
                "highest_challenge_streak": highest_streak,
                "daily_challenges_completed_count": completed_count,
                f"daily_challenges_data.{today_str}": {"correct": 0, "wrong": 0, "attempted": 0}
            }
        }
    )
    
    await msg.reply_text(
        "🎯 <b>Daily Challenge Initiated!</b>\n\n"
        "Generating today's 10-question mixed quiz for you. Good luck! 🔥", 
        parse_mode="HTML"
    )
    
    prev_poll_count = USER_POLL_COUNT.get(user_id, 2)
    USER_POLL_COUNT[user_id] = 10
    
    await process_user_article("Daily Challenge Mix", context, user_id, mode="quiz", is_daily_challenge=True)
    
    USER_POLL_COUNT[user_id] = prev_poll_count


async def handle_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = update.effective_user.id
    text = msg.text
    global USER_BATCHES, USER_MODE, USER_POLL_COUNT

    ensure_user(user_id)
    is_owner_user = (user_id == OWNER_ID and OWNER_USER_MODE_ACTIVE)

    # =========================
    # Score lookup
    # =========================
    if text in ("🏅 My Score", "🏅 My Scores"):
        score = get_user_score(user_id)
        if not score or score.get("attempted", 0) == 0:
            await msg.reply_text(
                "🏅 <b>Your Score</b>\n\nYou haven't attempted any questions yet.",
                parse_mode="HTML"
            )
            return
        
        points = score.get("points", 0)
        attempted = score.get("attempted", 0)
        correct = score.get("correct", 0)
        wrong = score.get("wrong", 0)
        rank = score.get("rank", 1)
        
        # Pull customized Daily Challenge stats
        daily_completed = score.get("daily_challenges_completed_count", 0)
        highest_streak = score.get("highest_challenge_streak", 0)
        
        accuracy = (correct / attempted) * 100 if attempted > 0 else 0.0
        
        response = (
            "🏅 <b>Your Score</b>\n\n"
            f"⭐ <b>Points</b>: {points}\n"
            f"📝 <b>Questions Attempted</b>: {attempted}\n"
            f"✅ <b>Correct</b>: {correct}\n"
            f"❌ <b>Wrong</b>: {wrong}\n"
            f"🎯 <b>Accuracy</b>: {accuracy:.2f}%\n\n"
            f"🎯 <b>Daily Challenges Completed</b>: {daily_completed}\n"
            f"🏆 <b>Highest Daily Challenge Streak</b>: {highest_streak} day(s)\n\n"
        )
        await msg.reply_text(response, parse_mode="HTML")
        return

    # =========================
    # Daily Challenge entry point
    # =========================
    if text in ("🎯 Daily Challenge", "Start Daily Challenge"):
        await trigger_daily_challenge(update, context, msg, user_id)
        return

    # =========================
    # Custom Quiz flow
    # =========================
    if text in ("🧠 Custom Quiz", "Custom Quiz"):
        USER_STATE[user_id] = "custom_quiz_type_selection"
        await msg.reply_text(
            "🛠️ <b>Custom Quiz</b>\n\nGenerate MCQs from your own notes, articles, or topics.\n\nChoose your preferred format:",
            parse_mode="HTML",
            reply_markup=custom_quiz_format_keyboard()
        )
        return

    if USER_STATE.get(user_id) == "custom_quiz_type_selection":
        if text == "⬅️ Back":
            USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"
            await msg.reply_text(
                "Returned to main menu.",
                reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
            )
            return

        if text == "📋 Statement Based MCQs":
            USER_STATE[user_id] = "upsc"
            await msg.reply_text(
                "📋 Statement based MCQs\n\nChoose option:",
                reply_markup=mode_keyboard()
            )
            return

        if text == "📝 Regular MCQs":
            USER_STATE[user_id] = "quiz"
            await msg.reply_text(
                "📝 Regular MCQs\n\nChoose option:",
                reply_markup=mode_keyboard()
            )
            return

        await msg.reply_text("Please choose a format option from the keyboard 👇", reply_markup=custom_quiz_format_keyboard())
        return

    # =========================
    # Subject Quiz format selection
    # =========================
    if text == "📖 Subject-wise Quiz":
        USER_STATE[user_id] = "subject_quiz_mode_selection"
        await msg.reply_text(
            "📖 <b>Subject-wise Quiz Mode</b>\n\nChoose your preferred quiz format:",
            parse_mode="HTML",
            reply_markup=subject_type_keyboard()
        )
        return

    if USER_STATE.get(user_id) == "subject_quiz_mode_selection":
        if text == "⬅️ Back":
            USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"
            await msg.reply_text(
                "Returned to main menu.",
                reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
            )
            return

        if text == "📋 Statement-based MCQs":
            USER_STATE[user_id] = "subject_quiz_upsc"
            await msg.reply_text(
                "📋 <b>Statement-based Subject Quiz</b>\n\nChoose a subject to generate 10 statement-based UPSC-style MCQs:",
                parse_mode="HTML",
                reply_markup=subject_keyboard()
            )
            return

        if text == "📝 Regular MCQs":
            USER_STATE[user_id] = "subject_quiz_regular"
            await msg.reply_text(
                "📝 <b>Regular Subject Quiz</b>\n\nChoose a subject to generate 10 regular 4-option MCQs:",
                parse_mode="HTML",
                reply_markup=subject_keyboard()
            )
            return

        await msg.reply_text("Please choose an option from the keyboard 👇", reply_markup=subject_type_keyboard())
        return

    if USER_STATE.get(user_id) in ("subject_quiz_upsc", "subject_quiz_regular"):
        if text == "⬅️ Back":
            # Go back to format selection step
            USER_STATE[user_id] = "subject_quiz_mode_selection"
            await msg.reply_text(
                "Returned to format selection.",
                reply_markup=subject_type_keyboard()
            )
            return
            
        if text in SUBJECT_LIST:
            if USER_DAILY_USAGE[user_id]["count"] >= 10:
                await msg.reply_text("❌ Daily limit reached.\n\nYou can generate only 10 articles/quizzes per day.")
                return
            
            current_format_state = USER_STATE[user_id]
            mode_to_use = "upsc" if current_format_state == "subject_quiz_upsc" else "quiz"
            mode_label = "statement-based UPSC-style" if mode_to_use == "upsc" else "regular 4-option"
            
            await msg.reply_text(f"🚀 Generating 10 {mode_label} MCQs on {text}...")
            
            # Temporarily configure output count to 10 for subject quizzes
            prev_poll_count = USER_POLL_COUNT.get(user_id, 2)
            USER_POLL_COUNT[user_id] = 10
            
            # Execute user poll generation with dynamically specified mode
            await process_user_article(text, context, user_id, mode=mode_to_use)
            
            USER_DAILY_USAGE[user_id]["count"] += 1
            USER_POLL_COUNT[user_id] = prev_poll_count  # Restore preference
            
            remaining = 10 - USER_DAILY_USAGE[user_id]["count"]
            USER_STATE[user_id] = "owner_user_mode" if is_owner_user else "main"
            
            await msg.reply_text(
                f"✅ Done!\n📌 Remaining today: {remaining}\n\nChoose option:",
                reply_markup=main_keyboard(is_owner_user_mode=is_owner_user)
            )
            return
            
        await msg.reply_text("Please choose a subject from the keyboard below 👇", reply_markup=subject_keyboard())
        return

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
    if text == "📋 Statement Based MCQs":
        USER_STATE[user_id] = "upsc"
        await msg.reply_text(
            "📋 Statement Based MCQs selected\n\nChoose option:",
            reply_markup=mode_keyboard()
        )
        return

    if text == "📝 Regular MCQs":
        USER_STATE[user_id] = "quiz"
        await msg.reply_text(
            "📝 Regular MCQs selected\n\nChoose option:",
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
        current_state = USER_STATE.get(user_id, "")
        if current_state in ("upsc", "quiz"):
            USER_STATE[user_id] = "custom_quiz_type_selection"
            await msg.reply_text(
                "Choose format:",
                reply_markup=custom_quiz_format_keyboard()
            )
            return

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

    global OWNER_USER_MODE_ACTIVE
    is_owner_user = (user_id == OWNER_ID and OWNER_USER_MODE_ACTIVE)

    if user_id != OWNER_ID or is_owner_user:
        if user_id == OWNER_ID and msg.text == "👑 God Mode":
            OWNER_USER_MODE_ACTIVE = False
            USER_STATE[OWNER_ID] = "main"
            await msg.reply_text("Welcome back to Owner Mode!", reply_markup=owner_main_keyboard())
            return
        await handle_user(update, context)
        return

    if update.effective_user.is_bot:
        return

    global BATCH_MODE, OWNER_QUEUE, USER_BATCHES, USER_MODE

    # =========================
    # Owner Batch Mode: Send Daily MCQs
    # =========================
    if USER_STATE.get(OWNER_ID) == "owner_batch":
        if msg.text == "❌ Cancel":
            BATCH_MODE = False
            OWNER_QUEUE.clear()
            USER_STATE[OWNER_ID] = "main"
            await msg.reply_text("❌ Batch cancelled. Queue cleared.", reply_markup=owner_main_keyboard())
            return

        if msg.text == "✅ Process & Post MCQs":
            if not OWNER_QUEUE:
                await msg.reply_text("❌ Queue is empty. Please send at least one article first.")
                return

            processing_msg = await msg.reply_text("🚀 Starting publication batch...\n1️⃣ Dispatching daily header message to group...")
            
            # Step 1: Send Daily Header
            await send_daily_banner(context)
            
            # Step 2: Process batch articles sequentially
            await processing_msg.edit_text(f"🚀 Processing {len(OWNER_QUEUE)} article(s) and posting polls...")
            for idx, article in enumerate(OWNER_QUEUE, start=1):
                await process_article(article, context)

            # Cleanup
            OWNER_QUEUE.clear()
            BATCH_MODE = False
            USER_STATE[OWNER_ID] = "main"
            await processing_msg.edit_text("✅ Daily Update and associated MCQs have been published successfully!")
            await msg.reply_text("Owner Options:", reply_markup=owner_main_keyboard())
            return

        # Add input to the owner's batch queue
        OWNER_QUEUE.append(msg.text)
        await msg.reply_text(
            f"📥 Added to batch ({len(OWNER_QUEUE)} article(s))\n"
            f"Keep sending more articles, or tap '✅ Process & Post MCQs' when finished.",
            reply_markup=owner_batch_keyboard()
        )
        return

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
    if msg.text == "📰 Send Daily MCQs":
        BATCH_MODE = True
        OWNER_QUEUE.clear()
        USER_STATE[OWNER_ID] = "owner_batch"
        await msg.reply_text(
            "📰 <b>Send Daily MCQs Mode Activated</b>\n\n"
            "Send your articles/notes one by one. The bot will save them.\n"
            "When you are ready, click <b>✅ Process & Post MCQs</b>. "
            "This will post the daily banner first, followed by all MCQs and summaries.",
            parse_mode="HTML",
            reply_markup=owner_batch_keyboard()
        )
        return

    if msg.text == "👤 User Mode":
        OWNER_USER_MODE_ACTIVE = True
        USER_STATE[OWNER_ID] = "main"
        await msg.reply_text(
            "Switched to User Mode. You can now use the bot like a normal user.",
            reply_markup=main_keyboard(is_owner_user_mode=True)
        )
        return

    if msg.text in ("📢 Updates", "Updates"):
        await msg.reply_text("Choose an update action:", reply_markup=updates_keyboard())
        return

    if msg.text == "⬅️ Back":
        await msg.reply_text("Returned to Owner Panel.", reply_markup=owner_main_keyboard())
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

    await msg.reply_text("Invalid action. Please use the menu buttons.")


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
        await update.message.reply_text("Welcome back, Owner!", reply_markup=owner_main_keyboard())
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


async def process_user_article(notes: str, context: ContextTypes.DEFAULT_TYPE, user_id: int, mode: str = "upsc", is_daily_challenge: bool = False):
    await context.bot.send_message(chat_id=user_id, text="⏳ Processing article...")
    ensure_user(user_id)
    count = USER_POLL_COUNT.get(user_id, 2)

    # Pass user_id to restrict questions by attempted list history
    result = await asyncio.to_thread(generate_content, notes, mode, count, user_id)
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

        correct_idx = next((i for i, o in enumerate(opt) if o[0] == ans), 0)
        user_poll = await context.bot.send_poll(
            chat_id=user_id,
            question=q[:300],
            options=[o[1][:100] for o in opt],
            type="quiz",
            is_anonymous=False,
            correct_option_id=correct_idx
        )
        # Store in database mapped with is_daily_challenge tag to keep updates synced
        polls_col.insert_one({
            "_id": user_poll.poll.id, 
            "correct_option_id": correct_idx, 
            "question": q[:300],
            "is_daily_challenge": is_daily_challenge,
            "user_id": user_id,
            "date": datetime.now().strftime("%Y-%m-%d")
        })
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
# DAILY CHALLENGE REMINDER SCHEDULER
# =========================

async def daily_reminder_scheduler(application):
    """Sends reminders dynamically at 8:00 AM IST daily to bot subscribers."""
    print("Daily challenge reminder scheduler initialized.")
    IST = timezone(timedelta(hours=5, minutes=30))
    last_sent_date = None

    while True:
        try:
            now_ist = datetime.now(IST)
            # Checks for exactly 8:00 AM IST
            if now_ist.hour == 8 and now_ist.minute == 0:
                today_str = now_ist.strftime("%Y-%m-%d")
                if last_sent_date != today_str:
                    last_sent_date = today_str
                    print(f"Executing daily challenge reminders broadcast for {today_str}...")

                    # Fetch active users from MongoDB
                    users = list(users_col.find())

                    inline_kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🎯 Start Daily Challenge", callback_data="start_daily_challenge")]
                    ])

                    msg_text = (
                        "🎯 <b>Daily Challenge is Ready!</b>\n\n"
                        "Test your knowledge with today's 10-question challenge and continue your streak! 🔥\n\n"
                        "Good luck! 🍀"
                    )

                    for u in users:
                        uid = u.get("id") or u.get("_id")
                        if not uid:
                            continue
                        try:
                            await application.bot.send_message(
                                chat_id=uid,
                                text=msg_text,
                                parse_mode="HTML",
                                reply_markup=inline_kb
                            )
                            # Control throughput and avoid hitting Telegram limits
                            await asyncio.sleep(0.05)
                        except Exception as e:
                            print(f"Skipping reminder for user {uid} due to chat limits/block: {e}")

            # Polling frequency
            await asyncio.sleep(60)
        except Exception as e:
            print(f"Error executing reminder routine: {e}")
            await asyncio.sleep(60)


async def post_init(application):
    """Integrates background cron tasks seamlessly upon bot start."""
    asyncio.create_task(daily_reminder_scheduler(application))


# =========================
# MAIN (WEBHOOK & POLLING AUTO-SWITCH)
# =========================

def main():
    # Attempt to migrate any uploaded JSON data first
    migrate_json_data()

    load_users()

    # Integrated post_init hook to register background scheduling engine
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("getid", getid))
    app.add_handler(CommandHandler("begin", begin))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_notes))
    app.add_handler(CommandHandler("start", start))

    # Port configured automatically via Render’s environment configuration
    PORT = int(os.environ.get("PORT", 8000))
    WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL") # Injected automatically by Render

    if WEBHOOK_URL:
        # WEBHOOK MODE: Runs on Render
        # Normalizes URL ending
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
