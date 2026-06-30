#!/usr/bin/env python3
"""
Quick test script to verify SMTP email delivery works.
Run: python test_email.py
"""

import asyncio
import sys
import os

# Add backend directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

from services.email_service import email_service
from config import get_settings

async def main():
    settings = get_settings()
    print("=== Email Delivery Test ===\n")
    print(f"SMTP Server: {settings.smtp_host}:{settings.smtp_port}")
    print(f"From: {settings.smtp_from_name} <{settings.smtp_from_email}>")
    print(f"User: {settings.smtp_user}")
    print()

    # Test 1: Send a notification email
    test_recipient = input("Enter test recipient email (or press Enter to send to yourself): ").strip()
    if not test_recipient:
        test_recipient = settings.smtp_user
        print(f"Using SMTP_USER as recipient: {test_recipient}")

    print(f"\nSending test notification email to {test_recipient}...")
    
    success = await email_service.send_notification_email(
        to_email=test_recipient,
        user_name="Test User",
        notification_type="Test Notification",
        notification_title="Test Email",
        notification_message="This is a test email from By8flow to verify SMTP configuration.",
        action_link="http://localhost:3000/dashboard",
    )

    if success:
        print("✅ Email sent successfully! Check your inbox (and spam folder).")
    else:
        print("❌ Email failed to send. Check backend logs for details.")
        print("\nCommon issues:")
        print("1. Gmail requires an App Password if 2FA is enabled.")
        print("2. Make sure SMTP_USER has 'Allow less secure apps' OFF (use App Password).")
        print("3. The 'From' email must match the authenticated Gmail or be configured as 'Send As' in Gmail settings.")

if __name__ == "__main__":
    asyncio.run(main())
