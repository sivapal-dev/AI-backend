from datetime import datetime, timedelta, timezone
from typing import Optional
import secrets
import string
import hashlib
from jose import jwt
import bcrypt
from config import get_settings

def generate_otp(length: int = 6) -> str:
    """Generate a secure numeric OTP"""
    return "".join(secrets.choice(string.digits) for _ in range(length))


def hash_otp(otp: str) -> str:
    settings = get_settings()
    salt = settings.jwt_secret
    return hashlib.sha256(f"{salt}:{otp}".encode()).hexdigest()


def verify_otp(plain_otp: str, hashed_otp: str) -> bool:
    import hmac
    calculated_hash = hash_otp(plain_otp)
    return hmac.compare_digest(calculated_hash, hashed_otp)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    settings = get_settings()
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.access_token_expire_minutes
        )
    to_encode.update({"exp": expire, "type": "access"})
    encoded_jwt = jwt.encode(
        to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm
    )
    return encoded_jwt


def create_refresh_token(data: dict) -> tuple[str, str]:
    settings = get_settings()
    to_encode = data.copy()
    jti = secrets.token_hex(32)
    expire = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    to_encode.update({"exp": expire, "type": "refresh", "jti": jti})
    encoded_jwt = jwt.encode(
        to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm
    )
    return encoded_jwt, jti


def hash_jti(jti: str) -> str:
    return hashlib.sha256(jti.encode()).hexdigest()


import logging
logger = logging.getLogger(__name__)

def decode_token(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(
        token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())
