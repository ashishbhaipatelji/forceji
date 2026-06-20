"""
Generate a Telethon StringSession for use as SESSION_STRING env var.

Run this ONCE locally (not on Railway/Replit):
    python3 generate_session.py

Then copy the printed string into your Railway Variables as SESSION_STRING.
This avoids losing the session file on every cloud redeploy.
"""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from decouple import config

api_id   = config("API_ID", cast=int)
api_hash = config("API_HASH")

print("Connecting to Telegram to generate a session string...")
print("You will be prompted for your phone number and a login code.\n")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    session_string = client.session.save()

print("\n" + "=" * 60)
print("SESSION_STRING (copy this to Railway Variables):")
print("=" * 60)
print(session_string)
print("=" * 60)
print("\nIMPORTANT: Keep this string private — it grants full account access.")
