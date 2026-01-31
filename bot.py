"""
Bet Slip Telegram Bot
=====================
This bot receives bet slip images via Telegram DM, extracts the data using
Claude's Vision API, and logs it to a Google Sheet.

Supports multiple photos at once - asks for trader only once for all photos.
"""

import os
import json
import base64
import logging
from datetime import datetime
from io import BytesIO

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
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

    # Get the next row number (current rows + 1)
    next_row = len(worksheet.get_all_values()) + 1

    # Commission formula: if payout/wager >= 2, use 1% of wager; else 1% of profit
    # Commission is owed on ALL bets (win, loss, or push) based on potential payout
    commission_formula = f'=IF(L{next_row}/K{next_row}>=2,K{next_row}*0.01,(L{next_row}-K{next_row})*0.01)'

    # Column order:
    # A: Timestamp, B: Date Placed, C: Trader, D: Bettor, E: Match Date, F: League,
    # G: Teams/Event, H: Selection, I: Bet Type, J: Odds, K: Wager,
    # L: Potential Payout, M: Result, N: Net Result, O: Commission,
    # P: Status, Q: Raw Text, R: Notes
    row = [
        bet_data.get("timestamp", ""),
        bet_data.get("date_placed", ""),
        bet_data.get("trader", ""),
        bet_data.get("bettor_name", ""),
        bet_data.get("match_date", ""),
        bet_data.get("league", ""),
        bet_data.get("teams_event", ""),
        bet_data.get("selection", ""),
        bet_data.get("bet_type", ""),
        bet_data.get("odds", ""),
        bet_data.get("wager_amount", ""),
        bet_data.get("potential_payout", ""),
        bet_data.get("result", ""),
        bet_data.get("net_result", ""),
        commission_formula,  # Commission calculated by formula
        bet_data.get("status", ""),
        bet_data.get("raw_text", ""),
        bet_data.get("notes", ""),
    ]

    worksheet.append_row(row, value_input_option="USER_ENTERED")
    logger.info(f"Appended bet to sheet: {bet_data.get('bettor_name')} - ${bet_data.get('wager_amount')}")


# ============================================================================
# CLAUDE VISION - BET SLIP EXTRACTION (MULTI-SLIP SUPPORT)
# ============================================================================
def extract_bet_data_from_image(image_bytes: bytes) -> list:
    """
    Send the bet slip image to Claude's Vision API and extract structured data.
    Returns a LIST of dictionaries, one for each bet slip found in the image.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Convert image to base64
    base64_image = base64.b64encode(image_bytes).decode("utf-8")

    # Prompt for structured extraction - MULTIPLE SLIPS
    extraction_prompt = """Analyze this image and extract information from ALL bet slips visible.

Return a JSON ARRAY of objects, one for each bet slip found. Each object should have these fields:
{
    "date_placed": "YYYY-MM-DD format, the date the BET WAS PLACED (usually shown near ticket/slip number, NOT the game date)",
    "match_date": "YYYY-MM-DD format, the date of the GAME/MATCH being bet on",
    "league": "the league (NFL, NBA, MLB, NHL, UFC, MLS, Premier League, etc.)",
    "teams_event": "the teams or event (e.g., 'Lakers vs Celtics' or 'Chiefs vs Ravens')",
    "selection": "what we bet ON specifically (e.g., 'Lakers -3.5', 'Over 45.5', 'Chiefs ML', 'Patrick Mahomes 300+ yards')",
    "bet_type": "type of bet (Straight, Parlay, Teaser, Prop, Over/Under, Moneyline, Spread, etc.)",
    "odds": "the odds as shown (e.g., -110, +150, -3.5)",
    "wager_amount": "numeric value only, the amount wagered (e.g., 100.00)",
    "potential_payout": "numeric value only, the total potential payout if bet wins (wager + winnings)",
    "result": "Win/Loss/Push/Pending - the outcome of the bet",
    "confidence": "high/medium/low - how confident you are in the extraction",
    "raw_text": "brief summary of what you can read on this slip",
    "notes": "any issues or unclear parts"
}

IMPORTANT:
- Return a JSON ARRAY even if there's only one slip: [{ ... }]
- Extract EVERY separate bet slip visible in the image
- date_placed is the date the BET WAS PLACED - look for this near the ticket number, slip ID, or at the top of the slip
- match_date is the date of the GAME being bet on
- selection should be the specific pick (team + spread, over/under, moneyline, etc.)
- potential_payout is the TOTAL you'd receive if you win (stake + profit)
- result should be "Win", "Loss", "Push", or "Pending" based on what the slip shows
- If you cannot clearly read a value, set confidence to "low" and explain in notes
- For parlays, list all legs in teams_event and selection separated by " / "
- Return ONLY the JSON array, no other text"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,  # Increased for multiple slips
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

        # Ensure it's a list
        if isinstance(extracted_data, dict):
            extracted_data = [extracted_data]

    except json.JSONDecodeError:
        # If parsing fails, return a needs-review entry as a list
        extracted_data = [{
            "date_placed": "",
            "match_date": "",
            "wager_amount": "",
            "potential_payout": "",
            "result": "Pending",
            "confidence": "low",
            "raw_text": response_text[:200],
            "notes": "Failed to parse - manual review needed"
        }]

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
        "💡 You can send multiple photos at once - I'll ask for the trader once for all of them.\n\n"
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
        "1. Take clear photos of your bet slips\n"
        "2. Send ALL photos at once (or quickly one after another)\n"
        "3. I'll ask who the trader is ONCE for all slips\n"
        "4. I'll process them all and log to the spreadsheet\n\n"
        "Tips:\n"
        "• Good lighting helps accuracy\n"
        "• Avoid blurry photos\n"
        "• Multiple slips per photo is fine too!"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming photos - collects all, asks for trader once."""
    user = update.effective_user
    username = user.username or str(user.id)

    # Get bettor name from mapping, or use Telegram name
    bettor_name = BETTOR_NAMES.get(username, user.first_name or username)

    # Download the photo
    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    photo_bytes = BytesIO()
    await photo_file.download_to_memory(photo_bytes)
    photo_bytes.seek(0)

    # Initialize pending_photos list if not exists
    if 'pending_photos' not in context.user_data:
        context.user_data['pending_photos'] = []

    # Check if this is the first photo (need to ask for trader)
    is_first_photo = len(context.user_data['pending_photos']) == 0

    # Add this photo to the pending list
    context.user_data['pending_photos'].append(photo_bytes.getvalue())
    context.user_data['bettor_name'] = bettor_name

    if is_first_photo:
        # First photo - ask for trader
        keyboard = [
            [
                InlineKeyboardButton("Will", callback_data="trader_Will"),
                InlineKeyboardButton("Serge", callback_data="trader_Serge"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "📸 Got it! Who was the trader?",
            reply_markup=reply_markup
        )
    # Additional photos are silently added - no response needed


async def handle_trader_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle trader selection and process ALL pending bet slips."""
    query = update.callback_query
    await query.answer()

    # Get trader from callback data
    trader = query.data.replace("trader_", "")
    bettor_name = context.user_data.get('bettor_name', 'Unknown')
    pending_photos = context.user_data.get('pending_photos', [])

    if not pending_photos:
        await query.edit_message_text("❌ No photos found. Please send the bet slips again.")
        return

    photo_count = len(pending_photos)
    # Update message to show processing
    await query.edit_message_text(f"📊 Processing {photo_count} photo{'s' if photo_count > 1 else ''}...\nTrader: {trader}")

    try:
        total_logged = 0
        total_review = 0
        total_wagered = 0
        all_results = []

        # Process each photo
        for i, photo_bytes in enumerate(pending_photos):
            # Extract data using Claude Vision - returns a LIST of bets per photo
            extracted_bets = extract_bet_data_from_image(photo_bytes)

            for extracted_data in extracted_bets:
                # Determine status based on confidence
                if extracted_data.get("confidence") == "low":
                    status = "NEEDS REVIEW"
                    total_review += 1
                else:
                    status = "LOGGED"
                    total_logged += 1

                # Calculate net result based on result
                result = extracted_data.get("result", "Pending")
                try:
                    wager = float(extracted_data.get("wager_amount", 0) or 0)
                    potential_payout = float(extracted_data.get("potential_payout", 0) or 0)
                    total_wagered += wager  # Track total wagered

                    if result.lower() == "win":
                        net_result = potential_payout - wager  # Profit
                    elif result.lower() == "loss":
                        net_result = -wager  # Lost the wager
                    elif result.lower() == "push":
                        net_result = 0  # Money back
                    else:
                        net_result = 0  # Pending
                except (ValueError, TypeError):
                    wager = extracted_data.get("wager_amount", "")
                    potential_payout = extracted_data.get("potential_payout", "")
                    net_result = ""

                # Prepare the row data
                bet_data = {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "date_placed": extracted_data.get("date_placed", ""),  # From the slip itself
                    "trader": trader,
                    "bettor_name": bettor_name,
                    "match_date": extracted_data.get("match_date", ""),
                    "league": extracted_data.get("league", ""),
                    "teams_event": extracted_data.get("teams_event", ""),
                    "selection": extracted_data.get("selection", ""),
                    "bet_type": extracted_data.get("bet_type", ""),
                    "odds": extracted_data.get("odds", ""),
                    "wager_amount": wager,
                    "potential_payout": potential_payout,
                    "result": result,
                    "net_result": net_result,
                    "status": status,
                    "raw_text": extracted_data.get("raw_text", "")[:500],
                    "notes": extracted_data.get("notes", ""),
                }

                # Append to Google Sheet
                append_bet_to_sheet(bet_data)

                # Add to summary
                wager_str = f"${wager}" if wager else "?"
                selection_str = extracted_data.get("selection", "Unknown")[:30]
                all_results.append(f"• {selection_str} ({wager_str})")

        # Clear stored photos
        context.user_data.pop('pending_photos', None)
        context.user_data.pop('bettor_name', None)

        # Send confirmation
        total_bets = total_logged + total_review
        summary_text = "\n".join(all_results[:15])
        if len(all_results) > 15:
            summary_text += f"\n... and {len(all_results) - 15} more"

        if total_review > 0:
            await query.edit_message_text(
                f"📊 Processed {photo_count} photo{'s' if photo_count > 1 else ''} → {total_bets} bet{'s' if total_bets > 1 else ''}\n\n"
                f"Trader: {trader}\n"
                f"Bettor: {bettor_name}\n"
                f"💰 Total Wagered: ${total_wagered:,.2f}\n\n"
                f"✅ Logged: {total_logged}\n"
                f"⚠️ Needs Review: {total_review}\n\n"
                f"Bets:\n{summary_text}"
            )
        else:
            await query.edit_message_text(
                f"🎉 Logged {total_bets} bet{'s' if total_bets > 1 else ''} from {photo_count} photo{'s' if photo_count > 1 else ''}!\n\n"
                f"Trader: {trader}\n"
                f"Bettor: {bettor_name}\n"
                f"💰 Total Wagered: ${total_wagered:,.2f}\n\n"
                f"Bets:\n{summary_text}"
            )

    except Exception as e:
        logger.error(f"Error processing bet slips: {e}")
        await query.edit_message_text(
            f"❌ Error processing bet slips\n\n"
            f"Error: {str(e)[:100]}\n\n"
            f"Please try again."
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages (not photos)."""
    await update.message.reply_text(
        "Please send me a photo of the bet slip. 📸\n\n"
        "I need an image to extract the bet information.\n"
        "You can send multiple photos at once!"
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
    application.add_handler(CallbackQueryHandler(handle_trader_selection, pattern="^trader_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Start the bot
    logger.info("Starting Bet Slip Logger bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
