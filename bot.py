"""
Bet Slip Telegram Bot
=====================
This bot receives bet slip images via Telegram DM, extracts the data using
OpenAI's Vision API, and logs it to a Google Sheet.

Setup required:
1. Create a Telegram bot via @BotFather and get the token
2. Get an OpenAI API key
3. Set up Google Sheets API credentials (see setup guide)
4. Configure the environment variables below
"""

import os
import json
import base64
import logging
from datetime import datetime
from io import BytesIO

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import gspread
from google.oauth2.service_account import Credentials
import anthropic

# ============================================================================
# CONFIGURATION - Set these as environment variables or edit directly
# ============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_API_KEY")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "YOUR_GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")  # JSON string of service account

# Map Telegram usernames/names to bettor names
BETTOR_NAMES = {
    "Dan_rill": "Danny",
    "dan_rill": "Danny",  # lowercase version just in case
    "Erich": "Erich",
    "Zak": "Zak",
}

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ============================================================================
# GOOGLE SHEETS CONNECTION
# ============================================================================
def get_google_sheet():
    """Connect to Google Sheets and return the worksheet."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    # Parse credentials from environment variable or file
    if GOOGLE_CREDENTIALS_JSON:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    else:
        # Fallback to local file for development
        with open("credentials.json", "r") as f:
            creds_dict = json.load(f)

    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(credentials)

    # Open the sheet and get the "Bet Log" worksheet
    sheet = client.open_by_key(GOOGLE_SHEET_ID)
    return sheet.worksheet("Bet Log")


def append_bet_to_sheet(bet_data: dict):
    """Append a single bet record to the Google Sheet."""
    worksheet = get_google_sheet()

    row = [
        bet_data.get("timestamp", ""),
        bet_data.get("bettor_name", ""),
        bet_data.get("bet_date", ""),
        bet_data.get("sport", ""),
        bet_data.get("bet_type", ""),
        bet_data.get("teams_event", ""),
        bet_data.get("odds", ""),
        bet_data.get("wager_amount", ""),
        bet_data.get("win_loss_amount", ""),
        bet_data.get("net_result", ""),
        bet_data.get("status", ""),
        bet_data.get("raw_text", ""),
        bet_data.get("notes", ""),
    ]

    worksheet.append_row(row, value_input_option="USER_ENTERED")
    logger.info(f"Appended bet to sheet: {bet_data.get('bettor_name')} - ${bet_data.get('wager_amount')}")


# ============================================================================
# CLAUDE VISION - BET SLIP EXTRACTION
# ============================================================================
def extract_bet_data_from_image(image_bytes: bytes) -> dict:
    """
    Send the bet slip image to Claude's Vision API and extract structured data.
    Returns a dictionary with extracted bet information.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Convert image to base64
    base64_image = base64.b64encode(image_bytes).decode("utf-8")

    # Prompt for structured extraction
    extraction_prompt = """Analyze this sports betting slip image and extract the following information.

Return your response as a JSON object with these exact fields:
{
    "bet_date": "YYYY-MM-DD format, the date on the bet slip",
    "sport": "the sport or league (NFL, NBA, MLB, NHL, UFC, Soccer, etc.)",
    "bet_type": "type of bet (Straight, Parlay, Teaser, Prop, Over/Under, Moneyline, Spread, etc.)",
    "teams_event": "the teams or event the bet is on (e.g., 'Lakers vs Celtics' or 'Patrick Mahomes passing yards')",
    "odds": "the odds/line as shown (e.g., -110, +150, -3.5, O/U 45.5)",
    "wager_amount": "numeric value only, the amount wagered (e.g., 100.00)",
    "win_loss_amount": "numeric value only, positive if won, negative if lost, 0 if pending",
    "is_winner": "true/false/pending",
    "confidence": "high/medium/low - how confident you are in the extraction",
    "raw_text": "brief summary of what you can read on the slip",
    "notes": "any issues or unclear parts"
}

Important:
- If you cannot clearly read a value, set confidence to "low" and explain in notes
- For win_loss_amount: use the payout amount minus wager if won, negative wager if lost
- If the bet is still pending/open, set is_winner to "pending" and win_loss_amount to 0
- Extract the actual date from the slip, not today's date
- For parlays, list all legs in teams_event separated by " / "
- Return ONLY the JSON object, no other text"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64_image
                        }
                    },
                    {
                        "type": "text",
                        "text": extraction_prompt
                    }
                ]
            }
        ]
    )

    # Parse the response
    response_text = response.content[0].text

    # Try to extract JSON from the response
    try:
        # Handle cases where response might have markdown code blocks
        if "```json" in response_text:
            json_str = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            json_str = response_text.split("```")[1].split("```")[0]
        else:
            json_str = response_text

        extracted_data = json.loads(json_str.strip())
    except json.JSONDecodeError:
        # If parsing fails, return a needs-review entry
        extracted_data = {
            "bet_date": "",
            "wager_amount": "",
            "win_loss_amount": "",
            "is_winner": "unknown",
            "confidence": "low",
            "raw_text": response_text[:200],
            "notes": "Failed to parse - manual review needed"
        }

    return extracted_data


# ============================================================================
# TELEGRAM BOT HANDLERS
# ============================================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /start command."""
    user = update.effective_user
    await update.message.reply_text(
        f"Hi {user.first_name}! 👋\n\n"
        "I'm the Bet Slip Logger bot. Send me photos of your bet slips "
        "and I'll automatically log them to the spreadsheet.\n\n"
        "Commands:\n"
        "/start - Show this message\n"
        "/status - Check if I'm connected properly\n"
        "/help - Get help with sending bet slips"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /status command - verify connections are working."""
    status_messages = []

    # Check Google Sheets connection
    try:
        worksheet = get_google_sheet()
        row_count = len(worksheet.get_all_values())
        status_messages.append(f"✅ Google Sheets: Connected ({row_count} rows)")
    except Exception as e:
        status_messages.append(f"❌ Google Sheets: Error - {str(e)[:50]}")

    # Check Claude API connection
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        # Simple test call to verify API key works
        client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=10,
            messages=[{"role": "user", "content": "hi"}]
        )
        status_messages.append("✅ Claude API: Connected")
    except Exception as e:
        status_messages.append(f"❌ Claude API: Error - {str(e)[:50]}")

    await update.message.reply_text("\n".join(status_messages))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /help command."""
    await update.message.reply_text(
        "📸 How to send bet slips:\n\n"
        "1. Take a clear photo of the bet slip\n"
        "2. Make sure the amounts and date are visible\n"
        "3. Send the photo directly to me\n"
        "4. I'll confirm when it's logged\n\n"
        "Tips:\n"
        "• Good lighting helps accuracy\n"
        "• Avoid blurry photos\n"
        "• Send one slip per photo for best results"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming photos (bet slips)."""
    user = update.effective_user
    username = user.username or str(user.id)

    # Get bettor name from mapping, or use Telegram name
    bettor_name = BETTOR_NAMES.get(username, user.first_name or username)

    # Send "processing" message
    processing_msg = await update.message.reply_text("📊 Processing bet slip...")

    try:
        # Download the photo (get the highest resolution version)
        photo = update.message.photo[-1]
        photo_file = await photo.get_file()

        # Download to bytes
        photo_bytes = BytesIO()
        await photo_file.download_to_memory(photo_bytes)
        photo_bytes.seek(0)

        # Extract data using OpenAI Vision
        extracted_data = extract_bet_data_from_image(photo_bytes.read())

        # Determine status based on confidence
        if extracted_data.get("confidence") == "low":
            status = "NEEDS REVIEW"
        elif extracted_data.get("is_winner") == "pending":
            status = "PENDING"
        else:
            status = "LOGGED"

        # Calculate net result
        try:
            wager = float(extracted_data.get("wager_amount", 0) or 0)
            win_loss = float(extracted_data.get("win_loss_amount", 0) or 0)
            net_result = win_loss
        except (ValueError, TypeError):
            wager = extracted_data.get("wager_amount", "")
            win_loss = extracted_data.get("win_loss_amount", "")
            net_result = ""

        # Prepare the row data
        bet_data = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "bettor_name": bettor_name,
            "bet_date": extracted_data.get("bet_date", ""),
            "sport": extracted_data.get("sport", ""),
            "bet_type": extracted_data.get("bet_type", ""),
            "teams_event": extracted_data.get("teams_event", ""),
            "odds": extracted_data.get("odds", ""),
            "wager_amount": wager,
            "win_loss_amount": win_loss,
            "net_result": net_result,
            "status": status,
            "raw_text": extracted_data.get("raw_text", "")[:500],
            "notes": extracted_data.get("notes", ""),
        }

        # Append to Google Sheet
        append_bet_to_sheet(bet_data)

        # Send confirmation
        if status == "NEEDS REVIEW":
            await processing_msg.edit_text(
                f"⚠️ Bet logged but NEEDS REVIEW\n\n"
                f"Bettor: {bettor_name}\n"
                f"Date: {bet_data['bet_date'] or 'unclear'}\n"
                f"Sport: {bet_data['sport'] or 'unclear'}\n"
                f"Type: {bet_data['bet_type'] or 'unclear'}\n"
                f"Event: {bet_data['teams_event'] or 'unclear'}\n"
                f"Odds: {bet_data['odds'] or 'unclear'}\n"
                f"Wager: ${wager if wager else 'unclear'}\n"
                f"Result: ${win_loss if win_loss else 'unclear'}\n\n"
                f"Note: {extracted_data.get('notes', 'Some values unclear')}"
            )
        else:
            result_emoji = "🎉" if (isinstance(win_loss, (int, float)) and win_loss > 0) else "📝"
            await processing_msg.edit_text(
                f"{result_emoji} Bet logged successfully!\n\n"
                f"Bettor: {bettor_name}\n"
                f"Date: {bet_data['bet_date']}\n"
                f"Sport: {bet_data['sport']}\n"
                f"Type: {bet_data['bet_type']}\n"
                f"Event: {bet_data['teams_event']}\n"
                f"Odds: {bet_data['odds']}\n"
                f"Wager: ${wager}\n"
                f"Result: ${win_loss}\n"
                f"Status: {status}"
            )

    except Exception as e:
        logger.error(f"Error processing bet slip: {e}")
        await processing_msg.edit_text(
            f"❌ Error processing bet slip\n\n"
            f"Error: {str(e)[:100]}\n\n"
            f"Please try again or contact support."
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages (not photos)."""
    await update.message.reply_text(
        "Please send me a photo of the bet slip. 📸\n\n"
        "I need an image to extract the bet information."
    )


# ============================================================================
# MAIN - BOT STARTUP
# ============================================================================
def main():
    """Start the bot."""
    # Create the Application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Start the bot
    logger.info("Starting Bet Slip Logger bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
