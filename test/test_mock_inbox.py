import asyncio
import sys
import os

# Add backend directory to path
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, backend_dir)

# Ensure stdout supports UTF-8 emojis on Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from services.imap_service import test_connection, fetch_emails, fetch_email_body, list_folders

async def test_sandbox_imap():
    print("=== Sandbox/Mock IMAP Service Test ===")
    
    email = "by8tech@gmail.com"
    pwd = "officetech@8"
    
    print("\n1. Testing test_connection...")
    conn_result = await test_connection(email, pwd)
    print(f"Result: {conn_result} (Expected: True)")
    assert conn_result is True, "test_connection failed!"
    
    print("\n2. Testing list_folders...")
    folders = await list_folders(email, pwd)
    print(f"Result: {folders} (Expected standard folders)")
    assert "INBOX" in folders, "INBOX folder missing!"
    
    print("\n3. Testing fetch_emails...")
    emails = await fetch_emails(email, pwd)
    print(f"Fetched {len(emails)} emails:")
    for mail in emails:
        print(f"  - [{mail['message_id']}] From: {mail['from']} | Subject: {mail['subject']}")
    assert len(emails) > 0, "No mock emails fetched!"
    
    print("\n4. Testing fetch_email_body for mock_msg_001...")
    msg_id = "mock_msg_001"
    body = await fetch_email_body(email, pwd, msg_id)
    assert body is not None, "Failed to fetch mock email body!"
    print(f"Result details:")
    print(f"  Subject: {body['subject']}")
    print(f"  Attachments: {body['attachments']}")
    print(f"  Snippet: {body['snippet']}")
    
    print("\n✅ All sandbox IMAP service checks passed!")

if __name__ == "__main__":
    asyncio.run(test_sandbox_imap())
