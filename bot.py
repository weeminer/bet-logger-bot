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
import requests
from datetime import datetime, timedelta
from io import BytesIO
from PIL import Image

# Try to import HEIC support (for iPhone photos sent as files)
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:
    HEIC_SUPPORTED = False

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import anthropic

# ============================================================================
# CONFIGURATION - Set these as environment variables or edit directly
# ============================================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_API_KEY")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "YOUR_GOOGLE_SHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")  # JSON string of service account
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "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")  # Tavily search API
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY", "")  # imgbb API for image uploads

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


# Google Drive folder ID for storing bet slip images (set this to your folder ID)
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")  # Optional: specific folder for images


# ============================================================================
# GOOGLE SHEETS CONNECTION
# ============================================================================
def get_google_credentials():
    """Get Google credentials for Sheets, Drive, and Vision."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/cloud-vision"
    ]

    if GOOGLE_CREDENTIALS_JSON:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    else:
        with open("credentials.json", "r") as f:
            creds_dict = json.load(f)

    return Credentials.from_service_account_info(creds_dict, scopes=scopes)


def extract_text_with_google_vision(image_bytes: bytes) -> str:
    """
    Use Google Cloud Vision API to extract text from an image.
    Uses DOCUMENT_TEXT_DETECTION to preserve layout structure.
    """
    try:
        credentials = get_google_credentials()
        service = build('vision', 'v1', credentials=credentials)

        # Encode image to base64
        image_content = base64.b64encode(image_bytes).decode('utf-8')

        # Call Vision API with DOCUMENT_TEXT_DETECTION for better structure
        request_body = {
            'requests': [{
                'image': {'content': image_content},
                'features': [{'type': 'DOCUMENT_TEXT_DETECTION'}]
            }]
        }

        response = service.images().annotate(body=request_body).execute()

        # Extract the text with structure
        if 'responses' in response and response['responses']:
            full_text_annotation = response['responses'][0].get('fullTextAnnotation', {})
            if full_text_annotation:
                full_text = full_text_annotation.get('text', '')
                logger.info(f"Google Vision extracted {len(full_text)} characters")
                return full_text

            # Fallback to textAnnotations
            annotations = response['responses'][0].get('textAnnotations', [])
            if annotations:
                full_text = annotations[0].get('description', '')
                logger.info(f"Google Vision (fallback) extracted {len(full_text)} characters")
                return full_text

        logger.warning("Google Vision returned no text")
        return ""

    except Exception as e:
        logger.error(f"Google Vision OCR error: {e}")
        return ""


def process_image_for_claude(image_bytes: bytes, max_dimension: int = 2000) -> bytes:
    """
    Process image for Claude API - convert to JPEG and resize for reliable OCR.

    Args:
        image_bytes: Original image bytes
        max_dimension: Maximum width or height in pixels

    Returns:
        Processed JPEG image bytes
    """
    size_mb = len(image_bytes) / (1024 * 1024)
    logger.info(f"Processing image: {size_mb:.2f}MB")

    try:
        # Open with PIL
        img = Image.open(BytesIO(image_bytes))
        width, height = img.size
        original_format = img.format
        logger.info(f"Image format: {original_format}, size: {width}x{height}")

        # Convert to RGB if needed
        if img.mode != 'RGB':
            img = img.convert('RGB')

        # Resize if larger than max dimension
        if width > max_dimension or height > max_dimension:
            if width > height:
                new_width = max_dimension
                new_height = int(height * (max_dimension / width))
            else:
                new_height = max_dimension
                new_width = int(width * (max_dimension / height))
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            logger.info(f"Resized from {width}x{height} to {new_width}x{new_height}")

        # Save to JPEG
        output = BytesIO()
        img.save(output, format='JPEG', quality=95)
        output.seek(0)

        processed_bytes = output.getvalue()
        new_size_mb = len(processed_bytes) / (1024 * 1024)
        logger.info(f"Processed image: {new_size_mb:.2f}MB")

        return processed_bytes

    except Exception as e:
        logger.error(f"Error processing image: {e}")
        return image_bytes


def upload_image_to_imgbb(image_bytes: bytes) -> str:
    """
    Upload an image to imgbb and return a shareable link.

    Args:
        image_bytes: The raw image bytes

    Returns:
        A shareable link to the uploaded image, or empty string if upload fails
    """
    if not IMGBB_API_KEY:
        logger.warning("IMGBB_API_KEY not set, skipping image upload")
        return ""

    try:
        data = {
            'key': IMGBB_API_KEY,
            'image': base64.b64encode(image_bytes).decode('utf-8')
        }

        response = requests.post(
            'https://api.imgbb.com/1/upload',
            data=data,
            timeout=30
        )

        if response.status_code == 200:
            link = response.json()['data']['url']
            logger.info(f"Uploaded image to imgbb: {link}")
            return link
        else:
            logger.error(f"imgbb upload failed: {response.status_code} - {response.text[:100]}")
            return ""

    except Exception as e:
        logger.error(f"Error uploading image to imgbb: {e}")
        return ""


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

    # Net Result formula: Win = payout - wager, Loss = -wager, Push = 0
    net_result_formula = f'=IF(M{next_row}="Win",L{next_row}-K{next_row},IF(M{next_row}="Loss",-K{next_row},IF(M{next_row}="Push",0,0)))'

    # Commission formula: if payout/wager >= 2, use 1% of wager; else 1% of profit
    # Commission is owed on ALL bets (win, loss, or push) based on potential payout
    commission_formula = f'=IF(L{next_row}/K{next_row}>=2,K{next_row}*0.01,(L{next_row}-K{next_row})*0.01)'

    # Odds Check formula: Calculate American odds from payout/wager and compare to extracted odds
    # Decimal odds = Payout / Wager
    # If decimal >= 2: American = (decimal - 1) * 100 (positive odds)
    # If decimal < 2: American = -100 / (decimal - 1) (negative odds)
    # Compare to column J with tolerance of ~5 (for rounding differences)
    odds_check_formula = f'''=LET(
        decimal_odds, L{next_row}/K{next_row},
        calc_american, IF(decimal_odds>=2, (decimal_odds-1)*100, -100/(decimal_odds-1)),
        extracted, VALUE(SUBSTITUTE(SUBSTITUTE(J{next_row},"+",""),",","")),
        diff, ABS(calc_american - extracted),
        IF(diff <= 5, "OK", "CHECK - Calc: "&ROUND(calc_american,0))
    )'''

    # Column order:
    # A: Timestamp, B: Date Placed, C: Trader, D: Bettor, E: Match Date, F: League,
    # G: Teams/Event, H: Selection, I: Bet Type, J: Odds, K: Wager,
    # L: Potential Payout, M: Result, N: Net Result, O: Commission,
    # P: Status, Q: Raw Text, R: Notes, S: Betslip Number, T: Odds Check, U: Image Link,
    # V: Grade Verification (filled by /grade command)
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
        net_result_formula,  # Net Result calculated by formula based on Result column
        commission_formula,  # Commission calculated by formula
        bet_data.get("status", ""),
        bet_data.get("raw_text", ""),
        bet_data.get("notes", ""),
        bet_data.get("betslip_number", ""),  # Column S: Betslip Number
        odds_check_formula,  # Column T: Odds verification
        bet_data.get("image_link", ""),  # Column U: Link to saved image
        "",  # Column V: Grade Verification (filled by /grade command)
    ]

    worksheet.append_row(row, value_input_option="USER_ENTERED")
    logger.info(f"Appended bet to sheet: {bet_data.get('bettor_name')} - ${bet_data.get('wager_amount')}")


# ============================================================================
# BET SLIP EXTRACTION - Google Vision OCR + Claude Parsing
# ============================================================================
def extract_bet_data_from_image(image_bytes: bytes) -> list:
    """
    Extract bet data using two-step process:
    1. Google Cloud Vision extracts the raw text (pure OCR, no hallucination)
    2. Claude parses the text into structured data (text only, no vision)
    """
    # Step 1: Process image for Google Vision
    processed_image = process_image_for_claude(image_bytes)

    # Step 2: Extract text using Google Cloud Vision
    ocr_text = extract_text_with_google_vision(processed_image)

    if not ocr_text:
        logger.error("Google Vision returned no text - cannot process image")
        return [{
            "confidence": "low",
            "notes": "Could not extract text from image",
            "result": "Pending"
        }]

    logger.info(f"OCR Text extracted ({len(ocr_text)} chars):\n{ocr_text[:500]}...")

    # Step 3: Use Claude to parse the extracted text (TEXT ONLY - no image)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    extraction_prompt = f"""Parse this OCR text from sports betting slips. There may be MULTIPLE slips.

OCR TEXT:
\"\"\"
{ocr_text}
\"\"\"

STEP 1 - FIND ALL BETSLIP NUMBERS:
Look for long numbers (11-20 digits) like: 12188711295, 12188710078, 12188756049, etc.
These are unique identifiers for each bet slip. COUNT how many you find.

STEP 2 - FOR EACH BETSLIP NUMBER, EXTRACT ITS DATA:
The OCR text may be jumbled, but each betslip has its own:
- Wager amount (e.g., "$500.00", "$1,000.00")
- Potential payout (e.g., "$2,100.00", "$935.00")
- Odds (e.g., "+320", "-115", "-110")
- Selection - INCLUDE QUARTER/HALF INFO! Examples:
  * "CHI Bulls +17.5 1H" (1st Half spread)
  * "CHI Bulls +2.5 2Q" (2nd Quarter spread)
  * "Over 58.5 1Q" (1st Quarter total)
  * "DEN Nuggets ML" (full game moneyline)
  * "Under 209.5" (full game total)
- Teams (e.g., "DEN Nuggets @ DET Pistons")

STEP 3 - OUTPUT ONE OBJECT PER BETSLIP:
You MUST output exactly as many objects as there are betslip numbers.
If you found 4 betslip numbers, output 4 objects.

FORMAT for each:
{{
    "betslip_number": "exact number like 12188711295",
    "date_placed": "YYYY-MM-DD",
    "match_date": "YYYY-MM-DD",
    "league": "NBA/NCAAB/NFL/NCAAF/MLB/NHL",
    "teams_event": "Team A @ Team B",
    "selection": "the pick WITH period if not full game (e.g., 'Bulls +17.5 1H', 'Over 58.5 1Q', 'Thunder +2.5 2Q')",
    "bet_type": "Spread/Moneyline/Total/Parlay/SGP",
    "odds": "+320 or -115 etc",
    "wager_amount": "number only like 500",
    "potential_payout": "number only like 2100",
    "result": "Pending",
    "confidence": "high/medium/low",
    "raw_text": "key text for THIS slip",
    "notes": "issues if any"
}}

IMPORTANT:
- College teams = NCAAB/NCAAF
- Pro teams (Nuggets, Celtics, etc.) = NBA/NFL
- DO NOT skip any betslip numbers
- DO NOT merge data from different slips
- Return JSON array only"""

    # TEXT-ONLY call to Claude - no image, just parsing the OCR text
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[
            {
                "role": "user",
                "content": extraction_prompt  # Just text, no image
            }
        ]
    )

    # Parse the response
    response_text = response.content[0].text

    try:
        # Handle markdown code blocks
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
        extracted_data = [{
            "result": "Pending",
            "confidence": "low",
            "raw_text": ocr_text[:300],
            "notes": "Failed to parse OCR text - manual review needed"
        }]

    return extracted_data


# ============================================================================
# BET GRADING WITH TAVILY WEB SEARCH
# ============================================================================

def get_pending_bets():
    """Fetch all pending bets from the sheet."""
    worksheet = get_google_sheet()
    all_rows = worksheet.get_all_values()

    pending_bets = []
    # Skip header row, find rows where Result (column M, index 12) is "Pending"
    for row_num, row in enumerate(all_rows[1:], start=2):  # start=2 because row 1 is header
        if len(row) > 12 and row[12].lower() == "pending":
            pending_bets.append({
                "row_num": row_num,
                "match_date": row[4] if len(row) > 4 else "",
                "league": row[5] if len(row) > 5 else "",
                "teams_event": row[6] if len(row) > 6 else "",
                "selection": row[7] if len(row) > 7 else "",
                "bet_type": row[8] if len(row) > 8 else "",
                "odds": row[9] if len(row) > 9 else "",
                "wager": row[10] if len(row) > 10 else "",
                "potential_payout": row[11] if len(row) > 11 else "",
            })

    return pending_bets


def search_game_result(bet: dict) -> str:
    """Search for game result using Tavily API."""
    # Build search query from bet info
    match_date = bet['match_date']
    league = bet['league']
    teams = bet['teams_event']
    selection = bet['selection']

    # Check if it's a quarter/half bet
    is_partial = any(x in selection.lower() for x in ['1q', '2q', '3q', '4q', '1h', '2h', 'first quarter', 'first half', 'second half'])

    if is_partial:
        query = f"{teams} {league} {match_date} box score quarter by quarter"
    else:
        query = f"{teams} {league} {match_date} final score result"

    logger.info(f"Searching Tavily for: {query}")

    try:
        response = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "advanced",
                "include_answer": True,
                "max_results": 5
            },
            timeout=30
        )
        response.raise_for_status()
        data = response.json()

        # Combine the answer and top results
        result_text = ""
        if data.get('answer'):
            result_text += f"Summary: {data['answer']}\n\n"

        for r in data.get('results', [])[:3]:
            result_text += f"Source: {r.get('title', '')}\n{r.get('content', '')}\n\n"

        return result_text if result_text else "No results found"

    except Exception as e:
        logger.error(f"Tavily search error: {e}")
        return f"Search error: {str(e)}"


def grade_bet_with_search(bet: dict, search_results: str) -> dict:
    """Use Claude to grade a bet based on search results."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    grading_prompt = f"""Grade this sports bet based on the search results provided.

BET DETAILS:
- Match Date: {bet['match_date']}
- League: {bet['league']}
- Teams/Event: {bet['teams_event']}
- Selection: {bet['selection']}
- Bet Type: {bet['bet_type']}

SEARCH RESULTS:
{search_results}

GRADING RULES:

SPREAD BETS - FOLLOW THESE STEPS:
1. Identify the TEAM the bet is on (from the selection)
2. Find both teams' final scores

FOR POSITIVE SPREADS (underdog getting points, e.g., "Magic +36.5"):
- Calculate: How many points did bet team LOSE by? (Opponent score - Bet team score)
- If bet team WON the game outright → BET WINS
- If bet team LOST: Compare loss margin to spread number
  * Loss margin < spread number → WIN (covered the spread)
  * Loss margin > spread number → LOSS (didn't cover)
  * Loss margin = spread number → PUSH
- Examples:
  * Magic +36.5, score Magic 92 - Thunder 128. Lost by 36. Is 36 < 36.5? YES → WIN
  * Jazz +8, score Jazz 124 - Suns 140. Lost by 16. Is 16 < 8? NO → LOSS
  * Pacers +8, score Pacers 100 - Celtics 108. Lost by 8. Is 8 < 8? NO (equal) → PUSH
  * Bulls +5, score Bulls 110 - Heat 105. Bulls WON outright → WIN

FOR NEGATIVE SPREADS (favorite giving points, e.g., "Warriors -8"):
- Calculate: How many points did bet team WIN by? (Bet team score - Opponent score)
- If bet team LOST the game → BET LOSES
- If bet team WON: Compare win margin to spread number
  * Win margin > spread number → WIN (covered the spread)
  * Win margin < spread number → LOSS (didn't cover)
  * Win margin = spread number → PUSH
- Examples:
  * Warriors -8, score Warriors 140 - Jazz 124. Won by 16. Is 16 > 8? YES → WIN
  * Lakers -6, score Lakers 110 - Kings 108. Won by 2. Is 2 > 6? NO → LOSS
  * Celtics -10, score Celtics 120 - Nets 110. Won by 10. Is 10 > 10? NO (equal) → PUSH
  * Bucks -5, score Bucks 98 - Heat 102. Bucks LOST → LOSS

OVER/UNDER - FOLLOW THESE STEPS:
1. Add both teams' scores: Team1 + Team2 = total
2. OVER wins if total > line, loses if total < line, PUSH if equal
3. UNDER wins if total < line, loses if total > line, PUSH if equal
   * Example: Over 237, score 123-113. Total = 123+113 = 236. Is 236 > 237? NO → LOSS
   * Example: Over 264, score 140-124. Total = 140+124 = 264. Is 264 > 264? NO (equal) → PUSH
   * Example: Under 237, score 123-113. Total = 236. Is 236 < 237? YES → WIN

MONEYLINE (ML): Just check which team won the game

1Q/1H BETS: Use ONLY the 1st quarter or 1st half scores, not the final score

If you cannot determine the result from the search, return "Pending"

IMPORTANT: ALWAYS show your math step-by-step in the reasoning field!

Return ONLY a JSON object:
{{
    "result": "Win" or "Loss" or "Push" or "Pending",
    "final_score": "the score used for grading (e.g., 'Spurs 123 - Pacers 113')",
    "reasoning": "SHOW YOUR MATH for over/under bets (e.g., '123 + 113 = 236 < 237, Over loses')",
    "confidence": "high" or "medium" or "low",
    "verification_details": "Specific score/stats for this bet type - see examples below"
}}

VERIFICATION_DETAILS EXAMPLES (be specific to the bet type):
- 1Q Total bet: "1Q: PHI 31 - GSW 32, Total: 63"
- 1H Spread bet: "1H: CHI 45 - MIL 58, Margin: -13"
- Full game Total: "Final: LAL 110 - BOS 105, Total: 215"
- Full game Spread: "Final: UTA 98 - IND 112, Margin: -14"
- Moneyline: "Final: DEN 118 - DET 105, DEN wins"
- Player prop (points): "Ace Bailey: 18 pts (line was O15.5)"
- Player prop (3PM): "Jay Huff: 2 3PM (line was U2.5)"
- Parlay with props: "Ace Bailey: 18 pts | Jay Huff: 2 3PM"

The verification_details should be a SHORT summary that lets someone quickly verify the grade."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": grading_prompt}]
        )

        response_text = response.content[0].text

        # Parse JSON
        if "```json" in response_text:
            json_str = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            json_str = response_text.split("```")[1].split("```")[0]
        else:
            json_str = response_text

        return json.loads(json_str.strip())

    except Exception as e:
        logger.error(f"Error grading bet with Claude: {e}")
        return {
            "result": "Pending",
            "confidence": "low",
            "final_score": "",
            "reasoning": f"Error grading: {str(e)[:50]}"
        }


def verify_grade(bet: dict, initial_grade: dict, search_results: str) -> dict:
    """Double-check the grading with a verification call."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    verify_prompt = f"""VERIFICATION CHECK: Please verify if this bet was graded correctly.

BET DETAILS:
- Teams/Event: {bet['teams_event']}
- Selection: {bet['selection']}
- Bet Type: {bet['bet_type']}

SEARCH RESULTS:
{search_results}

INITIAL GRADE:
- Result: {initial_grade.get('result')}
- Score: {initial_grade.get('final_score')}
- Reasoning: {initial_grade.get('reasoning')}

YOUR TASK:
1. Find the actual score in the search results
2. Re-calculate whether the bet won or lost using these rules:

SPREAD BETS:
FOR POSITIVE SPREADS (e.g., "Magic +36.5"):
- How many points did bet team LOSE by? (Opponent - Bet team)
- If bet team WON outright → BET WINS
- If bet team LOST: Is loss margin < spread number? YES → WIN, NO → LOSS, EQUAL → PUSH
  * Magic +36.5, Magic 92 - Thunder 128. Lost by 36. Is 36 < 36.5? YES → WIN
  * Jazz +8, Jazz 124 - Suns 140. Lost by 16. Is 16 < 8? NO → LOSS

FOR NEGATIVE SPREADS (e.g., "Warriors -8"):
- How many points did bet team WIN by? (Bet team - Opponent)
- If bet team LOST → BET LOSES
- If bet team WON: Is win margin > spread number? YES → WIN, NO → LOSS, EQUAL → PUSH
  * Warriors -8, Warriors 140 - Jazz 124. Won by 16. Is 16 > 8? YES → WIN
  * Lakers -6, Lakers 110 - Kings 108. Won by 2. Is 2 > 6? NO → LOSS

OVER/UNDER:
- Add both scores: total = score1 + score2
- Over wins if total > line, PUSH if equal
- Under wins if total < line, PUSH if equal

3. Compare your answer to the initial grade
4. SHOW YOUR MATH

Return JSON:
{{
    "verified_result": "Win" or "Loss" or "Push" or "Pending",
    "actual_score": "the score you found",
    "your_math": "show your calculation",
    "agrees_with_initial": true or false,
    "confidence": "high" or "medium" or "low",
    "verification_details": "Specific score/stats (e.g., '1Q: PHI 31 - GSW 32, Total: 63' or 'Ace Bailey: 18 pts')"
}}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": verify_prompt}]
        )

        response_text = response.content[0].text

        if "```json" in response_text:
            json_str = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            json_str = response_text.split("```")[1].split("```")[0]
        else:
            json_str = response_text

        return json.loads(json_str.strip())

    except Exception as e:
        logger.error(f"Verification error: {e}")
        return {"verified_result": initial_grade.get('result'), "agrees_with_initial": True, "confidence": "low"}


def grade_bet(bet: dict) -> dict:
    """Main function to grade a single bet using Tavily search with verification."""
    # Search for the game result
    search_results = search_game_result(bet)

    if "error" in search_results.lower() or search_results == "No results found":
        return {
            "result": "Pending",
            "confidence": "low",
            "final_score": "",
            "reasoning": "Could not find game results"
        }

    # Initial grading
    initial_grade = grade_bet_with_search(bet, search_results)

    # Skip verification for pending results
    if initial_grade.get('result', '').lower() == 'pending':
        return initial_grade

    # Verification step
    verification = verify_grade(bet, initial_grade, search_results)

    # If verification agrees, use the initial grade
    if verification.get('agrees_with_initial', False):
        initial_grade['confidence'] = 'high'
        initial_grade['reasoning'] += f" [VERIFIED: {verification.get('your_math', '')}]"
        # Use verification_details from verification if available, otherwise from initial grade
        if verification.get('verification_details'):
            initial_grade['verification_details'] = verification.get('verification_details')
        return initial_grade

    # If verification disagrees, use the verified result but flag it
    logger.warning(f"Grade verification mismatch for {bet['selection']}: initial={initial_grade.get('result')}, verified={verification.get('verified_result')}")

    return {
        "result": verification.get('verified_result', 'Pending'),
        "final_score": verification.get('actual_score', initial_grade.get('final_score', '')),
        "reasoning": f"CORRECTED: {verification.get('your_math', '')}",
        "confidence": verification.get('confidence', 'medium'),
        "verification_details": verification.get('verification_details', initial_grade.get('verification_details', ''))
    }


def update_bet_result(row_num: int, result: str, notes: str, verification_details: str = ""):
    """Update a bet's result in the sheet. Net Result is calculated by formula."""
    worksheet = get_google_sheet()

    # Column M (13) = Result, Column R (18) = Notes, Column V (22) = Verification Details
    # Note: Column N (Net Result) has a formula that auto-calculates based on Result
    worksheet.update_cell(row_num, 13, result)  # Result - this triggers the Net Result formula

    # Append grading notes to existing notes
    existing_notes = worksheet.cell(row_num, 18).value or ""
    new_notes = f"{existing_notes} | GRADED: {notes}" if existing_notes else f"GRADED: {notes}"
    worksheet.update_cell(row_num, 18, new_notes[:500])  # Notes (truncate if too long)

    # Write verification details to column V (22)
    if verification_details:
        worksheet.update_cell(row_num, 22, verification_details)


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
        "/grade - Grade all pending bets\n"
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
        "Commands:\n"
        "/grade - Grade all pending bets\n\n"
        "📌 FOR BETTER QUALITY:\n"
        "Send images as FILES instead of photos!\n"
        "• iOS: Tap 📎 → File → select image\n"
        "• Android: Tap 📎 → File → Gallery\n"
        "This prevents Telegram compression.\n\n"
        "Tips:\n"
        "• Good lighting helps accuracy\n"
        "• Avoid blurry photos\n"
        "• Multiple slips per photo is fine too!"
    )


async def grade_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /grade command - grade all pending bets."""
    await update.message.reply_text("🔍 Fetching pending bets...")

    try:
        # Get all pending bets
        pending_bets = get_pending_bets()

        if not pending_bets:
            await update.message.reply_text("✅ No pending bets to grade!")
            return

        await update.message.reply_text(
            f"📊 Found {len(pending_bets)} pending bet(s). Grading now...\n"
            "Searching for game results..."
        )

        graded = []
        errors = []
        not_found = []

        for bet in pending_bets:
            try:
                # Grade the bet using The Odds API
                grade_result = grade_bet(bet)

                result = grade_result.get("result", "Pending")

                # Track games that couldn't be found
                if result.lower() == "pending":
                    reason = grade_result.get('reasoning', '')
                    if 'not found' in reason.lower() or 'unknown league' in reason.lower():
                        not_found.append(f"• {bet['teams_event'][:30]} ({bet['league']})")
                    continue

                # Update the sheet (Net Result is calculated by formula based on Result)
                notes = f"{grade_result.get('final_score', '')} - {grade_result.get('reasoning', '')}"
                verification_details = grade_result.get('verification_details', '')
                update_bet_result(bet['row_num'], result, notes, verification_details)

                # Track for summary
                confidence = grade_result.get('confidence', 'unknown')
                graded.append({
                    "selection": bet['selection'][:25],
                    "result": result,
                    "confidence": confidence,
                    "score": grade_result.get('final_score', 'N/A'),
                })

            except Exception as e:
                errors.append(f"{bet['selection'][:20]}: {str(e)[:30]}")
                logger.error(f"Error grading bet {bet['selection']}: {e}")

        # Send summary
        if graded:
            summary_lines = []
            for g in graded[:20]:
                emoji = "✅" if g['result'] == "Win" else "❌" if g['result'] == "Loss" else "➖"
                conf = "⚠️" if g['confidence'] == "low" else ""
                summary_lines.append(f"{emoji} {g['selection']} → {g['result']} {conf}\n   Score: {g['score']}")

            summary = "\n".join(summary_lines)
            if len(graded) > 20:
                summary += f"\n... and {len(graded) - 20} more"

            wins = sum(1 for g in graded if g['result'] == "Win")
            losses = sum(1 for g in graded if g['result'] == "Loss")
            pushes = sum(1 for g in graded if g['result'] == "Push")

            await update.message.reply_text(
                f"🎯 Graded {len(graded)} bet(s)!\n\n"
                f"Wins: {wins} | Losses: {losses} | Pushes: {pushes}\n\n"
                f"{summary}"
            )
        else:
            await update.message.reply_text(
                "ℹ️ No bets were graded.\n"
                "Either games haven't been played yet, or results couldn't be found."
            )

        if not_found:
            not_found_text = "\n".join(not_found[:10])
            if len(not_found) > 10:
                not_found_text += f"\n... and {len(not_found) - 10} more"
            await update.message.reply_text(
                f"🔍 Couldn't match {len(not_found)} game(s):\n{not_found_text}\n\n"
                "Check team names in your sheet match the API format."
            )

        if errors:
            error_text = "\n".join(errors[:5])
            await update.message.reply_text(f"⚠️ Some errors occurred:\n{error_text}")

    except Exception as e:
        logger.error(f"Error in grade command: {e}")
        await update.message.reply_text(f"❌ Error grading bets: {str(e)[:100]}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming photos - collects all, asks for trader once."""
    user = update.effective_user
    username = user.username or str(user.id)

    # Get bettor name from mapping, or use Telegram name
    bettor_name = BETTOR_NAMES.get(username, user.first_name or username)

    # Download the photo - get the largest available size
    photo = update.message.photo[-1]  # -1 is largest
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
                InlineKeyboardButton("PYR", callback_data="trader_PYR"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "📸 Got it! Who was the trader?",
            reply_markup=reply_markup
        )
    # Additional photos are silently added - no response needed


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming documents/files (for uncompressed images)."""
    user = update.effective_user
    username = user.username or str(user.id)

    # Get bettor name from mapping, or use Telegram name
    bettor_name = BETTOR_NAMES.get(username, user.first_name or username)

    # Check if it's an image file
    document = update.message.document
    mime_type = document.mime_type or ""

    if not mime_type.startswith("image/"):
        await update.message.reply_text(
            "Please send image files only (jpg, png, etc.)"
        )
        return

    # Download the file (uncompressed)
    doc_file = await document.get_file()
    file_bytes = BytesIO()
    await doc_file.download_to_memory(file_bytes)
    file_bytes.seek(0)

    # Initialize pending_photos list if not exists
    if 'pending_photos' not in context.user_data:
        context.user_data['pending_photos'] = []

    # Check if this is the first photo (need to ask for trader)
    is_first_photo = len(context.user_data['pending_photos']) == 0

    # Add this photo to the pending list
    context.user_data['pending_photos'].append(file_bytes.getvalue())
    context.user_data['bettor_name'] = bettor_name

    if is_first_photo:
        # First photo - ask for trader
        keyboard = [
            [
                InlineKeyboardButton("Will", callback_data="trader_Will"),
                InlineKeyboardButton("Serge", callback_data="trader_Serge"),
                InlineKeyboardButton("PYR", callback_data="trader_PYR"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "📸 Got it! (File received - better quality) Who was the trader?",
            reply_markup=reply_markup
        )
    # Additional files are silently added - no response needed


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
            # Upload image to imgbb
            image_link = upload_image_to_imgbb(photo_bytes)

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
                    "betslip_number": extracted_data.get("betslip_number", ""),
                    "image_link": image_link,  # Link to uploaded image in Drive
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
    application.add_handler(CommandHandler("grade", grade_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.IMAGE, handle_document))  # Handle image files
    application.add_handler(CallbackQueryHandler(handle_trader_selection, pattern="^trader_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Start the bot
    logger.info("Starting Bet Slip Logger bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
