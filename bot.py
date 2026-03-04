import os
import time
import asyncio
import traceback
from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from dotenv import load_dotenv

# Load variables from the .env file
load_dotenv()

# ================= CONFIGURATION =================
# Fetching from environment variables
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Parse the comma-separated string from .env into a list of integers
allowed_users_str = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS = [int(u.strip()) for u in allowed_users_str.split(",") if u.strip()]

GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "service_account.json")
# =================================================

# Initialize Telegram App
app = Client("gdrive_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Initialize Google Drive Service
SCOPES = ['https://www.googleapis.com/auth/drive.file']
creds = None

# Load the token.json file we generated locally
if os.path.exists('token.json'):
    creds = Credentials.from_authorized_user_file('token.json', SCOPES)

# If the token is expired, refresh it automatically and save it
if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    else:
        raise Exception("token.json is missing or invalid! Please run auth.py locally and copy the token.json file to the server.")

drive_service = build('drive', 'v3', credentials=creds)

# Queue for processing files one by one to save server storage
upload_queue = asyncio.Queue()

def create_progress_bar(current, total, bar_length=20):
    """Creates a text-based progress bar."""
    if total == 0:
        return "[--------------------] 0%"
    percent = float(current) * 100 / total
    arrow = '█' * int(percent / 100 * bar_length)
    spaces = '░' * (bar_length - len(arrow))
    return f"[{arrow}{spaces}] {percent:.1f}%"

async def update_progress_msg(message, current_state, current, total, start_time, last_update_time):
    """Throttles Telegram message updates to avoid hitting rate limits."""
    now = time.time()
    # Update every 3 seconds or if it's completely done
    if now - last_update_time[0] > 3.0 or current == total:
        bar = create_progress_bar(current, total)
        downloaded_mb = current / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        speed = downloaded_mb / (now - start_time) if (now - start_time) > 0 else 0
        
        text = (f"**{current_state}**\n"
                f"{bar}\n"
                f"**Progress:** {downloaded_mb:.2f} MB / {total_mb:.2f} MB\n"
                f"**Speed:** {speed:.2f} MB/s")
        try:
            await message.edit_text(text)
            last_update_time[0] = now
        except MessageNotModified:
            pass

def upload_to_drive_sync(file_path, filename, status_msg, loop, start_time, last_update_time):
    """Synchronous function to handle chunked upload to GDrive."""
    file_metadata = {'name': filename, 'parents': [GDRIVE_FOLDER_ID]}
    media = MediaFileUpload(file_path, resumable=True, chunksize=1024 * 1024 * 5) # 5MB chunks
    
    request = drive_service.files().create(body=file_metadata, media_body=media, fields='id')
    
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            # Safely schedule the async progress update from the synchronous thread
            asyncio.run_coroutine_threadsafe(
                update_progress_msg(
                    status_msg, "Uploading to Google Drive... ☁️", 
                    status.resumable_progress, status.total_size, 
                    start_time, last_update_time
                ), loop
            )
    return response.get('id')

async def process_file(message, status_msg):
    """Handles the downloading, uploading, and strict cleanup of a file."""
    file_path = None
    start_time = time.time()
    last_update_time = [0.0]

    try:
        # 1. Download from Telegram
        async def tg_progress(current, total):
            await update_progress_msg(status_msg, "Downloading to Server... 📥", current, total, start_time, last_update_time)

        file_path = await message.download(progress=tg_progress)
        
        if not file_path:
            raise Exception("Failed to download file from Telegram.")

        filename = os.path.basename(file_path)

        # 2. Upload to Google Drive
        start_time = time.time() # Reset start time for upload speed tracking
        last_update_time = [0.0]
        
        # Run the blocking Google Drive upload in a separate thread so it doesn't freeze the bot
        loop = asyncio.get_running_loop()
        file_id = await loop.run_in_executor(
            None, upload_to_drive_sync, file_path, filename, status_msg, loop, start_time, last_update_time
        )

        # 3. Success Notification
        await status_msg.edit_text(f"✅ **Successfully Uploaded!**\n**File Name:** `{filename}`\n**Drive File ID:** `{file_id}`")

    except Exception as e:
        # Capture full traceback
        error_trace = traceback.format_exc()
        
        # ---> ADD THESE 3 LINES to print to Docker logs <---
        print("=== UPLOAD ERROR ===")
        print(error_trace)
        print("====================")
        
        # Replace backticks in the traceback to prevent breaking Telegram's markdown
        safe_trace = error_trace[-3500:].replace("`", "'")
        error_text = f"❌ **An Error Occurred:**\n\n`{str(e)}`\n\n**Traceback:**\n```python\n{safe_trace}\n```"
        
        try:
            await status_msg.edit_text(error_text)
        except Exception:
            # Absolute fallback if markdown still fails for any reason
            await message.reply_text("❌ **An upload error occurred.**\nThe error message contained invalid markdown, so it couldn't be sent here. Please check the Docker logs for the full traceback.")
            
    finally:
        # 4. STRICT CLEANUP: Guarantee file deletion to respect the 8GB limit
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                print(f"Deleted local file: {file_path}")
            except Exception as e:
                print(f"Failed to delete {file_path}: {e}")

async def queue_worker():
    """Background task that pulls messages from the queue and processes them sequentially."""
    while True:
        message, status_msg = await upload_queue.get()
        try:
            await process_file(message, status_msg)
        finally:
            upload_queue.task_done()

@app.on_message(filters.user(ALLOWED_USERS) & (filters.document | filters.video | filters.photo | filters.audio))
async def handle_media(client, message):
    """Triggered when you send media. Adds it to the queue."""
    # Find position in queue
    position = upload_queue.qsize() + 1
    status_msg = await message.reply_text(f"⏳ **Added to queue.**\nPosition in queue: {position}\nPlease wait...")
    
    # Put in queue
    await upload_queue.put((message, status_msg))

@app.on_message(filters.user(ALLOWED_USERS) & filters.command("start"))
async def start_cmd(client, message):
    await message.reply_text("👋 Send or forward any video, image, or document. I will queue it, download it temporarily, upload it to Google Drive, and delete it off my server.")

async def main():
    # Start the background worker for the queue
    asyncio.create_task(queue_worker())
    
    print("Bot is starting...")
    await app.start()
    print("Bot is running. Press Ctrl+C to stop.")
    
    # Keep the bot running
    import pyrogram
    await pyrogram.idle()

if __name__ == "__main__":
    # Ensure a 'downloads' directory exists for Pyrogram
    if not os.path.exists("downloads"):
        os.makedirs("downloads")
        
    app.run(main())
