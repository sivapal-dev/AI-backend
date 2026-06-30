from datetime import datetime, timedelta, timezone
from typing import Optional
from bson import ObjectId
from database import get_database
from models.user import UserCreate, UserInDB, UserRole
from utils.security import (
    generate_otp,
    hash_otp,
    verify_otp,
    create_access_token,
    create_refresh_token,
    hash_jti,
)
from services.email_service import email_service
from config import get_settings
import logging
import secrets

settings = get_settings()
logger = logging.getLogger(__name__)


class AuthService:
    OTP_EXPIRY_MINUTES = 15
    MAX_VERIFICATION_ATTEMPTS = 5

    def _validate_email(self, email: str) -> bool:
        import re

        pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        return bool(re.match(pattern, email))

    async def request_otp(self, email: str, name: Optional[str] = None, ip_address: Optional[str] = None) -> dict:
        logger.info(f"[Auth] Stage 1: Request received for email: {email}, IP: {ip_address}")
        if not self._validate_email(email):
            logger.warning(f"[Auth] OTP request validation failed for: {email}")
            return {
                "success": False,
                "error": "Invalid email format. Please enter a valid email address.",
            }
        logger.info(f"[Auth] Stage 2: Email validated successfully for: {email}")

        database = get_database()
        users_collection = database.users

        # Check if user exists
        existing_user = await users_collection.find_one({"email": email})
        
        # To prevent user enumeration, return a generic success message if not found
        if existing_user is None:
            logger.info(f"[Auth] User {email} not found. Returning success response to prevent user enumeration.")
            result = {
                "success": True,
                "message": "If your email is registered, we have sent a 6-digit verification code.",
                "is_new_user": False,
            }
            logger.info(f"[Auth] Stage 8: Response returned: {result}")
            return result

        is_new_user = False  # Self-registration disabled

        # Generate OTP
        otp = generate_otp()
        hashed_otp = hash_otp(otp)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=self.OTP_EXPIRY_MINUTES)
        logger.info(f"[Auth] Stage 4: OTP generated for {email}")

        # Update existing user's OTP
        await users_collection.update_one(
            {"email": email},
            {
                "$set": {
                    "verification_token": hashed_otp,
                    "verification_token_expires": expires_at,
                    "verification_attempts": 0,
                    "last_otp_request": datetime.now(timezone.utc),
                }
            },
        )
        logger.info(f"[Auth] Stage 5: OTP saved to MongoDB for {email}")

        user_name = existing_user.get("name")

        # Check Redis availability (dev helper logging)
        from redis_client import get_redis
        r_client = get_redis()
        if r_client is None:
            logger.info(f"[Auth] Redis is UNAVAILABLE. Caching disabled, utilizing MongoDB fallback for OTP.")
        else:
            logger.info(f"[Auth] Redis is AVAILABLE.")

        # Send OTP email
        try:
            email_sent, email_err = await email_service.send_otp_email(email, otp, user_name)
        except Exception as email_ex:
            logger.error(f"[Auth] Exception raised during send_otp_email: {email_ex}")
            email_sent, email_err = False, str(email_ex)

        if not email_sent:
            logger.error(f"[Auth] OTP email transmission failed to {email}. Triggering database state rollback.")
            
            # Revert OTP fields in the user document to prevent verification of unsent OTP
            await users_collection.update_one(
                {"email": email},
                {
                    "$set": {
                        "verification_token": existing_user.get("verification_token"),
                        "verification_token_expires": existing_user.get("verification_token_expires"),
                        "verification_attempts": existing_user.get("verification_attempts", 0),
                        "last_otp_request": existing_user.get("last_otp_request"),
                    }
                },
            )
            logger.info(f"[Auth] Database rollback completed successfully for {email}")
            result = {
                "success": False,
                "error": f"Failed to send OTP email. Reason: {email_err}",
            }
            logger.info(f"[Auth] Stage 8: Response returned: {result}")
            return result

        result = {
            "success": True,
            "message": "If your email is registered, we have sent a 6-digit verification code.",
            "is_new_user": is_new_user,
        }

        # Dev mode: include OTP in response when SMTP is not configured
        if not settings.smtp_user or not settings.smtp_password:
            logger.info(f"[Auth] SMTP credentials not set. Falling back to DEV MODE to return dev_otp.")
            result["dev_otp"] = otp

        logger.info(f"[Auth] Stage 8: Response returned: {result}")
        return result

    async def verify_otp(self, email: str, otp: str) -> dict:
        database = get_database()
        users_collection = database.users

        user = await users_collection.find_one({"email": email})
        if not user:
            logger.warning(f"[Auth] User not found: {email}")
            return {"success": False, "error": "User not found"}

        # Check verification attempts
        attempts = user.get("verification_attempts", 0)
        if attempts >= self.MAX_VERIFICATION_ATTEMPTS:
            logger.warning(f"[Auth] Too many attempts: {attempts}")
            await users_collection.update_one(
                {"email": email},
                {"$set": {"verification_token": None, "verification_token_expires": None}}
            )
            return {
                "success": False,
                "error": "Too many failed attempts. Please request a new OTP.",
            }

        # Check if OTP has expired
        token_expires = user.get("verification_token_expires")
        if token_expires and token_expires.tzinfo is None:
            token_expires = token_expires.replace(tzinfo=timezone.utc)
        if not token_expires or token_expires < datetime.now(timezone.utc):
            logger.warning(
                f"[Auth] OTP expired. Expires: {token_expires}, Now: {datetime.now(timezone.utc)}"
            )
            return {
                "success": False,
                "error": "OTP has expired. Please request a new one.",
            }

        # Verify OTP
        stored_hash = user.get("verification_token")
        logger.debug(f"[Auth] Stored hash: {stored_hash[:50] if stored_hash else 'None'}...")
        logger.debug(f"[Auth] Input OTP: {otp}")
        logger.debug(f"[Auth] verification_attempts: {attempts}")

        if not stored_hash:
            logger.error("[Auth] ERROR: No stored hash found - OTP was not set properly")
            return {"success": False, "error": "Invalid OTP. Please try again."}

        is_valid = verify_otp(otp, stored_hash)
        logger.debug(f"[Auth] OTP valid: {is_valid}")

        if not is_valid:
            new_attempts = attempts + 1
            update_fields = {"verification_attempts": new_attempts}
            if new_attempts >= self.MAX_VERIFICATION_ATTEMPTS:
                update_fields["verification_token"] = None
                update_fields["verification_token_expires"] = None
            await users_collection.update_one(
                {"email": email}, {"$set": update_fields}
            )
            return {"success": False, "error": "Invalid OTP. Please try again."}

        # OTP verified - clear OTP requests and reset rate limit for this email
        try:
            from dependencies import reset_rate_limit_for_email
            await reset_rate_limit_for_email(email)
            await database.otp_requests.delete_many({"email": email})
        except Exception as e:
            logger.error(f"[Auth] Failed to reset rate limits on verification: {e}")

        # OTP verified - create tokens
        user_id = str(user["_id"])
        token_data = {"sub": user_id, "email": email}

        access_token = create_access_token(token_data)
        refresh_token, jti = create_refresh_token(token_data)

        # Store refresh token jti hash for rotation detection
        await users_collection.update_one(
            {"_id": user["_id"]},
            {
                "$push": {
                    "refresh_token_hashes": {
                        "$each": [{
                            "hash": hash_jti(jti),
                            "created_at": datetime.now(timezone.utc),
                        }],
                        "$slice": -10
                    }
                }
            },
        )

        # Update user status
        await users_collection.update_one(
            {"email": email},
            {
                "$set": {
                    "email_verified": True,
                    "verification_token": None,
                    "verification_token_expires": None,
                    "verification_attempts": 0,
                    "last_login": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

        return {
            "success": True,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "user": {
                "id": user_id,
                "email": user["email"],
                "name": user["name"],
                "role": user["role"],
                "avatar": user.get("avatar"),
                "email_verified": True,
            },
        }

    async def refresh_tokens(self, refresh_token: str) -> dict:
        from utils.security import decode_token
        from jose import jwt

        try:
            payload = decode_token(refresh_token)
        except jwt.ExpiredSignatureError:
            return {"success": False, "error": "Refresh token expired"}
        except jwt.JWTError:
            return {"success": False, "error": "Invalid refresh token"}

        if payload.get("type") != "refresh":
            return {"success": False, "error": "Invalid refresh token"}

        user_id = payload.get("sub")
        email = payload.get("email")
        jti = payload.get("jti")

        if not jti:
            return {"success": False, "error": "Invalid refresh token"}

        database = get_database()
        user = await database.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            return {"success": False, "error": "User not found"}

        stored_hashes = user.get("refresh_token_hashes", [])
        if not isinstance(stored_hashes, list):
            stored_hashes = []
        incoming_hash = hash_jti(jti)
        matched = any(entry["hash"] == incoming_hash for entry in stored_hashes)

        if not matched:
            # Reuse detected — someone is replaying an already-consumed token
            # Invalidate ALL refresh tokens for this user as a security measure
            await database.users.update_one(
                {"_id": ObjectId(user_id)},
                {"$set": {"refresh_token_hashes": []}},
            )
            logger.warning(f"[Auth] Refresh token reuse detected for user {user_id} — all tokens invalidated")
            
            # Send security email notification
            await email_service.send_notification_email(
                to_email=user["email"],
                user_name=user.get("name", "User"),
                notification_type="security_alert",
                notification_title="All active sessions terminated",
                notification_message="An unauthorized attempt to reuse a session token was detected. For your security, all of your active sessions have been terminated. Please log in again and check your account security settings.",
                action_link="/otp-request"
            )
            
            # Send security alert email
            try:
                alert_html = (
                    f"<p>Hello {user.get('name', 'User')},</p>"
                    f"<p><strong>Security Alert:</strong> A refresh token reuse attempt was detected on your account.</p>"
                    f"<p>As a security precaution, all active sessions for your account have been invalidated, and you have been logged out.</p>"
                    f"<p>If this was not you, we recommend that you change your password immediately.</p>"
                    f"<p>Best regards,<br/>by8flow Security Team</p>"
                )
                await email_service.send_email(
                    to_email=email,
                    subject="Security Alert: Potential account compromise detected",
                    html_content=alert_html
                )
            except Exception as email_err:
                logger.error(f"[Auth] Failed to send token reuse security alert email: {email_err}")

            return {"success": False, "error": "Refresh token has already been used. Please log in again."}


        # Remove the consumed jti hash and issue new tokens
        await database.users.update_one(
            {"_id": ObjectId(user_id), "refresh_token_hashes": {"$type": "array"}},
            {"$pull": {"refresh_token_hashes": {"hash": incoming_hash}}},
        )

        token_data = {"sub": user_id, "email": email}
        new_access_token = create_access_token(token_data)
        new_refresh_token, new_jti = create_refresh_token(token_data)

        # Store the new jti hash
        await database.users.update_one(
            {"_id": ObjectId(user_id)},
            {
                "$push": {
                    "refresh_token_hashes": {
                        "$each": [{
                            "hash": hash_jti(new_jti),
                            "created_at": datetime.now(timezone.utc),
                        }],
                        "$slice": -10
                    }
                }
            },
        )

        return {
            "success": True,
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "token_type": "bearer",
        }

    def _generate_welcome_token(self) -> str:
        """Generate a secure random token for welcome email"""
        return secrets.token_urlsafe(32)

    async def send_welcome_email(self, email: str, name: str, welcome_token: str) -> dict:
        """Send welcome email with verification link"""
        settings = get_settings()
        app_url = settings.frontend_url
        verify_url = f"{app_url}/otp-verify?email={email}&welcome_token={welcome_token}"

        email_sent, email_err = await email_service.send_welcome_email(email, name, verify_url)

        if not email_sent:
            logger.warning(f"Failed to send welcome email to {email}: {email_err}")
            return {"success": False, "error": f"Failed to send welcome email. Reason: {email_err}"}

        return {"success": True, "message": "Welcome email sent"}

    async def verify_welcome_token(self, email: str, welcome_token: str) -> dict:
        """Verify welcome token and log user in (no OTP required)"""
        database = get_database()
        users_collection = database.users

        user = await users_collection.find_one({"email": email})
        if not user:
            logger.warning(f"[Auth] User not found for welcome verification: {email}")
            return {"success": False, "error": "User not found"}

        # Validate welcome token
        stored_token = user.get("welcome_token")
        token_expires = user.get("welcome_token_expires")

        if not stored_token or stored_token != welcome_token:
            logger.warning(f"[Auth] Invalid welcome token for {email}")
            return {"success": False, "error": "Invalid or expired welcome link"}

        # Ensure token_expires is timezone-aware for comparison
        if token_expires and token_expires.tzinfo is None:
            token_expires = token_expires.replace(tzinfo=timezone.utc)
        if not token_expires or token_expires < datetime.now(timezone.utc):
            logger.warning(f"[Auth] Welcome token expired for {email}. Expires: {token_expires}, Now: {datetime.now(timezone.utc)}")
            return {"success": False, "error": "Welcome link has expired. Please request a new one."}

        # Token is valid — log user in
        user_id = str(user["_id"])
        token_data = {"sub": user_id, "email": email}

        access_token = create_access_token(token_data)
        refresh_token, jti = create_refresh_token(token_data)

        # Store refresh token jti hash for rotation detection
        await users_collection.update_one(
            {"_id": user["_id"]},
            {
                "$push": {
                    "refresh_token_hashes": {
                        "$each": [{
                            "hash": hash_jti(jti),
                            "created_at": datetime.now(timezone.utc),
                        }],
                        "$slice": -10
                    }
                }
            },
        )

        # Update user: mark verified, clear welcome token, set last_login
        await users_collection.update_one(
            {"email": email},
            {
                "$set": {
                    "email_verified": True,
                    "welcome_token": None,
                    "welcome_token_expires": None,
                    "last_login": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )

        logger.info(f"[Auth] User {email} verified via welcome token")

        return {
            "success": True,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "user": {
                "id": user_id,
                "email": user["email"],
                "name": user["name"],
                "role": user["role"],
                "avatar": user.get("avatar"),
                "email_verified": True,
            },
        }

    async def get_user_by_id(self, user_id: str) -> Optional[dict]:
        from bson.errors import InvalidId
        database = get_database()
        try:
            oid = ObjectId(user_id)
        except (InvalidId, TypeError, ValueError):
            return None
        user = await database.users.find_one({"_id": oid})
        if user:
            user["id"] = str(user.pop("_id"))
        return user

    async def change_password(
        self, user_id: str, current_password: str, new_password: str
    ) -> dict:
        from utils.security import hash_password, verify_password

        database = get_database()
        user = await database.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            return {"success": False, "error": "User not found"}

        if not user.get("email_verified"):
            return {"success": False, "error": "Email not verified"}

        if not verify_password(current_password, user.get("password_hash", "")):
            return {"success": False, "error": "Current password is incorrect"}

        hashed = hash_password(new_password)
        await database.users.update_one(
            {"_id": ObjectId(user_id)},
            {
                "$set": {
                    "password_hash": hashed,
                    "updated_at": datetime.now(timezone.utc),
                    "refresh_token_hashes": [],
                }
            },
        )
        return {"success": True}


auth_service = AuthService()
