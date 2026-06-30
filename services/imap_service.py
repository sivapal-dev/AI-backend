"""
IMAP service for fetching and parsing emails from company email inbox.
Uses standard library imaplib + email modules.
"""
import asyncio
import imaplib
import email
from email.header import decode_header
from datetime import datetime
from typing import List, Dict, Optional, Any
import logging
from config import get_settings

logger = logging.getLogger(__name__)


def is_mock_password(password: str) -> bool:
    """Check if password is a local sandbox/mock password"""
    return password == "officetech@8" or password == "mock" or password.startswith("mock_")


MOCK_EMAILS = [
    {
        "message_id": "mock_msg_001",
        "subject": "⚠️ Urgent: Critical Database Connection Leak on Staging",
        "from": "Sarah Chen (Lead Backend) <sarah.chen@by8labs.com>",
        "to": "by8tech@gmail.com",
        "date": datetime(2026, 6, 9, 10, 15, 0),
        "snippet": "Hey team, we're seeing connection pool exhaustion on the staging database. It seems like the Motor driver sessions are not being closed properly in the new sprint router...",
        "body_text": "Hey team,\n\nWe are seeing a critical database connection leak on the staging server. It looks like the Motor driver sessions are not being closed properly in the new sprint router we deployed yesterday. Could someone from backend check it out? I've attached the logs.\n\nBest,\nSarah Chen",
        "body_html": "<p>Hey team,</p><p>We are seeing a critical database connection leak on the staging server. It looks like the Motor driver sessions are not being closed properly in the new sprint router we deployed yesterday. Could someone from backend check it out? I've attached the logs.</p><p>Best,<br>Sarah Chen</p>",
        "attachments": [{"filename": "staging_db_error.log", "content_type": "text/plain", "size": 14502}]
    },
    {
        "message_id": "mock_msg_002",
        "subject": "🚀 Sprint 4 Release: Production deployment scheduled for Thursday",
        "from": "Alex Miller (Product Manager) <alex.miller@by8labs.com>",
        "to": "by8tech@gmail.com",
        "date": datetime(2026, 6, 9, 9, 0, 0),
        "snippet": "Hi everyone, Sprint 4 is coming to an end. All target tickets are currently in QA or ready for deployment. Please review the release notes and double-check your tasks...",
        "body_text": "Hi everyone,\n\nSprint 4 is coming to an end. All target tickets are currently in QA or ready for deployment. Please review the release notes and double-check your tasks before our release meeting tomorrow morning.\n\nThanks,\nAlex",
        "body_html": "<p>Hi everyone,</p><p>Sprint 4 is coming to an end. All target tickets are currently in QA or ready for deployment. Please review the release notes and double-check your tasks before our release meeting tomorrow morning.</p><p>Thanks,<br>Alex</p>",
        "attachments": []
    },
    {
        "message_id": "mock_msg_003",
        "subject": "🎨 New UI/UX Design System Guidelines for By8flow v2",
        "from": "Elena Rostova (UI/UX Designer) <elena.r@by8labs.com>",
        "to": "by8tech@gmail.com",
        "date": datetime(2026, 6, 8, 16, 30, 0),
        "snippet": "Hey team! I have finalized the Figma components and theme tokens for the new dark mode. The new guidelines use sleek glassmorphism and HSL tailored colors. Please review...",
        "body_text": "Hey team!\n\nI have finalized the Figma components and theme tokens for the new dark mode. The new guidelines use sleek glassmorphism and HSL tailored colors. Please review them and let me know if we have any frontend challenges.\n\nBest,\nElena",
        "body_html": "<p>Hey team!</p><p>I have finalized the Figma components and theme tokens for the new dark mode. The new guidelines use sleek glassmorphism and HSL tailored colors. Please review them and let me know if we have any frontend challenges.</p><p>Best,<br>Elena</p>",
        "attachments": []
    },
    {
        "message_id": "mock_msg_004",
        "subject": "🔒 Security Alert: MSAL Token Rotation Policy Update",
        "from": "Security Ops <security@by8labs.com>",
        "to": "by8tech@gmail.com",
        "date": datetime(2026, 6, 8, 11, 0, 0),
        "snippet": "Attention all developers, we are rotating MSAL client secrets and updating redirect URIs tomorrow morning at 06:00 UTC. Ensure you pull the latest backend/.env...",
        "body_text": "Attention all developers,\n\nWe are rotating MSAL client secrets and updating redirect URIs tomorrow morning at 06:00 UTC. Ensure you pull the latest backend/.env files and restart your servers accordingly.\n\nSecurity Team",
        "body_html": "<p>Attention all developers,</p><p>Attention all developers, we are rotating MSAL client secrets and updating redirect URIs tomorrow morning at 06:00 UTC. Ensure you pull the latest backend/.env files and restart your servers accordingly.</p><p>Security Team</p>",
        "attachments": []
    }
]


class EmailMessage:
    """Normalized email data structure"""

    def __init__(
        self,
        message_id: str,
        subject: str,
        from_: str,
        to: str,
        date: datetime,
        snippet: str,
        body_text: Optional[str] = None,
        body_html: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        is_unread: bool = True,
    ):
        self.message_id = message_id
        self.subject = subject
        self.from_ = from_
        self.to = to
        self.date = date
        self.snippet = snippet
        self.body_text = body_text
        self.body_html = body_html
        self.attachments = attachments or []
        self.is_unread = is_unread

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "subject": self.subject,
            "from": self.from_,
            "to": self.to,
            "date": self.date.isoformat() if self.date else None,
            "snippet": self.snippet,
            "body_text": self.body_text,
            "body_html": self.body_html,
            "attachments": self.attachments,
            "is_unread": self.is_unread,
        }


def _decode_mime_header(value: Optional[str]) -> str:
    """Decode email header (subject, from, etc.) with proper charset handling"""
    if not value:
        return ""
    try:
        decoded_parts = decode_header(value)
        result = []
        for part, encoding in decoded_parts:
            if isinstance(part, bytes):
                # Decode bytes using detected encoding or utf-8 fallback
                result.append(part.decode(encoding or "utf-8", errors="replace"))
            else:
                result.append(part)
        return "".join(result)
    except Exception as e:
        logger.warning(f"Failed to decode header '{value}': {e}")
        return value


def _parse_email_body(msg) -> tuple[Optional[str], Optional[str], List[Dict]]:
    """
    Parse email message and extract plain text, HTML, and attachments.

    Returns:
        (plain_text, html_text, attachments_list)
    """
    plain_text = None
    html_text = None
    attachments = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = part.get("Content-Disposition", "")

            # Skip multipart containers
            if part.is_multipart():
                continue

            # Handle attachments
            if "attachment" in content_disposition or "filename" in content_disposition:
                filename = part.get_filename()
                if filename:
                    filename = _decode_mime_header(filename)
                    attachments.append(
                        {
                            "filename": filename,
                            "content_type": content_type,
                            "size": len(part.get_payload(decode=True) or b""),
                        }
                    )
                continue

            # Handle body parts
            payload = part.get_payload(decode=True)
            if payload is None:
                continue

            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                decoded = payload.decode("utf-8", errors="replace")

            if content_type == "text/plain" and plain_text is None:
                plain_text = decoded
            elif content_type == "text/html" and html_text is None:
                html_text = decoded
    else:
        # Single-part message
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                decoded = payload.decode("utf-8", errors="replace")

            content_type = msg.get_content_type()
            if content_type == "text/plain":
                plain_text = decoded
            elif content_type == "text/html":
                html_text = decoded

    # Defensive correction: if plain_text is HTML, promote it to html_text
    if plain_text and not html_text:
        plain_text_strip = plain_text.strip()
        if (
            plain_text_strip.lower().startswith("<!doctype html")
            or plain_text_strip.lower().startswith("<html")
            or plain_text_strip.lower().startswith("<body")
            or plain_text_strip.lower().startswith("<div")
            or "<html>" in plain_text_strip.lower()
            or "<body" in plain_text_strip.lower()
        ):
            html_text = plain_text
            plain_text = None

    return plain_text, html_text, attachments


def _create_snippet(text: Optional[str], html: Optional[str], max_len: int = 200) -> str:
    """Create a plain text snippet from email body (prefer plain text)"""
    body = ""
    if text and text.strip():
        body = text.strip()
        import html as pyhtml
        body = pyhtml.unescape(body)
    elif html and html.strip():
        import re
        body = html
        # Remove head, style, script blocks first
        body = re.sub(r"<head\b[^>]*>.*?</head>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<style\b[^>]*>.*?</style>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<script\b[^>]*>.*?</script>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        
        # Remove all remaining HTML tags
        body = re.sub(r"<[^>]+>", " ", body)
        
        # Decode HTML entities
        import html as pyhtml
        body = pyhtml.unescape(body)
        
        # Normalize whitespace
        body = re.sub(r"\s+", " ", body).strip()

    if len(body) > max_len:
        return body[:max_len].rstrip() + "…"
    return body


async def test_connection(
    email_address: str,
    password: str,
    imap_host: Optional[str] = None,
    imap_port: Optional[int] = None,
) -> bool:
    """
    Test IMAP connection with given credentials.

    Args:
        email_address: Full email address (e.g., john@by8labs.com)
        password: Email account password
        imap_host: Optional host overriding settings
        imap_port: Optional port overriding settings

    Returns:
        True if connection successful, False otherwise
    """
    if is_mock_password(password):
        logger.info(f"Sandbox mock connection successful for {email_address}")
        return True

    settings = get_settings()
    if not imap_host:
        imap_host = settings.imap_host
    if not imap_port:
        imap_port = settings.imap_port

    def _connect():
        try:
            mail = imaplib.IMAP4_SSL(imap_host, imap_port, timeout=10)
            mail.login(email_address, password)
            mail.select("INBOX")
            mail.close()
            mail.logout()
            return True
        except imaplib.IMAP4.error as e:
            logger.warning(f"IMAP login failed for {email_address}: {e}")
            return False
        except Exception as e:
            logger.error(f"IMAP connection error for {email_address}: {e}")
            return False

    return await asyncio.to_thread(_connect)


async def fetch_emails(
    email_address: str,
    password: str,
    folder: str = "INBOX",
    limit: int = 20,
    offset: int = 0,
    imap_host: Optional[str] = None,
    imap_port: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch a list of emails from the specified folder.

    Args:
        email_address: User's email address
        password: Decrypted IMAP password
        folder: Mailbox folder (default: "INBOX")
        limit: Max number of emails to return
        offset: Number of emails to skip from latest
        imap_host: Optional host overriding settings
        imap_port: Optional port overriding settings

    Returns:
        List of email dicts (metadata only, no body)
    """
    if is_mock_password(password):
        logger.info(f"Fetching sandbox mock emails for {email_address}")
        emails = []
        for item in MOCK_EMAILS:
            emails.append({
                "message_id": item["message_id"],
                "subject": item["subject"],
                "from": item["from"],
                "to": item["to"],
                "date": item["date"],
                "snippet": item["snippet"],
            })
        return emails[offset : offset + limit]

    settings = get_settings()
    if not imap_host:
        imap_host = settings.imap_host
    if not imap_port:
        imap_port = settings.imap_port

    def _fetch():
        emails = []
        mail = None
        content_type = None
        try:
            mail = imaplib.IMAP4_SSL(imap_host, imap_port)
            mail.login(email_address, password)
            mail.select(folder)

            # Search all emails, sorted by date descending (latest first)
            status, messages = mail.search(None, "ALL")
            if status != "OK":
                logger.error(f"IMAP search failed: {status}")
                return []

            mail_ids = messages[0].split()
            total = len(mail_ids)

            # Reverse to get latest first, then apply offset/limit
            latest_ids = mail_ids[::-1]
            page_ids = latest_ids[offset : offset + limit]

            for num in page_ids:
                status, msg_data = mail.fetch(num, "(RFC822)")
                if status != "OK":
                    continue

                for response_part in msg_data:
                    if not isinstance(response_part, tuple):
                        continue

                    raw_email = response_part[1]
                    msg = email.message_from_bytes(raw_email)

                    message_id = msg.get("Message-ID", "").strip("<>")
                    subject = _decode_mime_header(msg.get("Subject"))
                    from_ = _decode_mime_header(msg.get("From"))
                    to = _decode_mime_header(msg.get("To"))
                    date_str = msg.get("Date")

                    # Parse date
                    try:
                        from email.utils import parsedate_to_datetime

                        parsed = parsedate_to_datetime(date_str) if date_str else None
                        date = parsed if parsed is not None else datetime.now()
                    except Exception:
                        date = datetime.now()

                    # Create snippet from body (fetch body partially for snippet)
                    snippet = ""
                    plain_content = None
                    html_content = None

                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain" and not part.is_multipart():
                                payload = part.get_payload(decode=True)
                                if payload:
                                    plain_content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                            elif part.get_content_type() == "text/html" and html_content is None:
                                payload = part.get_payload(decode=True)
                                if payload:
                                    html_content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    else:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            content_type = msg.get_content_type()
                            content_charset = msg.get_content_charset() or "utf-8"
                            content_decoded = payload.decode(content_charset, errors="replace")
                            if content_type == "text/html":
                                html_content = content_decoded
                            else:
                                plain_content = content_decoded

                    snippet = _create_snippet(plain_content, html_content, max_len=200)

                    emails.append(
                        {
                            "message_id": message_id,
                            "subject": subject or "(No Subject)",
                            "from": from_,
                            "to": to,
                            "date": date,
                            "snippet": snippet,
                        }
                    )

            return emails
        except imaplib.IMAP4.error as e:
            logger.error(f"IMAP error fetching emails: {e}")
            raise ValueError(f"Failed to fetch emails: {e}")
        except Exception as e:
            logger.error(f"Unexpected error fetching emails: {e}")
            raise ValueError(f"Failed to fetch emails: {e}")
        finally:
            if mail is not None:
                try:
                    mail.close()
                except Exception:
                    pass
                try:
                    mail.logout()
                except Exception:
                    pass

    return await asyncio.to_thread(_fetch)


async def fetch_email_body(
    email_address: str,
    password: str,
    message_id: str,
    folder: str = "INBOX",
    imap_host: Optional[str] = None,
    imap_port: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """
    Fetch a single email with full body content.

    Args:
        email_address: User's email address
        password: Decrypted IMAP password
        message_id: Message-ID header value
        folder: Mailbox folder
        imap_host: Optional host overriding settings
        imap_port: Optional port overriding settings

    Returns:
        Full email dict with body_text, body_html, attachments, or None if not found
    """
    if is_mock_password(password):
        logger.info(f"Fetching sandbox mock email body for {message_id}")
        for item in MOCK_EMAILS:
            if item["message_id"] == message_id:
                return {
                    "message_id": item["message_id"],
                    "subject": item["subject"],
                    "from": item["from"],
                    "to": item["to"],
                    "date": item["date"],
                    "body_text": item["body_text"],
                    "body_html": item["body_html"],
                    "snippet": item["snippet"],
                    "attachments": item["attachments"]
                }
        return None

    settings = get_settings()
    if not imap_host:
        imap_host = settings.imap_host
    if not imap_port:
        imap_port = settings.imap_port

    def _fetch_body():
        mail = None
        content_type = None
        try:
            mail = imaplib.IMAP4_SSL(imap_host, imap_port)
            mail.login(email_address, password)
            mail.select(folder)

            # Search by Message-ID
            search_criteria = f'(HEADER Message-ID "{message_id}")'
            status, messages = mail.search(None, search_criteria)
            if status != "OK" or not messages[0]:
                logger.warning(f"Email not found: {message_id}")
                return None

            mail_ids = messages[0].split()
            num = mail_ids[0]  # Take first match

            status, msg_data = mail.fetch(num, "(RFC822)")
            if status != "OK":
                return None

            for response_part in msg_data:
                if not isinstance(response_part, tuple):
                    continue

                raw_email = response_part[1]
                msg = email.message_from_bytes(raw_email)

                subject = _decode_mime_header(msg.get("Subject"))
                from_ = _decode_mime_header(msg.get("From"))
                to = _decode_mime_header(msg.get("To"))
                date_str = msg.get("Date")

                try:
                    from email.utils import parsedate_to_datetime

                    date = parsedate_to_datetime(date_str) if date_str else datetime.now()
                except Exception:
                    date = datetime.now()

                body_text, body_html, attachments = _parse_email_body(msg)

                # Build snippet
                snippet = _create_snippet(body_text, body_html, max_len=300)

                return {
                    "message_id": message_id,
                    "subject": subject or "(No Subject)",
                    "from": from_,
                    "to": to,
                    "date": date,
                    "body_text": body_text,
                    "body_html": body_html,
                    "snippet": snippet,
                    "attachments": attachments,
                }

            return None

        except imaplib.IMAP4.error as e:
            logger.error(f"IMAP error fetching email body: {e}")
            raise ValueError(f"Failed to fetch email: {e}")
        except Exception as e:
            logger.error(f"Unexpected error fetching email body: {e}")
            raise ValueError(f"Failed to fetch email: {e}")
        finally:
            if mail is not None:
                try:
                    mail.close()
                except Exception:
                    pass
                try:
                    mail.logout()
                except Exception:
                    pass

    return await asyncio.to_thread(_fetch_body)


async def list_folders(
    email_address: str,
    password: str,
    imap_host: Optional[str] = None,
    imap_port: Optional[int] = None,
) -> List[str]:
    """
    List available mailbox folders (INBOX, Sent, Drafts, etc.)

    Returns:
        List of folder names
    """
    if is_mock_password(password):
        return ["INBOX", "Sent", "Drafts", "Archive", "Trash"]

    settings = get_settings()
    if not imap_host:
        imap_host = settings.imap_host
    if not imap_port:
        imap_port = settings.imap_port

    def _list():
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(imap_host, imap_port)
            mail.login(email_address, password)

            status, folders = mail.list()
            if status != "OK":
                return ["INBOX"]

            folder_list = []
            for folder_bytes in folders:
                # Parse folder name from LIST response: b'(\\HasNoChildren) "/" "INBOX"'
                folder_str = folder_bytes.decode("utf-8", errors="replace")
                parts = folder_str.split(' "/" ')
                if len(parts) == 2:
                    folder_name = parts[1].strip('"')
                    folder_list.append(folder_name)

            return folder_list or ["INBOX"]

        except Exception as e:
            logger.error(f"Failed to list folders: {e}")
            return ["INBOX"]
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass

    return await asyncio.to_thread(_list)
