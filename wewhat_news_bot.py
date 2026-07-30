import os
import json
from dotenv import load_dotenv

# Load .env file FIRST
load_dotenv()

import asyncio
import feedparser
import requests
from datetime import datetime, timedelta
import pytz
from anthropic import Anthropic
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request
from gspread import authorize
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ======================
# CONFIGURATION
# ======================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # Your BotFather token
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")  # Anthropic API key
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")  # Your Sheet ID
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")  # Path to JSON file
TELEGRAM_GROUP_ID = os.getenv("TELEGRAM_GROUP_ID")  # Your group chat ID (get this after first bot message)

MALAYSIA_TZ = pytz.timezone("Asia/Kuala_Lumpur")
DIGEST_HOUR = 9  # 9 AM Malaysia time

# News sources
MALAYSIA_SOURCES = {
    "malaysiakini": "https://www.malaysiakini.com/feed",
    "thestar": "https://www.thestar.com.my/feed/",
    "bernama": "https://www.bernama.gov.my/rss",
}

GLOBAL_SOURCES = {
    "reuters": "https://www.reuters.com/world",
    "bbc": "http://feeds.bbc.co.uk/news/world/rss.xml",
    "cna": "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml",
}

# Categories to prioritize
PRIORITY_CATEGORIES = {
    "Malaysian Politics": ["Malaysia", "PM", "parliament", "election", "minister", "government"],
    "Malaysian Business": ["Malaysia", "business", "company", "startup", "entrepreneur", "MYR"],
    "Malaysian Startups": ["Malaysia", "startup", "founder", "venture", "funded"],
    "Government Initiatives": ["Digital Malaysia", "government initiative", "policy", "Malaysia"],
    "E-commerce & Retail": ["e-commerce", "retail", "online shopping", "Shopee", "Lazada", "mall"],
    "Small Business & SMEs": ["SME", "small business", "entrepreneur", "business owner"],
    "Social Issues": ["bullying", "social issue", "activism", "community", "Malaysia"],
    "Education": ["education", "student", "university", "school", "Malaysia"],
    "Environment & Sustainability": ["environment", "sustainability", "climate", "green", "Malaysia"],
    "Fashion & Beauty": ["fashion", "beauty", "designer", "brand", "Malaysia"],
    "Food & Beverage": ["food", "restaurant", "F&B", "culinary", "Malaysia"],
    "Travel & Tourism": ["travel", "tourism", "destination", "Malaysia", "tourist"],
    "Real Estate & Property": ["property", "real estate", "development", "Malaysia"],
    "Wellness & Health": ["health", "wellness", "fitness", "medical"],
    "Automotive": ["car", "automotive", "vehicle", "F1", "motorsport"],
    "Entertainment & Pop Culture": ["entertainment", "celebrity", "film", "music", "pop culture", "Malaysia"],
}

# ======================
# GOOGLE SHEETS SETUP
# ======================

def init_google_sheets():
    """Initialize Google Sheets API connection"""
    if GOOGLE_SERVICE_ACCOUNT_JSON:
        creds = Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_JSON,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
    else:
        logger.error("Google Service Account JSON not found")
        return None
    
    client = authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEETS_ID)
    return sheet.worksheet(0)  # First worksheet

# ======================
# NEWS FETCHING
# ======================

def fetch_news_from_rss(sources):
    """Fetch news from RSS feeds"""
    articles = []
    for source_name, url in sources.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:3]:  # Top 3 from each source
                articles.append({
                    "title": entry.get("title", "No title"),
                    "summary": entry.get("summary", "No summary"),
                    "link": entry.get("link", ""),
                    "source": source_name,
                    "published": entry.get("published", ""),
                })
        except Exception as e:
            logger.error(f"Error fetching from {source_name}: {e}")
    
    return articles

def categorize_and_score_articles(articles):
    """Score articles based on relevance to chosen categories"""
    scored = []
    
    for article in articles:
        title_lower = article["title"].lower()
        summary_lower = article["summary"].lower()
        text = f"{title_lower} {summary_lower}"
        
        score = 0
        matched_categories = []
        
        for category, keywords in PRIORITY_CATEGORIES.items():
            for keyword in keywords:
                if keyword.lower() in text:
                    score += 1
                    if category not in matched_categories:
                        matched_categories.append(category)
        
        # Boost Malaysia sources
        if article["source"] in MALAYSIA_SOURCES:
            score += 5
        
        if score > 0:  # Only include if relevant
            scored.append({
                **article,
                "score": score,
                "matched_categories": matched_categories,
            })
    
    # Sort by score and return top 10
    return sorted(scored, key=lambda x: x["score"], reverse=True)[:10]

# ======================
# CLAUDE SUMMARIZATION
# ======================

def generate_summary_and_talking_points(article):
    """Use Claude to summarize and suggest talking points"""
    client = Anthropic()
    
    prompt = f"""
You are an editorial assistant for WeWhat, a Malaysian digital media platform.

Article:
Title: {article['title']}
Summary: {article['summary']}
Source: {article['source']}

Your task:
1. Provide a brief, digestible summary (2-3 sentences max)
2. Explain how this relates to Malaysians specifically
3. Suggest 3-4 talking points that would be interesting for a Malaysian audience

Format your response as JSON with keys: "summary", "relevance_to_malaysians", "talking_points" (array)

Keep it concise and actionable for editorial decision-making.
"""
    
    message = client.messages.create(
        model="claude-opus-4-1",
        max_tokens=500,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    
    try:
        response_text = message.content[0].text
        # Extract JSON from response
        import re
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception as e:
        logger.error(f"Error parsing Claude response: {e}")
    
    return {
        "summary": article["summary"][:300],
        "relevance_to_malaysians": "Potentially relevant to Malaysian audience",
        "talking_points": ["Story appears noteworthy"]
    }

# ======================
# TELEGRAM BOT HANDLERS
# ======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command"""
    chat_id = update.effective_chat.id
    logger.info(f"Bot started in chat: {chat_id}")
    await update.message.reply_text(
        "WeWhat News Bot initialized! ✅\n"
        f"Your Group Chat ID: {chat_id}\n"
        "Daily digest will arrive at 9 AM Malaysia time."
    )

async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE):
    """Send daily digest"""
    try:
        # Fetch news
        logger.info("Fetching news...")
        malaysia_articles = fetch_news_from_rss(MALAYSIA_SOURCES)
        global_articles = fetch_news_from_rss(GLOBAL_SOURCES)
        all_articles = malaysia_articles + global_articles
        
        # Score and filter
        logger.info("Scoring and filtering articles...")
        top_articles = categorize_and_score_articles(all_articles)
        
        if not top_articles:
            logger.warning("No relevant articles found")
            return
        
        # Prepare digest
        for idx, article in enumerate(top_articles[:10], 1):
            claude_output = generate_summary_and_talking_points(article)
            
            message = (
                f"📰 **Story {idx}/10**\n\n"
                f"**Headline:** {article['title']}\n\n"
                f"**Summary:** {claude_output.get('summary', article['summary'][:200])}\n\n"
                f"**Relevance to Malaysians:** {claude_output.get('relevance_to_malaysians', 'N/A')}\n\n"
                f"**Talking Points:**\n"
            )
            
            for point in claude_output.get('talking_points', []):
                message += f"• {point}\n"
            
            message += f"\n**Source:** {article['source']}\n**Link:** {article['link']}"
            
            # Create inline buttons
            keyboard = [
                [
                    InlineKeyboardButton("✅ Accept", callback_data=f"accept_{idx}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject_{idx}"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Store article data in context for callback
            context.user_data[f"article_{idx}"] = {
                "headline": article['title'],
                "summary": claude_output.get('summary', article['summary'][:200]),
                "relevance": claude_output.get('relevance_to_malaysians', 'N/A'),
                "talking_points": claude_output.get('talking_points', []),
                "source": article['source'],
                "url": article['link'],
                "date": datetime.now(MALAYSIA_TZ).strftime("%Y-%m-%d"),
            }
            
            await context.bot.send_message(
                chat_id=TELEGRAM_GROUP_ID,
                text=message,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            
            # Small delay between messages
            await asyncio.sleep(1)
        
        logger.info("Daily digest sent successfully")
    
    except Exception as e:
        logger.error(f"Error in send_daily_digest: {e}")

async def accept_story(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle accept button"""
    query = update.callback_query
    await query.answer()
    
    story_idx = query.data.split("_")[1]
    article_data = context.user_data.get(f"article_{story_idx}", {})
    
    # Write to Google Sheets
    try:
        ws = init_google_sheets()
        if ws:
            ws.append_row([
                article_data.get("date", ""),
                article_data.get("headline", ""),
                article_data.get("summary", ""),
                article_data.get("relevance", ""),
                article_data.get("source", ""),
                article_data.get("url", ""),
                " | ".join(article_data.get("talking_points", [])),
                "Accepted"
            ])
            logger.info(f"Story {story_idx} added to Google Sheets")
    except Exception as e:
        logger.error(f"Error writing to Sheets: {e}")
    
    await query.edit_message_text(text="✅ Story accepted and added to WeWhat submissions!")

async def reject_story(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle reject button"""
    query = update.callback_query
    await query.answer()
    
    story_idx = query.data.split("_")[1]
    
    await query.edit_message_text(text="❌ Story rejected. Moving on...")

# ======================
# SCHEDULER
# ======================

def schedule_daily_digest(application):
    """Schedule digest for 9 AM Malaysia time every day"""
    job_queue = application.job_queue
    
    # Calculate next 9 AM Malaysia time
    now = datetime.now(MALAYSIA_TZ)
    next_run = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
    
    if next_run <= now:
        next_run += timedelta(days=1)
    
    time_until_run = (next_run - now).total_seconds()
    
    logger.info(f"Next digest scheduled for: {next_run}")
    
    # Schedule immediately for testing, then daily
    job_queue.run_repeating(
        send_daily_digest,
        interval=86400,  # 24 hours
        first=time_until_run
    )

# ======================
# MAIN
# ======================

def main():
    """Start the bot"""
    # Create application
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(accept_story, pattern="^accept_"))
    application.add_handler(CallbackQueryHandler(reject_story, pattern="^reject_"))
    
    # Schedule daily digest
    schedule_daily_digest(application)
    
    # Start bot
    logger.info("Starting WeWhat News Bot...")
    application.run_polling()

if __name__ == "__main__":
    main()
