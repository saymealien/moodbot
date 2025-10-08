#There was a bug when u press "Settings" sometimes bot few times reacted as "Please enter a number between 1 and 10:" before showing settings. Fixed ver here.

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from datetime import datetime, time
from zoneinfo import ZoneInfo, available_timezones
import asyncio
import nest_asyncio
import sqlite3
import json
import pandas as pd
from io import BytesIO
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
import difflib
import random  

nest_asyncio.apply()

# =======================
# States
# =======================
ADD_PARAM, DELETE_PARAM_SELECT = range(2)
ESTIMATE_START = range(1)
EXPORT_CHOOSE = range(1)
SET_TIMEZONE, SET_REMINDERS = range(2, 4)

DB_FILE = "mood_tracker.db"

# =======================
# Keyboards
# =======================
MAIN_MENU = ReplyKeyboardMarkup([
    ["Estimate", "Export results"],
    ["Settings"]
], resize_keyboard=True)

SETTINGS_MENU = ReplyKeyboardMarkup([
    ["Set timezone", "Set reminders"],
    ["Add parameter", "Delete parameter"],
    ["Back to Main"]
], resize_keyboard=True)

PARAMETER_MENU = ReplyKeyboardMarkup([
    ["Mood", "Energy", "Sleep Quality"],
    ["Stress", "Focus", "Motivation"],
    ["Custom Parameter", "Finish", "Cancel"]
], resize_keyboard=True, one_time_keyboard=True)

EXPORT_MENU = ReplyKeyboardMarkup([
    ["CSV", "XLSX", "PDF"], ["Cancel"]
], resize_keyboard=True, one_time_keyboard=True)

TIMEZONE_MENU = ReplyKeyboardMarkup([
    ["New York", "London", "Berlin", "Tokyo"],
    ["Moscow", "Sydney", "Los Angeles", "Other"],
    ["Cancel"]
], resize_keyboard=True)

# =======================
# Database functions
# =======================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        date TEXT,
        parameter TEXT,
        value INTEGER
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER PRIMARY KEY,
        timezone TEXT DEFAULT 'UTC',
        reminders TEXT,
        parameters TEXT
    )
    """)
    conn.commit()
    conn.close()

def add_entry_to_db(chat_id, date, parameter, value):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO entries (chat_id, date, parameter, value) VALUES (?, ?, ?, ?)",
              (chat_id, date, parameter, value))
    conn.commit()
    conn.close()

def get_entries_from_db(chat_id, start_date=None, end_date=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    query = "SELECT date, parameter, value FROM entries WHERE chat_id=?"
    params = [chat_id]
    if start_date:
        query += " AND date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND date <= ?"
        params.append(end_date)
    query += " ORDER BY date DESC"
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return rows

def get_user_parameters(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT parameters FROM user_settings WHERE user_id=?", (user_id,))
    result = c.fetchone()
    conn.close()
    if result and result[0]:
        try:
            return json.loads(result[0])
        except json.JSONDecodeError:
            return []
    return []

def set_user_parameters(user_id, parameters):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    params_json = json.dumps(parameters)
    c.execute("""INSERT OR REPLACE INTO user_settings 
                 (user_id, parameters, timezone, reminders) 
                 VALUES (?, ?, 
                         COALESCE((SELECT timezone FROM user_settings WHERE user_id=?), 'UTC'), 
                         COALESCE((SELECT reminders FROM user_settings WHERE user_id=?), '[]'))""",
              (user_id, params_json, user_id, user_id))
    conn.commit()
    conn.close()

def get_user_timezone(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT timezone FROM user_settings WHERE user_id=?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else "UTC"

def set_user_timezone(user_id, timezone):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO user_settings 
                 (user_id, timezone, parameters, reminders) 
                 VALUES (?, ?, 
                         COALESCE((SELECT parameters FROM user_settings WHERE user_id=?), '[]'), 
                         COALESCE((SELECT reminders FROM user_settings WHERE user_id=?), '[]'))""",
              (user_id, timezone, user_id, user_id))
    conn.commit()
    conn.close()

def get_user_reminders(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT reminders FROM user_settings WHERE user_id=?", (user_id,))
    result = c.fetchone()
    conn.close()
    if result and result[0]:
        try:
            return json.loads(result[0])
        except json.JSONDecodeError:
            return []
    return []

def set_user_reminders(user_id, reminders):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    reminders_json = json.dumps(reminders)
    c.execute("""INSERT OR REPLACE INTO user_settings 
                 (user_id, reminders, timezone, parameters) 
                 VALUES (?, ?, 
                         COALESCE((SELECT timezone FROM user_settings WHERE user_id=?), 'UTC'), 
                         COALESCE((SELECT parameters FROM user_settings WHERE user_id=?), '[]'))""",
              (user_id, reminders_json, user_id, user_id))
    conn.commit()
    conn.close()

# =======================
# Reminder system 
# =======================
last_sent = {}

async def send_reminder(user_id: int, app: Application):
    try:
        await app.bot.send_message(user_id, "⏰ Time to rate your daily parameters!")
    except Exception as e:
        print(f"Failed reminder to {user_id}: {e}")

def get_all_users_with_reminders():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, timezone, reminders FROM user_settings WHERE reminders IS NOT NULL AND reminders != '[]'")
    results = c.fetchall()
    conn.close()
    return results

async def schedule_reminders(app: Application):
    global last_sent
    while True:
        users = get_all_users_with_reminders()
        current_utc = datetime.utcnow()
        
        for user_id, tz_str, reminders_json in users:
            try:
                tz = ZoneInfo(tz_str) if tz_str else ZoneInfo("UTC")
                user_time = current_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
                
                if user_id not in last_sent:
                    last_sent[user_id] = {}
                
                reminders = json.loads(reminders_json)
                for reminder_time in reminders:
                    h, m = map(int, reminder_time.split(':'))
                    target_time = user_time.replace(hour=h, minute=m, second=0, microsecond=0)
                    
                    time_diff = (user_time - target_time).total_seconds()
                    if abs(time_diff) < 60:  # Within 1 minute
                        today_str = user_time.date().isoformat()
                        if last_sent[user_id].get(reminder_time) != today_str:
                            await send_reminder(user_id, app)
                            last_sent[user_id][reminder_time] = today_str
                            print(f"Sent reminder to {user_id} at {reminder_time}")
                            
            except Exception as e:
                print(f"Error processing reminders for user {user_id}: {e}")
        
        await asyncio.sleep(30)  # Check every 30 seconds


# =======================
# Bot handlers
# =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Daily Parameter Tracker!\n"
        "Track your daily metrics like mood, energy, motivation, etc.",
        reply_markup=MAIN_MENU
    )

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Clear any conversation state
    if context.user_data:
        context.user_data.clear()
    await update.message.reply_text("⚙️ Settings Menu", reply_markup=SETTINGS_MENU)

# =======================
# Estimate conversation
# =======================
async def estimate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    parameters = get_user_parameters(user_id)
    
    if not parameters:
        await update.message.reply_text(
            "First add parameters to estimate in Settings.", 
            reply_markup=MAIN_MENU
        )
        return ConversationHandler.END
    
    context.user_data['parameters'] = parameters
    context.user_data['current_param'] = 0
    context.user_data['ratings'] = {}
    
    await ask_next_parameter(update, context)
    return ESTIMATE_START

async def ask_next_parameter(update, context):
    params = context.user_data['parameters']
    current = context.user_data['current_param']
    
    if current >= len(params):
        await save_ratings(update, context)
        return ConversationHandler.END
    
    param = params[current]
    keyboard = ReplyKeyboardMarkup([
        ["1", "2", "3", "4", "5"],
        ["6", "7", "8", "9", "10"],
        ["Cancel", "Settings", "Back to Main"]
    ], resize_keyboard=True, one_time_keyboard=True)
    
    await update.message.reply_text(f"Rate {param} (1-10):", reply_markup=keyboard)

async def rating_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    # Check if user wants to navigate away
    if text in ["Cancel", "Settings", "Back to Main"]:
        await cancel(update, context)
        return ConversationHandler.END
    
    if not text.isdigit() or not (1 <= int(text) <= 10):
        await update.message.reply_text("Please enter a number between 1 and 10:")
        return ESTIMATE_START
    
    params = context.user_data['parameters']
    current = context.user_data['current_param']
    param = params[current]
    
    context.user_data['ratings'][param] = int(text)
    context.user_data['current_param'] += 1
    
    await ask_next_parameter(update, context)
    return ESTIMATE_START

async def save_ratings(update, context):
    user_id = update.message.from_user.id
    ratings = context.user_data['ratings']
    today = datetime.now().strftime("%Y-%m-%d")
    
    for param, value in ratings.items():
        add_entry_to_db(user_id, today, param, value)
    
    summary = ", ".join([f"{k}={v}" for k, v in ratings.items()])
    
    # Random donation message (10% probability) 
    donation_text = ""
    if random.random() < 0.1:  # 10% probability
        donation_text = ("\n\n❤️ Love this bot? Support it: https://saymealien.space/donation.html \n"
                        "☕ Creator: @saymealien")
    
    await update.message.reply_text(
        f"✅ Saved for {today}:\n{summary}{donation_text}", 
        reply_markup=MAIN_MENU
    )

# =======================
# Add parameter conversation 
# =======================
async def add_parameter_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['new_params'] = []
    await update.message.reply_text(
        "📊 Choose parameters to track or add custom ones:",
        reply_markup=PARAMETER_MENU
    )
    return ADD_PARAM

async def parameter_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.message.from_user.id
    
    if text in ["Cancel", "Settings", "Back to Main"]:
        await cancel(update, context)
        return ConversationHandler.END
    
    if text == "Finish":
        new_params = context.user_data.get('new_params', [])
        if new_params:
            current_params = get_user_parameters(user_id)
            all_params = sorted(list(set(current_params + new_params)))
            set_user_parameters(user_id, all_params)
            await update.message.reply_text(
                f"✅ Added parameters: {', '.join(new_params)}", 
                reply_markup=SETTINGS_MENU
            )
        else:
            await update.message.reply_text("No new parameters added.", reply_markup=SETTINGS_MENU)
        return ConversationHandler.END
    
    if text == "Custom Parameter":
        await update.message.reply_text(
            "✏️ Enter custom parameter name:",
            reply_markup=ReplyKeyboardMarkup([["Back to Menu", "Cancel"]], resize_keyboard=True)
        )
        context.user_data['awaiting_custom'] = True
        return ADD_PARAM
    
    # Handle custom parameter input
    if context.user_data.get('awaiting_custom'):
        if text in ["Back to Menu", "Cancel"]:
            context.user_data.pop('awaiting_custom', None)
            await update.message.reply_text(
                "📊 Choose parameters to track or add custom ones:",
                reply_markup=PARAMETER_MENU
            )
            return ADD_PARAM
        
        # Add custom parameter
        context.user_data.setdefault('new_params', []).append(text)
        context.user_data.pop('awaiting_custom', None)
        
        queued = context.user_data.get('new_params', [])
        await update.message.reply_text(
            f"✅ Added '{text}' to queue.\n"
            f"📋 Queued: {', '.join(queued)}\n"
            f"Choose more parameters or press Finish:",
            reply_markup=PARAMETER_MENU
        )
        return ADD_PARAM
    
    # Handle preset parameters
    preset_params = [
        "Mood", "Energy", "Sleep Quality", "Stress", 
        "Motivation", "Focus", "Productivity"
    ]
    
    if text in preset_params:
        # Check if already added
        current_queue = context.user_data.get('new_params', [])
        if text in current_queue:
            await update.message.reply_text(
                f"⚠️ '{text}' already in queue.\nChoose another parameter:",
                reply_markup=PARAMETER_MENU
            )
            return ADD_PARAM
        
        context.user_data.setdefault('new_params', []).append(text)
        queued = context.user_data.get('new_params', [])
        
        await update.message.reply_text(
            f"✅ Added '{text}' to queue.\n"
            f"📋 Queued: {', '.join(queued)}\n"
            f"Choose more parameters or press Finish:",
            reply_markup=PARAMETER_MENU
        )
        return ADD_PARAM
    
    # Invalid input
    await update.message.reply_text("Please choose from the menu:", reply_markup=PARAMETER_MENU)
    return ADD_PARAM

# =======================
# Delete parameter conversation
# =======================
async def delete_parameter_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    parameters = get_user_parameters(user_id)
    
    if not parameters:
        await update.message.reply_text("No parameters to delete.", reply_markup=SETTINGS_MENU)
        return ConversationHandler.END
    
    context.user_data['delete_params'] = parameters
    keyboard_buttons = [[param] for param in parameters]
    keyboard_buttons.append(["Cancel", "Back to Main"])
    keyboard = ReplyKeyboardMarkup(keyboard_buttons, resize_keyboard=True, one_time_keyboard=True)
    
    await update.message.reply_text("🗑️ Select parameter to delete:", reply_markup=keyboard)
    return DELETE_PARAM_SELECT

async def delete_parameter_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.message.from_user.id
    
    if text in ["Cancel", "Settings", "Back to Main"]:
        await cancel(update, context)
        return ConversationHandler.END
    
    parameters = get_user_parameters(user_id)
    if text in parameters:
        parameters.remove(text)
        set_user_parameters(user_id, parameters)
        await update.message.reply_text(f"✅ Deleted parameter '{text}'.", reply_markup=SETTINGS_MENU)
    else:
        await update.message.reply_text("Parameter not found.", reply_markup=SETTINGS_MENU)
    
    return ConversationHandler.END

# =======================
# Timezone conversation
# =======================
async def set_timezone_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌍 Choose your timezone or type 'Other' to enter a custom one:",
        reply_markup=TIMEZONE_MENU
    )
    return SET_TIMEZONE

async def timezone_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    user_id = update.message.from_user.id
    
    if user_input in ["Cancel", "Settings", "Back to Main"]:
        await cancel(update, context)
        return ConversationHandler.END
    
    timezone_map = {
        "New York": "America/New_York",
        "London": "Europe/London", 
        "Berlin": "Europe/Berlin",
        "Tokyo": "Asia/Tokyo",
        "Moscow": "Europe/Moscow",
        "Sydney": "Australia/Sydney",
        "Los Angeles": "America/Los_Angeles"
    }
    
    if user_input in timezone_map:
        timezone_name = timezone_map[user_input]
        set_user_timezone(user_id, timezone_name)
        await update.message.reply_text(
            f"✅ Timezone set to {user_input} ({timezone_name})", 
            reply_markup=SETTINGS_MENU
        )
        return ConversationHandler.END
    elif user_input == "Other":
        await update.message.reply_text(
            "🌍 Please enter your timezone (e.g., Europe/Paris):", 
            reply_markup=ReplyKeyboardMarkup([["Cancel", "Back to Main"]], resize_keyboard=True)
        )
        return SET_TIMEZONE
    else:
        if user_input in available_timezones():
            set_user_timezone(user_id, user_input)
            await update.message.reply_text(f"✅ Timezone set to {user_input}", reply_markup=SETTINGS_MENU)
            return ConversationHandler.END
        else:
            matches = difflib.get_close_matches(user_input, available_timezones(), n=5)
            if matches:
                await update.message.reply_text(
                    f"⚠️ Timezone not found. Did you mean?\n- " + "\n- ".join(matches), 
                    reply_markup=TIMEZONE_MENU
                )
            else:
                await update.message.reply_text(
                    "⚠️ Timezone not recognized. Please choose from the menu.", 
                    reply_markup=TIMEZONE_MENU
                )
            return SET_TIMEZONE

# =======================
# Set reminders conversation
# =======================
async def set_reminders_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⏰ Enter reminder time in HH:MM format (e.g., 21:30):",
        reply_markup=ReplyKeyboardMarkup([["Cancel", "Back to Main"]], resize_keyboard=True)
    )
    return SET_REMINDERS

async def reminders_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    user_id = update.message.from_user.id
    
    if user_input in ["Cancel", "Settings", "Back to Main"]:
        await cancel(update, context)
        return ConversationHandler.END
    
    try:
        if len(user_input) == 5 and user_input[2] == ':':
            hours, minutes = map(int, user_input.split(':'))
            if 0 <= hours <= 23 and 0 <= minutes <= 59:
                set_user_reminders(user_id, [user_input])
                user_tz = get_user_timezone(user_id)
                await update.message.reply_text(
                    f"✅ Reminder set for {user_input} daily ({user_tz})!", 
                    reply_markup=SETTINGS_MENU
                )
                return ConversationHandler.END
        raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Invalid format. Please use HH:MM format (e.g., 21:30):")
        return SET_REMINDERS

# =======================
# Export conversation
# =======================
async def export_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    entries = get_entries_from_db(user_id)
    
    if not entries:
        # Show what would be exported (empty with current parameters)
        params = get_user_parameters(user_id)
        if params:
            await update.message.reply_text(
                f"📤 No data yet, but export will include columns:\n"
                f"📊 {', '.join(['Date'] + params)}\n\n"
                f"Choose format:",
                reply_markup=EXPORT_MENU
            )
        else:
            await update.message.reply_text(
                "📤 No parameters set yet. Add parameters first in Settings.",
                reply_markup=MAIN_MENU
            )
            return ConversationHandler.END
    else:
        await update.message.reply_text("📤 Choose export format:", reply_markup=EXPORT_MENU)
    
    return EXPORT_CHOOSE

async def export_format_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fmt = update.message.text.strip().lower()
    
    if fmt in ["cancel", "settings", "back to main"]:
        await cancel(update, context)
        return ConversationHandler.END
    
    if fmt not in ["csv", "xlsx", "pdf"]:
        await update.message.reply_text("❌ Invalid option.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    
    chat_id = update.message.chat_id
    user_id = update.message.from_user.id
    entries = get_entries_from_db(chat_id)
    
    # Always create DataFrame with user parameters as columns
    user_params = get_user_parameters(user_id)
    if not user_params:
        await update.message.reply_text("❌ No parameters set.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    
    if not entries:
        # Create empty DataFrame with correct columns
        columns = ["date"] + user_params
        df = pd.DataFrame(columns=columns)
    else:
        # Convert to pivot table format
        df = pd.DataFrame(entries, columns=["date", "parameter", "value"])
        df = df.pivot_table(index="date", columns="parameter", values="value", aggfunc="first").reset_index()
        df = df.fillna("")
    
    try:
        buffer = BytesIO()
        
        if fmt == "xlsx":
            with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='Daily Tracker')
            buffer.seek(0)
            filename = "daily_tracker.xlsx"
            caption = "📊 Excel format"
        
        elif fmt == "pdf":
            doc = SimpleDocTemplate(buffer, pagesize=A4)
            styles = getSampleStyleSheet()
            elements = [Paragraph("Daily Parameter Tracker", styles["Heading1"])]
            
            data = [df.columns.tolist()]
            if not df.empty:
                data += df.values.tolist()
            else:
                # Add empty row to show structure
                data.append([""] * len(df.columns))
            
            table = Table(data, repeatRows=1)
            table.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.lightblue),
                ('TEXTCOLOR',(0,0),(-1,0),colors.black),
                ('ALIGN',(0,0),(-1,-1),'CENTER'),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('FONTSIZE', (0,0), (-1,0), 12),
                ('FONTSIZE', (0,1), (-1,-1), 10),
                ('BOTTOMPADDING', (0,0), (-1,0), 12),
                ('BACKGROUND', (0,1), (-1,-1), colors.beige),
                ('GRID', (0,0), (-1,-1), 1, colors.black),
            ]))
            elements.append(table)
            doc.build(elements)
            buffer.seek(0)
            filename = "daily_tracker.pdf"
            caption = "📄 PDF format"
        
        else:  # CSV
            df.to_csv(buffer, index=False, encoding='utf-8')
            buffer.seek(0)
            filename = "daily_tracker.csv"
            caption = "📊 CSV format"
        
        await update.message.reply_document(
            document=buffer,
            filename=filename,
            caption=caption,
            reply_markup=MAIN_MENU
        )
    
    except Exception as e:
        await update.message.reply_text(f"❌ Export error: {str(e)}", reply_markup=MAIN_MENU)
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Clear any conversation state
    if context.user_data:
        context.user_data.clear()
    
    text = update.message.text
    
    # If user pressed a main menu button, redirect appropriately
    if text == "Settings":
        await settings_menu(update, context)
    elif text == "Export results":
        await export_start(update, context)
        return EXPORT_CHOOSE
    elif text == "Estimate":
        await estimate_start(update, context)
        return ESTIMATE_START
    elif text == "Back to Main":
        await update.message.reply_text("↩️ Back to main menu", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("❌ Action cancelled.", reply_markup=MAIN_MENU)
    
    return ConversationHandler.END

# =======================
# Main menu handler 
# =======================
async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    if text == "Settings":
        await settings_menu(update, context)
    elif text == "Back to Main":
        await update.message.reply_text("↩️ Back to main menu", reply_markup=MAIN_MENU)
    elif text == "Estimate":
        await estimate_start(update, context)
        return ESTIMATE_START
    elif text == "Export results":
        await export_start(update, context)
        return EXPORT_CHOOSE
    else:
        # Handle any other text that doesn't match conversation patterns
        await update.message.reply_text("Please use the menu buttons:", reply_markup=MAIN_MENU)

# =======================
# Donation handler
# =======================
async def donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "\n\n❤️ Love this bot? Support it: https://saymealien.space/donation.html \n"
                        "☕ Creator: @saymealien",
        reply_markup=MAIN_MENU
    )

# =======================
# Fallback handler - THIS IS THE KEY FIX
# =======================
async def fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
    # Clear any stuck conversation state
    if context.user_data:
        context.user_data.clear()
    
    # Handle main menu buttons regardless of conversation state
    if text == "Settings":
        await settings_menu(update, context)
        return ConversationHandler.END
    elif text == "Back to Main":
        await update.message.reply_text("↩️ Back to main menu", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    elif text == "Estimate":
        await estimate_start(update, context)
        return ESTIMATE_START
    elif text == "Export results":
        await export_start(update, context)
        return EXPORT_CHOOSE
    else:
        await update.message.reply_text(
            "Please use the menu buttons to navigate:",
            reply_markup=MAIN_MENU
        )
        return ConversationHandler.END

# =======================
# MAIN FUNCTION
# =======================
if __name__ == "__main__":
    init_db()
    
    # REPLACE WITH YOUR ACTUAL BOT TOKEN
    TOKEN = "MYAU"
    
    app = Application.builder().token(TOKEN).build()
    
    # Conversation handlers with navigation buttons in keyboards
    export_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^(Export results)$"), export_start)],
        states={
            EXPORT_CHOOSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, export_format_chosen)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^(Settings|Back to Main)$"), cancel)
        ],
        allow_reentry=True
    )
    
    estimate_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^(Estimate)$"), estimate_start)],
        states={
            ESTIMATE_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, rating_received)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^(Settings|Back to Main)$"), cancel)
        ],
        allow_reentry=True
    )
    
    add_param_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^(Add parameter)$"), add_parameter_start)],
        states={
            ADD_PARAM: [MessageHandler(filters.TEXT & ~filters.COMMAND, parameter_received)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^(Settings|Back to Main)$"), cancel)
        ],
        allow_reentry=True
    )
    
    delete_param_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^(Delete parameter)$"), delete_parameter_start)],
        states={
            DELETE_PARAM_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_parameter_selected)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^(Settings|Back to Main)$"), cancel)
        ],
        allow_reentry=True
    )
    
    timezone_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^(Set timezone)$"), set_timezone_start)],
        states={
            SET_TIMEZONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, timezone_received)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^(Settings|Back to Main)$"), cancel)
        ],
        allow_reentry=True
    )
    
    reminders_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^(Set reminders)$"), set_reminders_start)],
        states={
            SET_REMINDERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, reminders_received)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex("^(Settings|Back to Main)$"), cancel)
        ],
        allow_reentry=True
    )
    
    # Add handlers in this order (export first!)
    app.add_handler(export_conv)
    app.add_handler(estimate_conv)
    app.add_handler(add_param_conv) 
    app.add_handler(delete_param_conv)
    app.add_handler(timezone_conv)
    app.add_handler(reminders_conv)
    app.add_handler(CommandHandler("donate", donate))
    app.add_handler(MessageHandler(filters.Regex("^(Donate)$"), donate))
    
    # Command handlers
    app.add_handler(CommandHandler("start", start))
    
    # Main menu navigation handlers - ADD THESE BEFORE FALLBACK
    app.add_handler(MessageHandler(filters.Regex("^(Settings)$"), settings_menu))
    app.add_handler(MessageHandler(filters.Regex("^(Back to Main)$"), main_menu_handler))
    app.add_handler(MessageHandler(filters.Regex("^(Estimate)$"), main_menu_handler))
    app.add_handler(MessageHandler(filters.Regex("^(Export results)$"), main_menu_handler))
    
    # Fallback handler (MUST BE LAST - this catches everything else)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_handler))
    
    # Start reminders in background 
    asyncio.get_event_loop().create_task(schedule_reminders(app))
    
    print("🤖 Bot is starting...")
    app.run_polling()
