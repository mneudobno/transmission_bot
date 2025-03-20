#!/usr/bin/env python3
"""
Telegram Bot for searching torrents via Jackett and adding them to Transmission
Author: Created for mneudobno
Date: 2025-03-20
"""

import logging
import os
import time
import json
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)
import transmission_rpc
from typing import Dict, List
from datetime import datetime

# Configure enhanced logging
log_format = '%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s'
logging.basicConfig(
    format=log_format,
    level=logging.INFO,
    handlers=[
        logging.FileHandler("/home/pi/dev/telegram_transmission/bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# States for conversation
SEARCH, SELECT_TORRENT = range(2)

# Configuration from environment variables
TRANSMISSION_HOST = 'localhost'
TRANSMISSION_PORT = 9091
TRANSMISSION_USER = os.environ.get('TRANSMISSION_USER', '')
TRANSMISSION_PASSWORD = os.environ.get('TRANSMISSION_PASSWORD', '')

# Jackett configuration
JACKETT_URL = "http://localhost:9117"
JACKETT_API_KEY = os.environ.get('JACKETT_API_KEY', '')

# Telegram token
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')

# Parse allowed users from env var (comma-separated list)
allowed_users_str = os.environ.get('ALLOWED_TELEGRAM_USERS', '')
ALLOWED_USERS = [int(user_id.strip()) for user_id in allowed_users_str.split(',') if user_id.strip()]

# Cache for search results
search_results_cache: Dict[int, List[Dict]] = {}

# Function to check if a user is allowed
def is_user_allowed(user_id):
    return user_id in ALLOWED_USERS

# Middleware to check user permissions
async def check_user(update: Update):
    user_id = update.effective_user.id
    username = update.effective_user.username
    logger.info(f"Access attempt by user: {username} (ID: {user_id})")
    
    if not is_user_allowed(user_id):
        logger.warning(f"Unauthorized access attempt by user: {username} (ID: {user_id})")
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")
        return False
    
    logger.info(f"Authorized access by user: {username} (ID: {user_id})")
    return True

# Initialize Transmission client
def init_transmission():
    logger.info(f"Initializing Transmission client at {TRANSMISSION_HOST}:{TRANSMISSION_PORT}")
    try:
        client = transmission_rpc.Client(
            host=TRANSMISSION_HOST,
            port=TRANSMISSION_PORT,
            username=TRANSMISSION_USER,
            password=TRANSMISSION_PASSWORD
        )
        logger.info("Successfully connected to Transmission")
        return client
    except Exception as e:
        logger.error(f"Failed to connect to Transmission: {e}")
        raise

# Jackett search function
async def search_jackett(query):
    """Search for torrents using Jackett API"""
    logger.info(f"Searching Jackett for: {query}")
    try:
        # Prepare parameters
        params = {
            "apikey": JACKETT_API_KEY,
            "Query": query,
            "Category": []  # Empty list means all categories
        }
        
        # Make request to Jackett
        url = f"{JACKETT_URL}/api/v2.0/indexers/all/results"
        response = requests.get(url, params=params)
        
        if response.status_code != 200:
            logger.error(f"Jackett API error: {response.status_code} - {response.text}")
            return []
        
        data = response.json()
        results = data.get('Results', [])
        logger.info(f"Found {len(results)} results from Jackett")
        
        # Sort results by seeders (highest first)
        results = sorted(results, key=lambda x: x.get('Seeders', 0), reverse=True)
        # Format results
        formatted_results = []
        for result in results[:10]:  # Limit to 10 results
            # Get magnet link or download link
            magnet = result.get('MagnetUri', '')
            if not magnet and 'Link' in result:
                magnet = result.get('Link', '')
                
            formatted_results.append({
                'id': result.get('Guid', ''),
                'title': result.get('Title', 'Unknown'),
                'size': result.get('Size', 0),
                'seeds': result.get('Seeders', 0),
                'peers': result.get('Peers', 0),
                'magnet': magnet,
                'tracker': result.get('Tracker', 'Unknown')
            })
            
        return formatted_results
        
    except Exception as e:
        logger.error(f"Error searching with Jackett: {e}", exc_info=True)
        return []

# Format file size to human-readable format
def format_size(size_bytes):
    """Format size in bytes to human readable format"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes/1024:.2f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes/(1024*1024):.2f} MB"
    else:
        return f"{size_bytes/(1024*1024*1024):.2f} GB"

# Define command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Check if user is allowed
    if not await check_user(update):
        return ConversationHandler.END
    
    username = update.effective_user.username
    logger.info(f"Start command received from {username}")
    
    message = ('Hi! I can help you search for torrents and add them to Transmission.\n\n'
               'Send me a search query to get started.\n\n'
               'Commands:\n'
               '/start - Start the bot\n'
               '/cancel - Cancel current operation\n'
               '/status - Check Transmission status')
    
    await update.message.reply_text(message)
    return SEARCH

async def search_torrent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Check if user is allowed
    if not await check_user(update):
        return ConversationHandler.END
    
    query = update.message.text
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    logger.info(f"Search query received from {username}: '{query}'")
    
    # Send a "searching" message
    search_message = await update.message.reply_text(f"🔎 Searching for: {query}...")
    
    try:
        # Search for torrents using Jackett
        results = await search_jackett(query)
        
        if not results:
            logger.info(f"No search results found for query: '{query}'")
            await search_message.edit_text(
                "No results found. Please try a different search term."
            )
            return SEARCH
        
        # Cache the search results for this user
        search_results_cache[user_id] = results
        
        # Create keyboard with search results
        keyboard = []
        for i, torrent in enumerate(results):
            # Format size
            size_str = format_size(torrent.get('size', 0))
                
            # Format button text: Title (Size) [Tracker] - Seeds/Peers
            button_text = f"{i+1}. {torrent.get('title', 'Unknown')} ({size_str}) [{torrent.get('tracker', '?')}] - {torrent.get('seeds', '?')}/{torrent.get('peers', '?')}"
            
            # Log each result
            logger.debug(f"Result {i+1}: {torrent.get('title', 'Unknown')}")
            
            # Truncate button text if too long
            if len(button_text) > 80:
                button_text = button_text[:77] + "..."
                
            keyboard.append([InlineKeyboardButton(button_text, callback_data=str(i))])
        
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        logger.info(f"Sending search results to user {username}")
        await search_message.edit_text(
            "Please select a torrent to download:",
            reply_markup=reply_markup
        )
        return SELECT_TORRENT
        
    except Exception as e:
        logger.error(f"Error searching torrents: {e}", exc_info=True)
        await search_message.edit_text(
            f"❌ An error occurred while searching: {str(e)[:200]}... Please try again later."
        )
        return SEARCH

async def select_torrent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    if query.data == "cancel":
        logger.info(f"User {username} cancelled the search")
        await query.edit_message_text("Search cancelled. Send me a new search query.")
        return SEARCH
    
    try:
        # Get selected torrent from cache
        torrent_index = int(query.data)
        user_results = search_results_cache.get(user_id, [])
        
        if not user_results or torrent_index >= len(user_results):
            logger.warning(f"User {username} made invalid selection: {query.data}")
            await query.edit_message_text("Invalid selection. Please try searching again.")
            return SEARCH
        
        selected_torrent = user_results[torrent_index]
        torrent_title = selected_torrent.get('title', 'Unknown')
        magnet_link = selected_torrent.get('magnet', '')
        
        if not magnet_link:
            logger.error(f"No magnet link found for torrent: {torrent_title}")
            await query.edit_message_text("❌ No magnet link found for this torrent. Please try another one.")
            return SEARCH
        
        logger.info(f"User {username} selected torrent: '{torrent_title}'")
        
        # Send message that we're adding the torrent
        await query.edit_message_text(f"⏳ Adding torrent to Transmission: {torrent_title}...")
        
        # Initialize Transmission client
        transmission_client = init_transmission()
        
        # Add the torrent to Transmission
        logger.info(f"Adding torrent to Transmission: '{torrent_title}'")
        transmission_client.add_torrent(magnet_link)
        
        # Get current date and time for the log
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Log the successful addition
        logger.info(f"Successfully added torrent to Transmission at {now}: '{torrent_title}'")
        
        # Send confirmation
        await query.edit_message_text(
            f"✅ Torrent added to Transmission!\n\n"
            f"Title: {torrent_title}\n"
            f"Size: {format_size(selected_torrent.get('size', 0))}\n"
            f"Tracker: {selected_torrent.get('tracker', 'Unknown')}\n"
            f"Added at: {now}"
        )
        
        # Clear cache for this user
        search_results_cache.pop(user_id, None)
        
        return SEARCH
        
    except transmission_rpc.error.TransmissionError as e:
        logger.error(f"Transmission error: {e}", exc_info=True)
        await query.edit_message_text(f"❌ Failed to add torrent to Transmission: {str(e)[:200]}... Please try again later.")
        return SEARCH
    except Exception as e:
        logger.error(f"Error selecting torrent: {e}", exc_info=True)
        await query.edit_message_text(f"❌ An error occurred while processing your selection: {str(e)[:200]}... Please try again.")
        return SEARCH

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show status of Transmission torrents"""
    # Check if user is allowed
    if not await check_user(update):
        return ConversationHandler.END
    
    username = update.effective_user.username
    logger.info(f"Status command received from {username}")
    
    try:
        # Initialize Transmission client
        transmission_client = init_transmission()
        
        # Get all torrents
        torrents = transmission_client.get_torrents()
        
        if not torrents:
            await update.message.reply_text("No torrents in Transmission.")
            return SEARCH
        
        # Create status message
        message = "📥 Current Transmission Torrents:\n\n"
        
        for i, torrent in enumerate(torrents[:10], 1):  # Limit to 10 torrents
            # Calculate percentage
            percent_done = torrent.percent_done * 100
            
            # Get status
            status = torrent.status
            
            # Create status emoji
            if status == 'downloading':
                emoji = "⬇️"
            elif status == 'seeding':
                emoji = "⬆️"
            elif status == 'stopped':
                emoji = "⏹️"
            elif status == 'checking':
                emoji = "🔍"
            else:
                emoji = "❓"
            
            # Format size
            size_bytes = torrent.total_size
            size_str = format_size(size_bytes)
            
            # Add to message
            message += f"{i}. {emoji} {torrent.name[:40]}{'...' if len(torrent.name) > 40 else ''}\n"
            message += f"   • Status: {status.capitalize()} ({percent_done:.1f}%)\n"
            message += f"   • Size: {size_str}\n"
            message += f"   • Speed: ⬇️ {format_size(torrent.rate_download)}/s ⬆️ {format_size(torrent.rate_upload)}/s\n\n"
        
        if len(torrents) > 10:
            message += f"...and {len(torrents) - 10} more torrents."
        
        await update.message.reply_text(message)
        return SEARCH
        
    except Exception as e:
        logger.error(f"Error getting Transmission status: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Failed to get Transmission status: {str(e)[:200]}...")
        return SEARCH

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    logger.info(f"User {username} manually cancelled the operation")
    
    # Clear any cached results for this user
    search_results_cache.pop(user_id, None)
    
    await update.message.reply_text("Operation cancelled. Send me a search query when you're ready.")
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a message to the user."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    
    # Get the user who caused the error
    user_id = update.effective_user.id if update and update.effective_user else "Unknown"
    username = update.effective_user.username if update and update.effective_user else "Unknown"
    
    logger.error(f"Error occurred for user {username} (ID: {user_id})")
    
    # Send message to the user
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "Sorry, an error occurred while processing your request. Please try again later."
        )

def main() -> None:
    # Validate that we have the required configuration
    missing_vars = []
    
    if not TELEGRAM_TOKEN:
        missing_vars.append("TELEGRAM_BOT_TOKEN")
    if not JACKETT_API_KEY:
        missing_vars.append("JACKETT_API_KEY")
    if not TRANSMISSION_USER:
        missing_vars.append("TRANSMISSION_USER")
    if not TRANSMISSION_PASSWORD:
        missing_vars.append("TRANSMISSION_PASSWORD")
    if not ALLOWED_USERS:
        missing_vars.append("ALLOWED_TELEGRAM_USERS")
    
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}. Exiting.")
        return
        
    logger.info("Starting Telegram Transmission Bot with Jackett integration")
    logger.info(f"Current user: {os.environ.get('USER', 'unknown')}")
    logger.info(f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Create the Application and pass it your bot's token
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Set up the ConversationHandler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_torrent),
                CommandHandler("start", start),
                CommandHandler("status", status),
            ],
            SELECT_TORRENT: [CallbackQueryHandler(select_torrent)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Register the conversation handler
    application.add_handler(conv_handler)
    
    # Register error handler
    application.add_error_handler(error_handler)

    # Start the Bot
    logger.info("Starting bot polling")
    application.run_polling()

if __name__ == '__main__':
    main()