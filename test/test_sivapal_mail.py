import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import sys
import imaplib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_settings
from utils.encryption import decrypt_password

async def main():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    db = client[settings.database_name]
    
    # Sivapal's record in MongoDB Atlas
    user = await db.users.find_one({"email": "kangasiva15@gmail.com"})
    if not user:
        print("Sivapal's user account (kangasiva15@gmail.com) not found in cloud database.")
        client.close()
        return

    creds = user.get("email_credentials")
    if not creds:
        print("No email credentials configured for Sivapal.")
        client.close()
        return

    email_address = creds.get("email_address")
    encrypted_password = creds.get("encrypted_password")
    imap_host = creds.get("imap_host", "imap.hostinger.com")
    imap_port = creds.get("imap_port", 993)

    print(f"Decrypted email address: {email_address}")
    print(f"IMAP Server: {imap_host}:{imap_port}")

    try:
        password = decrypt_password(encrypted_password)
        print("Password successfully decrypted.")
    except Exception as e:
        print(f"Failed to decrypt password: {e}")
        client.close()
        return

    print("Attempting to connect to IMAP server...")
    try:
        mail = imaplib.IMAP4_SSL(imap_host, imap_port)
        print("SSL Connection established. Attempting login...")
        mail.login(email_address, password)
        print("Login successful!")
        mail.select("INBOX")
        print("Selected INBOX successfully!")
        mail.close()
        mail.logout()
        print("Connection check complete: SUCCESS")
    except imaplib.IMAP4.error as e:
        print(f"IMAP login failed: {e}")
    except Exception as e:
        print(f"Connection failed with error: {type(e).__name__}: {e}")

    client.close()

if __name__ == "__main__":
    asyncio.run(main())
