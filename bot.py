import os
import time
import asyncio
import traceback
from pyrogram import Client, filters
from pyrogram.errors import MessageNotModified
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from dotenv import load_dotenv

# Load variables from the .env file
load_dotenv()

# ================= CONFIGURATION =================
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

allowed_users_str = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS = [int(u.strip()) for u in allowed_users_str.split(",") if u.strip()]

GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID")
# =================================================

# Initialize Telegram App
app = Client("gdrive_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Initialize Google Drive Service using token.json
SCOPES = ['https://www.googleapis.com/auth/drive.file']
creds = None

if os.path.exists('token.json'):
    creds = Credentials.from_authorized_user_file('token.json', SCOPES)

if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    else:
        raise Exception("token.json is missing or invalid! Please run auth.py locally and copy token.json to the server.")

drive_service = build('drive', 'v3', credentials=creds)

# Queue for processing files one by one
upload_queue = asyncio.Queue()

def create_progress_bar(current, total, bar_length=20):
    if total == 0:
        return "[--------------------] 0%"
    percent = float(current) * 100 / total
    arrow = '█' * int(percent / 100 * bar_length)
    spaces = '░' * (bar_length - len(arrow))
    return f"[{arrow}{spaces}] {percent:.1f}%"

async def update_progress_msg(message, current_state, current, total, start_time, last_update_time):
    now = time.time()
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

def get_unique_filename(filename):
    """Checks Google Drive and appends _2, _3, etc. if the file exists."""
    base, ext = os.path.splitext(filename)
    current_name = filename
    counter = 2

    while True:
        # Escape single quotes in the filename so the Drive API doesn't crash
        safe_name = current_name.replace("'", "\\'")
        query = f"name='{safe_name}' and '{GDRIVE_FOLDER_ID}' in parents and trashed=false"
        
        results = drive_service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        items = results.get('files', [])

        if not items:
            # Name is completely unique!
            return current_name

        # Name exists, increment and try again
        current_name = f"{base}_{counter}{ext}"
        counter += 1

def upload_to_drive_sync(file_path, original_filename, status_msg, loop, start_time, last_update_time):
    # 1. Determine the final, unique filename before uploading
    final_filename = get_unique_filename(original_filename)

    # 2. Upload with the unique name
    file_metadata = {'name': final_filename, 'parents': [GDRIVE_FOLDER_ID]}
    media = MediaFileUpload(file_path, resumable=True, chunksize=1024 * 1024 * 5)
    
    request = drive_service.files().create(body=file_metadata, media_body=media, fields='id')
    
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            asyncio.run_coroutine_threadsafe(
                update_progress_msg(
                    status_msg, f"Uploading as `{final_filename}`... ☁️", 
                    status.resumable_progress, status.total_size, 
                    start_time, last_update_time
                ), loop
            )
    return response.get('id'), final_filename

async def process_file(message, status_msg):
    file_path = None
    start_time = time.time()
    last_update_time = [0.0]

    try:
        # 1. Download
        async def tg_progress(current, total):
            await update_progress_msg(status_msg, "Downloading to Server... 📥", current, total, start_time, last_update_time)

        file_path = await message.download(progress=tg_progress)
        
        if not file_path:
            raise Exception("Failed to download file from Telegram.")

        original_filename = os.path.basename(file_path)

        # 2. Upload
        start_time = time.time()
        last_update_time = [0.0]
        
        loop = asyncio.get_running_loop()
        file_id, final_filename = await loop.run_in_executor(
            None, upload_to_drive_sync, file_path, original_filename, status_msg, loop, start_time, last_update_time
        )

        # 3. Success Notification
        await status_msg.edit_text(f"✅ **Successfully Uploaded!**\n**Saved As:** `{final_filename}`\n**Drive File ID:** `{file_id}`")

    except Exception as e:
        error_trace = traceback.format_exc()
        
        # Print to server console for debugging
        print("=== UPLOAD ERROR ===")
        print(error_trace)
        print("====================")
        
        safe_trace = error_trace[-3500:].replace("`", "'")
        error_text = f"❌ **An Error Occurred:**\n\n`{str(e)}`\n\n**Traceback:**\n```python\n{safe_trace}\n```"
        
        try:
            await status_msg.edit_text(error_text)
        except Exception:
            await message.reply_text("❌ **An upload error occurred.**\nThe error message contained invalid markdown. Please check the Docker logs.")
            
    finally:
        # 4. Strict Cleanup
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                print(f"Deleted local file: {file_path}")
            except Exception as e:
                print(f"Failed to delete {file_path}: {e}")

async def queue_worker():
    while True:
        message, status_msg = await upload_queue.get()
        try:
            await process_file(message, status_msg)
        finally:
            upload_queue.task_done()

@app.on_message(filters.user(ALLOWED_USERS) & (filters.document | filters.video | filters.photo | filters.audio))
async def handle_media(client, message):
    position = upload_queue.qsize() + 1
    status_msg = await message.reply_text(f"⏳ **Added to queue.**\nPosition in queue: {position}\nPlease wait...")
    await upload_queue.put((message, status_msg))

@app.on_message(filters.user(ALLOWED_USERS) & filters.command("start"))
async def start_cmd(client, message):
    await message.reply_text("👋 Send or forward any video, image, or document. I will queue it, upload it to Google Drive, and safely wipe it from my server.")

async def main():
    asyncio.create_task(queue_worker())
    print("Bot is starting...")
    await app.start()
    print("Bot is running. Press Ctrl+C to stop.")
    import pyrogram
    await pyrogram.idle()

if __name__ == "__main__":
    if not os.path.exists("downloads"):
        os.makedirs("downloads")
    app.run(main())