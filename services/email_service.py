from jinja2 import Template
from aiosmtplib import SMTP, SMTPConnectError, SMTPAuthenticationError, SMTPException
from email.message import EmailMessage
from config import get_settings
from typing import Optional, Tuple
import asyncio
import logging
import urllib.parse
import traceback

settings = get_settings()
logger = logging.getLogger(__name__)


def log_email_config_summary() -> None:
    """
    Print startup log showing which email sender is active and which SMTP config
    is loaded — WITHOUT exposing secrets (passwords/API keys are masked).

    Call this once during app lifespan startup.
    """
    s = get_settings()
    resend_active = bool(s.resend_api_key)
    smtp_user_set = bool(s.smtp_user)
    smtp_pass_set = bool(s.smtp_password)

    logger.info(
        "═" * 60 + "\n"
        "[EmailService] STARTUP CONFIG SUMMARY\n"
        f"  Primary sender  : {'Resend HTTP API (port 443)' if resend_active else 'SMTP (aiosmtplib)'}\n"
        f"  RESEND_API_KEY  : {'SET (' + s.resend_api_key[:6] + '...)' if resend_active else 'NOT SET'}\n"
        f"  SMTP_HOST       : {s.smtp_host}\n"
        f"  SMTP_PORT       : {s.smtp_port}\n"
        f"  SMTP_USER       : {s.smtp_user if smtp_user_set else '(not set)'}\n"
        f"  SMTP_PASSWORD   : {'SET (****)' if smtp_pass_set else '(not set)'}\n"
        f"  SMTP_USE_TLS    : {s.smtp_use_tls}  (True=port 465 implicit TLS)\n"
        f"  SMTP_START_TLS  : {s.smtp_start_tls}  (True=port 587 STARTTLS)\n"
        f"  SMTP_TIMEOUT    : {s.smtp_timeout}s\n"
        f"  SMTP_FROM_EMAIL : {s.smtp_from_email}\n"
        f"  SMTP_FROM_NAME  : {s.smtp_from_name}\n"
        + "═" * 60
    )

    if not resend_active and not (smtp_user_set and smtp_pass_set):
        logger.warning(
            "[EmailService] WARNING: Neither RESEND_API_KEY nor SMTP credentials are configured. "
            "Emails will only be printed to console (DEV MODE)."
        )
    elif not resend_active and smtp_user_set:
        logger.info(
            "[EmailService] Using SMTP directly. Make sure SMTP server is reachable."
        )


OTP_EMAIL_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Your By8flow Sign-In Code</title>
</head>
<body style="margin:0; padding:20px; background: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
        <tr>
            <td align="center">
                <table role="presentation" width="520" cellspacing="0" cellpadding="0" border="0" style="max-width:520px; width:100%; background:#ffffff; border-radius:16px; overflow:hidden; box-shadow:0 20px 60px rgba(0,0,0,0.3);">
                    <!-- Header -->
                    <tr>
                        <td align="center" style="background: #3b82f6; padding:40px 32px; text-align:center;">
                            <div style="font-size:28px; font-weight:800; color:#ffffff; letter-spacing:-0.5px; text-align:center;">By8flow</div>
                        </td>
                    </tr>
                    <!-- Content -->
                    <tr>
                        <td align="center" style="padding:48px 32px 32px; text-align:center;">
                            <p style="font-size:18px; color:#1f2937; margin:0 0 24px 0; font-weight:500; text-align:center;">Hello {{ name or 'there' }},</p>

                            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background: #f0f9ff; border:2px dashed #3b82f6; border-radius:12px; padding:32px; margin:24px 0 32px 0;">
                                <tr>
                                    <td align="center" style="text-align:center;">
                                        <div style="font-size:12px; text-transform:uppercase; letter-spacing:2px; color:#6b7280; margin-bottom:12px; font-weight:600; text-align:center;">Your Verification Code</div>
                                        <div style="font-size:48px; font-weight:800; letter-spacing:8px; color:#1e293b; font-family:'Courier New', monospace; text-align:center;">{{ otp }}</div>
                                    </td>
                                </tr>
                            </table>

                            <p style="font-size:14px; color:#6b7280; line-height:1.7; margin:0 0 24px 0; text-align:center;">
                                This 6-digit code will expire in <strong style="color:#1f2937;">5 minutes</strong>.<br>
                                Enter it in the app to complete your sign-in.<br>
                                If you didn't request this code, please ignore this email.
                            </p>

                            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#fef3c7; border-left:4px solid #f59e0b; padding:16px; border-radius:8px; margin-top:24px;">
                                <tr>
                                    <td style="font-size:13px; color:#92400e; text-align:left;">
                                        <strong>Security Notice:</strong> Never share this code with anyone. By8flow staff will never ask for your OTP.
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                    <!-- Footer -->
                    <tr>
                        <td align="center" style="padding:24px 32px; background:#f8fafc; text-align:center; border-top:1px solid #e5e7eb;">
                            <p style="font-size:12px; color:#9ca3af; margin:0 0 4px 0; text-align:center;"><span style="font-weight:600; color:#3b82f6;">By8flow</span> - By8Labs Internal Tools</p>
                            <p style="font-size:12px; color:#9ca3af; margin:0; text-align:center;">This is an automated message, please do not reply</p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
"""

WELCOME_EMAIL_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Welcome to By8flow</title>
</head>
<body style="margin:0; padding:20px; background: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
        <tr>
            <td align="center">
                <table role="presentation" width="520" cellspacing="0" cellpadding="0" border="0" style="max-width:520px; width:100%; background:#ffffff; border-radius:16px; overflow:hidden; box-shadow:0 20px 60px rgba(0,0,0,0.3);">
                    <!-- Header -->
                    <tr>
                        <td align="center" style="background: #3b82f6; padding:40px 32px; text-align:center;">
                            <div style="font-size:28px; font-weight:800; color:#ffffff; letter-spacing:-0.5px; text-align:center;">By8flow</div>
                        </td>
                    </tr>
                    <!-- Content -->
                    <tr>
                        <td align="center" style="padding:48px 32px 32px; text-align:center;">
                            <p style="font-size:18px; color:#1f2937; margin:0 0 24px 0; font-weight:500; text-align:center;">Hello {{ name }},</p>

                            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background: #f0f9ff; border:2px dashed #3b82f6; border-radius:12px; padding:32px; margin:24px 0 32px 0;">
                                <tr>
                                    <td align="center" style="text-align:center;">
                                        <div style="font-size:16px; color:#1e293b; line-height:1.7;">
                                            Welcome to By8flow!<br><br>
                                            Click the button below to verify your email and get started.
                                        </div>
                                    </td>
                                </tr>
                            </table>

                            <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:32px 0;">
                                <tr>
                                    <td align="center" style="background:#3b82f6; border-radius:8px; padding:14px 32px;">
                                        <a href="{{ verify_url }}" target="_blank" style="font-size:16px; font-weight:600; color:#ffffff; text-decoration:none; display:inline-block;">
                                            Verify Email
                                        </a>
                                    </td>
                                </tr>
                            </table>

                            <p style="font-size:14px; color:#6b7280; line-height:1.7; margin:24px 0 0 0; text-align:center;">
                                If the button doesn't work, copy and paste this link:<br>
                                <a href="{{ verify_url }}" style="color:#3b82f6; word-break:break-all;">{{ verify_url }}</a>
                            </p>

                            <!-- Footer -->
                            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:48px; border-top:1px solid #e5e7eb; padding-top:24px;">
                                <tr>
                                    <td align="center" style="text-align:center;">
                                        <p style="font-size:12px; color:#9ca3af; margin:0 0 8px 0; text-align:center;">By8flow — Your Workspace, Your Way</p>
                                        <p style="font-size:12px; color:#9ca3af; margin:0; text-align:center;">
                                            You received this email because you signed up for By8flow.
                                        </p>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
"""

NOTIFICATION_EMAIL_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ notification_title }} — By8flow</title>
</head>
<body style="margin:0; padding:20px; background: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
        <tr>
            <td align="center">
                <table role="presentation" width="520" cellspacing="0" cellpadding="0" border="0" style="max-width:520px; width:100%; background:#ffffff; border-radius:16px; overflow:hidden; box-shadow:0 20px 60px rgba(0,0,0,0.3);">
                    <!-- Header -->
                    <tr>
                        <td align="center" style="background: #3b82f6; padding:40px 32px; text-align:center;">
                            <div style="font-size:28px; font-weight:800; color:#ffffff; letter-spacing:-0.5px; text-align:center;">By8flow</div>
                        </td>
                    </tr>
                    <!-- Content -->
                    <tr>
                        <td align="center" style="padding:48px 32px 32px; text-align:center;">
                            <p style="font-size:18px; color:#1f2937; margin:0 0 24px 0; font-weight:500; text-align:center;">
                                Hello {{ user_name }},<br>
                                you have a new notification.
                            </p>

                            <!-- Notification Card -->
                            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background: #f8fafc; border:1px solid #e5e7eb; border-radius:12px; padding:24px; text-align:left; margin:24px 0;">
                                <tr>
                                    <td style="text-align:left;">
                                        <p style="font-size:14px; font-weight:600; color:#3b82f6; margin:0 0 12px 0; text-transform:uppercase; letter-spacing:0.5px; text-align:left;">
                                            {{ notification_type }}
                                        </p>
                                        <h3 style="font-size:20px; font-weight:700; color:#1f2937; margin:0 0 12px 0; text-align:left; line-height:1.4;">
                                            {{ notification_title }}
                                        </h3>
                                        <p style="font-size:15px; color:#4b5563; line-height:1.6; margin:0; text-align:left;">
                                            {{ notification_message }}
                                        </p>
                                    </td>
                                </tr>
                            </table>

                            <!-- CTA Button (if action link provided) -->
                            {% if action_link %}
                            <table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:24px 0 0 0;">
                                <tr>
                                    <td align="center" style="background:#3b82f6; border-radius:8px; padding:14px 32px;">
                                        <a href="{{ action_link }}" target="_blank" style="font-size:16px; font-weight:600; color:#ffffff; text-decoration:none; display:inline-block;">
                                            View in App
                                        </a>
                                    </td>
                                </tr>
                            </table>
                            {% endif %}

                            <!-- Fallback Link -->
                            {% if action_link %}
                            <p style="font-size:13px; color:#6b7280; line-height:1.6; margin:16px 0 0 0; text-align:center;">
                                If the button doesn't work, copy and paste this link:<br>
                                <a href="{{ action_link }}" style="color:#3b82f6; word-break:break-all;">{{ action_link }}</a>
                            </p>
                            {% endif %}

                            <!-- Footer -->
                            <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:48px; border-top:1px solid #e5e7eb; padding-top:24px;">
                                <tr>
                                    <td align="center" style="text-align:center;">
                                        <p style="font-size:12px; color:#9ca3af; margin:0 0 8px 0; text-align:center;">By8flow — Your Workspace, Your Way</p>
                                        <p style="font-size:12px; color:#9ca3af; margin:0 0 4px 0; text-align:center;">
                                            You're receiving this email because you have email notifications enabled in your By8flow settings.
                                        </p>
                                        <p style="font-size:12px; color:#9ca3af; margin:0; text-align:center;">
                                            Need to unsubscribe? You can manage your notification preferences in your account settings.
                                        </p>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
"""


class EmailService:
    def __init__(self):
        self.smtp_host     = settings.smtp_host
        self.smtp_port     = settings.smtp_port
        self.smtp_user     = settings.smtp_user
        self.smtp_password = settings.smtp_password
        self.from_name     = settings.smtp_from_name
        self.from_email    = settings.smtp_from_email
        self.smtp_use_tls  = settings.smtp_use_tls
        self.smtp_start_tls = settings.smtp_start_tls
        self.smtp_timeout  = settings.smtp_timeout
        self.resend_api_key = settings.resend_api_key

    # ─────────────────────────────────────────────────────────────────────────
    # Layer 1: Resend HTTP API  (port 443 — works on Render Free tier)
    # ─────────────────────────────────────────────────────────────────────────
    async def _send_via_resend(
        self, to_email: str, subject: str, html_content: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Send email using the Resend HTTP API (https://api.resend.com/emails).
        Uses HTTPS port 443 — not affected by Render's SMTP port block.
        Requires RESEND_API_KEY environment variable.
        """
        import httpx

        logger.info(
            f"[Email/Resend] Attempting to send via Resend HTTP API │ "
            f"to={to_email} │ subject='{subject}'"
        )

        payload = {
            "from": f"{self.from_name} <{self.from_email}>",
            "to": [to_email],
            "subject": subject,
            "html": html_content,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info("[Email/Resend] Connecting to api.resend.com (port 443)...")
                response = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {self.resend_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )

            if response.status_code in (200, 201):
                resp_data = response.json()
                email_id = resp_data.get("id", "(unknown)")
                logger.info(
                    f"[Email/Resend] ✓ Email sent successfully │ "
                    f"to={to_email} │ resend_id={email_id}"
                )
                return True, None
            else:
                err = f"Resend API error {response.status_code}: {response.text}"
                logger.error(f"[Email/Resend] ✗ Failed │ {err}")
                return False, err

        except httpx.TimeoutException as e:
            err = f"Resend API timeout: {e}"
            logger.error(f"[Email/Resend] ✗ Timeout │ {err}")
            return False, err
        except Exception as e:
            err = f"Resend API exception: {type(e).__name__}: {e}"
            logger.exception(f"[Email/Resend] ✗ Exception │ {err}")
            return False, err

    # ─────────────────────────────────────────────────────────────────────────
    # Layer 2: aiosmtplib SMTP with retry  (port 587/465)
    # ─────────────────────────────────────────────────────────────────────────
    async def _send_via_smtp_once(
        self, message: EmailMessage, to_email: str, attempt: int
    ) -> Tuple[bool, Optional[str]]:
        """Single SMTP attempt with full step-by-step diagnostic logging."""
        logger.info(
            f"[Email/SMTP] Attempt {attempt} │ "
            f"Connecting to {self.smtp_host}:{self.smtp_port} │ "
            f"use_tls={self.smtp_use_tls} │ start_tls={self.smtp_start_tls} │ "
            f"timeout={self.smtp_timeout}s │ to={to_email}"
        )

        smtp_client = SMTP(
            hostname=self.smtp_host,
            port=self.smtp_port,
            use_tls=self.smtp_use_tls,
            timeout=self.smtp_timeout,
        )

        try:
            logger.info(f"[Email/SMTP] Step 1/5 │ Opening TCP connection to {self.smtp_host}:{self.smtp_port}...")
            await smtp_client.connect()
            logger.info(f"[Email/SMTP] Step 1/5 │ ✓ TCP connection established.")

            if self.smtp_start_tls:
                logger.info("[Email/SMTP] Step 2/5 │ Initiating STARTTLS upgrade...")
                await smtp_client.starttls()
                logger.info("[Email/SMTP] Step 2/5 │ ✓ STARTTLS upgrade complete.")
            else:
                logger.info("[Email/SMTP] Step 2/5 │ Skipped STARTTLS (use_tls=True or not required).")

            if self.smtp_user and self.smtp_password:
                logger.info(f"[Email/SMTP] Step 3/5 │ Authenticating as {self.smtp_user}...")
                await smtp_client.login(self.smtp_user, self.smtp_password)
                logger.info("[Email/SMTP] Step 3/5 │ ✓ Authentication successful.")
            else:
                logger.warning("[Email/SMTP] Step 3/5 │ No credentials — skipping authentication.")

            logger.info(f"[Email/SMTP] Step 4/5 │ Sending message to {to_email}...")
            await smtp_client.send_message(message)
            logger.info(f"[Email/SMTP] Step 4/5 │ ✓ Message accepted by SMTP server.")

            logger.info("[Email/SMTP] Step 5/5 │ Sending QUIT...")
            await smtp_client.quit()
            logger.info("[Email/SMTP] Step 5/5 │ ✓ QUIT complete. Connection closed.")

            logger.info(f"[Email/SMTP] ✓ Email delivered successfully to {to_email}.")
            return True, None

        except asyncio.TimeoutError as e:
            tb = traceback.format_exc()
            err = (
                f"SMTP TIMEOUT on attempt {attempt} connecting to "
                f"{self.smtp_host}:{self.smtp_port} (timeout={self.smtp_timeout}s).\n"
                f"Error: {type(e).__name__}: {e}\n"
                f"Traceback:\n{tb}"
            )
            logger.error(f"[Email/SMTP] ✗ Timeout │ {err}")
            try:
                await smtp_client.quit()
            except Exception:
                pass
            return False, err

        except SMTPConnectError as e:
            tb = traceback.format_exc()
            err = (
                f"SMTP connection refused on attempt {attempt} to "
                f"{self.smtp_host}:{self.smtp_port}.\n"
                f"Error: {type(e).__name__}: {e}\n"
                f"Traceback:\n{tb}"
            )
            logger.error(f"[Email/SMTP] ✗ Connection refused │ {err}")
            return False, err

        except SMTPAuthenticationError as e:
            tb = traceback.format_exc()
            err = (
                f"SMTP authentication failed on attempt {attempt} for user '{self.smtp_user}'.\n"
                f"Error: {type(e).__name__}: {e}\n"
                f"Traceback:\n{tb}"
            )
            logger.error(f"[Email/SMTP] ✗ Auth failed │ {err}")
            return False, err

        except SMTPException as e:
            tb = traceback.format_exc()
            err = (
                f"SMTP protocol error on attempt {attempt}:\n"
                f"Error: {type(e).__name__}: {e}\n"
                f"Traceback:\n{tb}"
            )
            logger.error(f"[Email/SMTP] ✗ SMTP error │ {err}")
            try:
                await smtp_client.quit()
            except Exception:
                pass
            return False, err

        except OSError as e:
            tb = traceback.format_exc()
            err = (
                f"SMTP network error on attempt {attempt} to "
                f"{self.smtp_host}:{self.smtp_port}:\n"
                f"Error: {type(e).__name__}: {e}\n"
                f"Traceback:\n{tb}"
            )
            logger.error(f"[Email/SMTP] ✗ Network error │ {err}")
            return False, err

        except Exception as e:
            tb = traceback.format_exc()
            err = (
                f"Unexpected SMTP error on attempt {attempt}:\n"
                f"Error: {type(e).__name__}: {e}\n"
                f"Traceback:\n{tb}"
            )
            logger.exception(f"[Email/SMTP] ✗ Unexpected error │ {err}")
            try:
                await smtp_client.quit()
            except Exception:
                pass
            return False, err

    async def _send_via_smtp(
        self, message: EmailMessage, to_email: str
    ) -> Tuple[bool, Optional[str]]:
        """SMTP sender with 1 retry (2 attempts total) before giving up."""
        for attempt in range(1, 3):
            ok, err = await self._send_via_smtp_once(message, to_email, attempt)
            if ok:
                return True, None
            if attempt < 2:
                wait = 2.0
                logger.warning(
                    f"[Email/SMTP] Attempt {attempt} failed. "
                    f"Retrying in {wait}s... Error was: {err}"
                )
                await asyncio.sleep(wait)
        return False, err

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────
    async def send_email(
        self, to_email: str, subject: str, html_content: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Send an email using the best available method:

          1. Resend HTTP API (RESEND_API_KEY set) — works on Render Free tier
          2. aiosmtplib SMTP with retry             — requires paid Render or local
          3. Console / DEV MODE                     — no credentials at all

        Returns (success: bool, error_message: Optional[str])
        """
        import re

        # ── Helper: strip HTML → plain text ──────────────────────────────────
        def to_plain_text(html: str) -> str:
            t = re.sub(r"<(script|style)\b[^>]*>([\s\S]*?)<\/\1>", "", html, flags=re.IGNORECASE)
            t = re.sub(r"</?(p|div|h[1-6]|li|tr|br/?)>", "\n", t, flags=re.IGNORECASE)
            t = re.sub(r"<[^>]+>", "", t)
            t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            return re.sub(r"\n\s*\n", "\n\n", t).strip()

        # ── Layer 3: DEV MODE ─────────────────────────────────────────────────
        if not self.smtp_user and not self.smtp_password and not self.resend_api_key:
            plain = to_plain_text(html_content)
            logger.warning(
                "\n" + "=" * 60 +
                f"\n[Email/DevMode] No credentials configured — email NOT sent.\n"
                f"To: {to_email}\nSubject: {subject}\n\n{plain}\n" +
                "=" * 60
            )
            return True, None

        # ── Layer 1: Resend HTTP API (primary for Render) ─────────────────────
        resend_err = None
        if self.resend_api_key:
            ok, err = await self._send_via_resend(to_email, subject, html_content)
            if ok:
                return True, None
            resend_err = err
            logger.warning(
                f"[Email] Resend failed ({err}). "
                "Falling back to SMTP..."
            )

        # ── Layer 2: aiosmtplib SMTP ──────────────────────────────────────────
        if self.smtp_user and self.smtp_password:
            message = EmailMessage()
            message["From"]    = f"{self.from_name} <{self.from_email}>"
            message["To"]      = to_email
            message["Subject"] = subject
            message.set_content(to_plain_text(html_content))
            message.add_alternative(html_content, subtype="html")

            smtp_ok, smtp_err = await self._send_via_smtp(message, to_email)
            if smtp_ok:
                return True, None
            
            if resend_err:
                combined_err = f"Resend failed ({resend_err}) and SMTP fallback failed ({smtp_err})"
                logger.error(f"[Email] ✗ {combined_err}")
                return False, combined_err
            return False, smtp_err

        # ── If Resend failed and no SMTP credentials ──────────────────────────
        if resend_err:
            logger.error(f"[Email] ✗ Resend failed: {resend_err}")
            return False, resend_err

        err = (
            "Email not sent: No credentials (RESEND_API_KEY or SMTP) are configured. "
            "Set RESEND_API_KEY on Render to deliver emails."
        )
        logger.error(f"[Email] ✗ {err}")
        return False, err



    async def send_otp_email(self, to_email: str, otp: str, name: str = None) -> (bool, Optional[str]):
        template = Template(OTP_EMAIL_TEMPLATE)
        html = template.render(otp=otp, name=name)
        return await self.send_email(
            to_email=to_email, subject="Your By8flow Sign-In Code", html_content=html
        )

    async def send_welcome_email(self, to_email: str, name: str, verify_url: str) -> (bool, Optional[str]):
        template = Template(WELCOME_EMAIL_TEMPLATE)
        html = template.render(name=name, verify_url=verify_url)
        return await self.send_email(
            to_email=to_email, subject="Your By8flow Account Has Been Created", html_content=html
        )

    async def send_notification_email(
        self,
        to_email: str,
        user_name: str,
        notification_type: str,
        notification_title: str,
        notification_message: str,
        action_link: str = None,
    ) -> (bool, Optional[str]):
        """Send a generic notification email to the user."""
        # Prepend frontend URL for relative links so email clients resolve correctly
        link = action_link or ""
        if link and link.startswith("/"):
            link = urllib.parse.urljoin(settings.frontend_url, link)
        template = Template(NOTIFICATION_EMAIL_TEMPLATE)
        html = template.render(
            user_name=user_name,
            notification_type=notification_type.upper(),
            notification_title=notification_title,
            notification_message=notification_message,
            action_link=link,
        )
        subject = f"By8flow: {notification_title}"
        return await self.send_email(to_email=to_email, subject=subject, html_content=html)


email_service = EmailService()
