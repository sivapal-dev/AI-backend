import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from main import app
from database import get_database, connect_db, close_db
from datetime import datetime, timezone, timedelta
from utils.security import generate_otp, hash_otp, verify_otp
from services.email_service import email_service
from services.auth_service import auth_service

async def test_otp_generation():
    print("Testing OTP generation...")
    otp = generate_otp(6)
    assert len(otp) == 6, f"Expected length 6, got {len(otp)}"
    assert otp.isdigit(), f"Expected digits only, got {otp}"
    # Randomness check
    otps = {generate_otp(6) for _ in range(100)}
    assert len(otps) > 90, f"Expected highly random OTPs, got only {len(otps)} unique ones out of 100"
    print("[OK] OTP generation is correct.")

async def test_email_sending(test_email, test_name):
    print("Testing OTP email sending...")
    with patch("services.email_service.SMTP") as mock_smtp_class:
        mock_smtp = mock_smtp_class.return_value
        mock_smtp.connect = AsyncMock()
        mock_smtp.starttls = AsyncMock()
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()
        mock_smtp.quit = AsyncMock()
        with patch.object(email_service, "smtp_user", "test_user"), \
             patch.object(email_service, "smtp_password", "test_pass"):
            success, err = await email_service.send_otp_email(test_email, "123456", test_name)
            assert success, f"Expected send_otp_email to return True, got {err}"
            assert mock_smtp.connect.called, "Expected SMTP.connect to be called"
            assert mock_smtp.send_message.called, "Expected SMTP.send_message to be called"
    print("[OK] Email sending logic works (mocked).")

async def test_otp_storage_security(db, test_email, test_name):
    print("Testing OTP storage security...")
    # Clear previous requests
    await db.otp_requests.delete_many({"email": test_email})
    await db.users.update_one({"email": test_email}, {"$set": {"verification_token": None, "verification_token_expires": None}})

    with patch("services.email_service.email_service.send_otp_email", return_value=(True, None)) as mock_send:
        result = await auth_service.request_otp(test_email, test_name)
        assert result["success"], f"Request OTP failed: {result.get('error')}"
        
        user = await db.users.find_one({"email": test_email})
        stored_hash = user.get("verification_token")
        assert stored_hash is not None, "verification_token should be set in user document"
        
        # Ensure plain text OTP is NOT stored in the database
        dev_otp = result.get("dev_otp") or mock_send.call_args[0][1]
        for key, val in user.items():
            assert val != dev_otp, f"Plain OTP should not be stored in key '{key}'"
            
        assert verify_otp(dev_otp, stored_hash), "Stored hash should match generated OTP"
    print("[OK] OTP is stored securely (hashed).")

async def test_expired_otp_rejection(db, test_email, test_name):
    print("Testing expired OTP rejection...")
    await db.otp_requests.delete_many({"email": test_email})
    with patch("services.email_service.email_service.send_otp_email", return_value=(True, None)) as mock_send:
        result = await auth_service.request_otp(test_email, test_name)
        dev_otp = result.get("dev_otp") or mock_send.call_args[0][1]
        
        # Manually expire in DB
        expired_time = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.users.update_one(
            {"email": test_email},
            {"$set": {"verification_token_expires": expired_time}}
        )
        
        verify_result = await auth_service.verify_otp(test_email, dev_otp)
        assert not verify_result["success"], "Expected expired OTP verification to fail"
        assert verify_result["error"] == "OTP has expired. Please request a new one.", f"Unexpected error msg: {verify_result.get('error')}"
    print("[OK] Expired OTPs are rejected correctly.")

async def test_verify_succeeds_only_with_latest(db, test_email, test_name):
    print("Testing verification with latest OTP only...")
    await db.otp_requests.delete_many({"email": test_email})
    with patch("services.email_service.email_service.send_otp_email", return_value=(True, None)) as mock_send:
        # First request
        res1 = await auth_service.request_otp(test_email, test_name)
        otp1 = res1.get("dev_otp") or mock_send.call_args_list[0][0][1]
        
        # Second request
        res2 = await auth_service.request_otp(test_email, test_name)
        otp2 = res2.get("dev_otp") or mock_send.call_args_list[1][0][1]
        
        # Try verifying first OTP (should fail)
        verify1 = await auth_service.verify_otp(test_email, otp1)
        assert not verify1["success"], "Older OTP should have been invalidated"
        
        # Try verifying second OTP (should succeed)
        verify2 = await auth_service.verify_otp(test_email, otp2)
        assert verify2["success"], f"Latest OTP verification failed: {verify2.get('error')}"
    print("[OK] Only the latest OTP is accepted.")

async def test_invalid_otp_attempts_lockout(db, test_email, test_name):
    print("Testing invalid OTP attempts and lockout...")
    await db.otp_requests.delete_many({"email": test_email})
    # Request OTP
    with patch("services.email_service.email_service.send_otp_email", return_value=(True, None)) as mock_send:
        res = await auth_service.request_otp(test_email, test_name)
        dev_otp = res.get("dev_otp") or mock_send.call_args[0][1]
        
        # Wrong code
        verify_wrong = await auth_service.verify_otp(test_email, "000000")
        assert not verify_wrong["success"], "Wrong OTP should fail"
        assert verify_wrong["error"] == "Invalid OTP. Please try again."
        
        user = await db.users.find_one({"email": test_email})
        assert user.get("verification_attempts") == 1, f"Expected 1 attempt, got {user.get('verification_attempts')}"
        
        # 4 more failed attempts
        for i in range(4):
            await auth_service.verify_otp(test_email, "000000")
            
        # Verify lockout
        verify_lockout = await auth_service.verify_otp(test_email, dev_otp)
        assert not verify_lockout["success"], "Should be locked out on 6th attempt even with correct OTP"
        assert verify_lockout["error"] == "Too many failed attempts. Please request a new OTP.", f"Got error: {verify_lockout.get('error')}"
        
        # Check tokens cleared in DB
        user = await db.users.find_one({"email": test_email})
        assert user.get("verification_token") is None, "Verification token should be cleared after lockout"
        assert user.get("verification_token_expires") is None, "Token expires should be cleared after lockout"
    print("[OK] Lockout on invalid OTP attempts is working correctly.")

async def test_rate_limiting(db, test_email, test_name):
    print("Testing OTP rate limiting...")
    # Clear any rate limit entries for this email in Redis/Mongo/Memory
    from dependencies import rate_limiter, reset_rate_limit_for_email
    from fastapi import Request, HTTPException
    from starlette.requests import Request as StarletteRequest

    await reset_rate_limit_for_email(test_email)

    body = f'{{"email": "{test_email}", "name": "{test_name}"}}'.encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/request-otp",
        "headers": [(b"content-length", str(len(body)).encode())],
        "client": ("8.8.8.8", 12345),
    }
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    # We patch client IP to something other than localhost (e.g. "8.8.8.8") to trigger rate limiting
    with patch("dependencies.get_client_ip", return_value="8.8.8.8"), \
         patch("redis_client.get_redis", return_value=None), \
         patch("dependencies.get_database", return_value=db):
        
        limiter = rate_limiter(max_requests=5, window_seconds=60, email_field="email")
        
        # 5 requests should pass
        for i in range(5):
            req = StarletteRequest(scope=scope, receive=receive)
            await limiter(req)
            
        # 6th request must fail with 429
        req_blocked = StarletteRequest(scope=scope, receive=receive)
        try:
            await limiter(req_blocked)
            assert False, "Expected 6th request to be blocked by rate limit"
        except HTTPException as exc:
            assert exc.status_code == 429, f"Expected 429, got {exc.status_code}"
            assert "Rate limit exceeded" in exc.detail
        
    print("[OK] OTP rate limiting protects against abuse.")


async def test_rollback_on_email_failure(db, test_email, test_name):
    print("Testing OTP rollback on email sending failure...")
    from dependencies import rate_limiter, rollback_rate_limit, reset_rate_limit_for_email
    from fastapi import Request, HTTPException
    from starlette.requests import Request as StarletteRequest
    from routers.auth import request_otp as request_otp_route
    from models.user import OTPRequest

    await reset_rate_limit_for_email(test_email)
    
    await db.users.update_one(
        {"email": test_email},
        {
            "$set": {
                "verification_token": "original_token",
                "verification_token_expires": datetime.now(timezone.utc) + timedelta(minutes=5),
                "verification_attempts": 2,
                "last_otp_request": datetime.now(timezone.utc) - timedelta(minutes=10)
            }
        }
    )

    body = f'{{"email": "{test_email}", "name": "{test_name}"}}'.encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/request-otp",
        "headers": [(b"content-length", str(len(body)).encode())],
        "client": ("8.8.8.8", 12345),
    }
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    # Force email sending to fail
    with patch("dependencies.get_client_ip", return_value="8.8.8.8"), \
         patch("redis_client.get_redis", return_value=None), \
         patch("dependencies.get_database", return_value=db), \
         patch("services.email_service.email_service.send_otp_email", return_value=(False, "Failed to send OTP email")):
        
        # 1. Run the route-level rate limiter first to simulate dependency injection
        req = StarletteRequest(scope=scope, receive=receive)
        limiter = rate_limiter(max_requests=5, window_seconds=60, email_field="email")
        await limiter(req)
        
        # Ensure state metadata is set
        assert getattr(req.state, "rate_limit_key", None) is not None
        assert getattr(req.state, "rate_limit_layer", None) == "mongo"
        
        # 2. Call the route handler function manually
        otp_req_model = OTPRequest(email=test_email, name=test_name)
        try:
            await request_otp_route(otp_req_model, req, _rate_limit=None)
            assert False, "Expected route to raise HTTPException on email sending failure"
        except HTTPException as exc:
            assert exc.status_code == 400
            assert "Failed to send OTP email" in exc.detail

        # 3. User's OTP states should be rolled back to their previous values in users collection
        user = await db.users.find_one({"email": test_email})
        assert user.get("verification_token") == "original_token", f"Expected verification_token to be 'original_token', got {user.get('verification_token')}"
        assert user.get("verification_attempts") == 2, f"Expected attempts to be 2, got {user.get('verification_attempts')}"

    # 4. Rate limit should NOT have been consumed. We can check this by verifying
    # we can still make 5 successful requests (under successful email patch).
    with patch("dependencies.get_client_ip", return_value="8.8.8.8"), \
         patch("redis_client.get_redis", return_value=None), \
         patch("dependencies.get_database", return_value=db), \
         patch("services.email_service.email_service.send_otp_email", return_value=(True, None)):
         
        for i in range(5):
            req = StarletteRequest(scope=scope, receive=receive)
            await limiter(req)
            otp_req_model = OTPRequest(email=test_email, name=test_name)
            await request_otp_route(otp_req_model, req, _rate_limit=None)

        # 6th should be blocked now
        req_blocked = StarletteRequest(scope=scope, receive=receive)
        try:
            await limiter(req_blocked)
            assert False, "Expected 6th request to be blocked by rate limit after 5 allowed ones"
        except HTTPException as exc:
            assert exc.status_code == 429

    print("[OK] OTP rate limit and user state successfully rolled back on email sending failure.")


async def test_mongodb_rate_limit_fallback(db):
    print("Testing MongoDB rate limiter fallback (Redis offline simulation)...")
    from dependencies import rate_limiter
    from fastapi import Request, HTTPException
    from unittest.mock import Mock

    # Clear mongo rate limits first
    await db.rate_limits.delete_many({"_id": "ratelimit:/api/test-route:9.9.9.9"})

    # Create dummy FastAPI request
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/test-route",
        "headers": [],
        "client": ("9.9.9.9", 12345),
    }
    request = Request(scope=scope)

    # Simulate Redis offline (get_redis returns None)
    with patch("redis_client.get_redis", return_value=None), \
         patch("dependencies.get_database", return_value=db):
        
        limiter = rate_limiter(max_requests=2, window_seconds=60)
        
        # Request 1: should pass
        await limiter(request)
        
        # Request 2: should pass
        await limiter(request)

        # Request 3: should fail with 429
        try:
            await limiter(request)
            assert False, "Expected 3rd request to fail when limit is 2"
        except HTTPException as exc:
            assert exc.status_code == 429, f"Expected 429 status code, got {exc.status_code}"
            assert "Rate limit exceeded" in exc.detail
            
        # Verify rate limit entries exist in MongoDB
        limit_doc = await db.rate_limits.find_one({"_id": "ratelimit:/api/test-route:9.9.9.9"})
        assert limit_doc is not None, "Expected rate limit document in MongoDB rate_limits collection"
        # IMPORTANT: The blocked (3rd) request must NOT push a timestamp.
        # Only the 2 allowed requests should be recorded.
        assert len(limit_doc["timestamps"]) == 2, (
            f"Expected 2 timestamps (only allowed requests are recorded), "
            f"got {len(limit_doc['timestamps'])}"
        )

    print("[OK] MongoDB rate limiter fallback successfully limits requests when Redis is down.")


async def test_first_request_always_allowed(db):
    """
    CRITICAL: Even after a previous deploy left stale timestamps in MongoDB,
    the very first request in a fresh window MUST be allowed (count=0).
    Verifies the stale-document purge in check_mongo_rate_limit.
    """
    print("Testing: first request always allowed (stale-data simulation)...")
    from dependencies import rate_limiter
    from fastapi import Request, HTTPException

    ip = "8.8.8.8"
    email = "freshuser@test.com"
    key = f"ratelimit:/api/auth/request-otp:{email}:{ip}"

    # Pre-seed with expired timestamps (simulates stale Render deploy data)
    from datetime import datetime, timezone, timedelta
    expired_ts = datetime.now(timezone.utc) - timedelta(seconds=120)
    await db.rate_limits.delete_one({"_id": key})
    await db.rate_limits.insert_one({
        "_id": key,
        "timestamps": [expired_ts, expired_ts, expired_ts, expired_ts, expired_ts],
        "updated_at": expired_ts,
    })

    # Build a request that simulates a fresh user
    body = b'{"email": "freshuser@test.com"}'
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/request-otp",
        "headers": [(b"content-length", str(len(body)).encode())],
        "client": (ip, 9999),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    from starlette.requests import Request as StarletteRequest
    request = StarletteRequest(scope=scope, receive=receive)

    with patch("redis_client.get_redis", return_value=None), \
         patch("dependencies.get_database", return_value=db):
        limiter = rate_limiter(max_requests=5, window_seconds=60, email_field="email")
        try:
            await limiter(request)
        except HTTPException as exc:
            assert False, (
                f"CRITICAL: First OTP request was BLOCKED with 429! "
                f"Stale data was NOT purged. detail={exc.detail}"
            )

    # Cleanup
    await db.rate_limits.delete_one({"_id": key})
    print("[OK] First request always allowed even with stale MongoDB data.")


async def test_email_ip_isolation(db):
    """
    Two different users from the same IP must NOT block each other.
    User A at max limit must not prevent User B's first request.
    """
    print("Testing: email+IP isolation (different users, same IP)...")
    from dependencies import rate_limiter
    from fastapi import Request, HTTPException

    shared_ip = "203.0.113.10"
    email_a = "user_a@test.com"
    email_b = "user_b@test.com"
    key_a = f"ratelimit:/api/auth/request-otp:{email_a}:{shared_ip}"
    key_b = f"ratelimit:/api/auth/request-otp:{email_b}:{shared_ip}"

    await db.rate_limits.delete_one({"_id": key_a})
    await db.rate_limits.delete_one({"_id": key_b})

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    # Pre-seed user A's key at exactly the limit
    await db.rate_limits.insert_one({
        "_id": key_a,
        "timestamps": [now, now, now, now, now],  # 5 requests = at limit
        "updated_at": now,
    })

    # User A’s 6th request should be blocked
    body_a = b'{"email": "user_a@test.com"}'
    scope_a = {
        "type": "http", "method": "POST",
        "path": "/api/auth/request-otp",
        "headers": [(b"content-length", str(len(body_a)).encode())],
        "client": (shared_ip, 1111),
    }
    async def receive_a():
        return {"type": "http.request", "body": body_a, "more_body": False}
    from starlette.requests import Request as SR
    req_a = SR(scope=scope_a, receive=receive_a)

    # User B’s first request should be ALLOWED
    body_b = b'{"email": "user_b@test.com"}'
    scope_b = {
        "type": "http", "method": "POST",
        "path": "/api/auth/request-otp",
        "headers": [(b"content-length", str(len(body_b)).encode())],
        "client": (shared_ip, 2222),
    }
    async def receive_b():
        return {"type": "http.request", "body": body_b, "more_body": False}
    req_b = SR(scope=scope_b, receive=receive_b)

    with patch("redis_client.get_redis", return_value=None), \
         patch("dependencies.get_database", return_value=db):
        limiter = rate_limiter(max_requests=5, window_seconds=60, email_field="email")

        # User A should be blocked (at limit)
        try:
            await limiter(req_a)
            assert False, "User A should have been blocked (at limit)"
        except HTTPException as exc:
            assert exc.status_code == 429, f"Expected 429 for User A, got {exc.status_code}"

        # User B should be allowed (different key)
        try:
            await limiter(req_b)
        except HTTPException as exc:
            assert False, (
                f"CRITICAL: User B was blocked by User A’s rate limit! "
                f"Email+IP isolation is broken. detail={exc.detail}"
            )

    # Cleanup
    await db.rate_limits.delete_one({"_id": key_a})
    await db.rate_limits.delete_one({"_id": key_b})
    print("[OK] Email+IP isolation: users from the same IP are rate-limited independently.")

async def test_resend_integration(db):
    """
    Test Resend HTTP API sending:
      1. Success path (mock response code 200)
      2. Failure path (mock response code 400 with API error details)
      3. Fallback path (mock Resend failure, verify SMTP fallback is invoked)
    """
    print("Testing: Resend HTTP API integration...")
    from services.email_service import email_service
    import httpx
    
    test_email = "test_resend@by8labs.com"
    test_subject = "Test Resend Email"
    test_body = "<p>Test body</p>"
    
    # 1. Success Path
    mock_response_success = httpx.Response(200, json={"id": "re_123456"})
    with patch("httpx.AsyncClient.post", return_value=mock_response_success) as mock_post, \
         patch.object(email_service, "resend_api_key", "re_test_key_abc123"):
        success, err = await email_service.send_email(test_email, test_subject, test_body)
        assert success, f"Expected send_email via Resend to succeed, got error: {err}"
        assert mock_post.called, "Expected httpx client POST to be called"
        
    # 2. Failure Path
    mock_response_fail = httpx.Response(400, text="Invalid API key or sender domain")
    with patch("httpx.AsyncClient.post", return_value=mock_response_fail) as mock_post, \
         patch.object(email_service, "resend_api_key", "re_test_key_abc123"), \
         patch.object(email_service, "smtp_user", ""), \
         patch.object(email_service, "smtp_password", ""):
        success, err = await email_service.send_email(test_email, test_subject, test_body)
        assert not success, "Expected send_email via Resend to fail with bad credentials and no SMTP fallback"
        assert "Resend API error 400" in err, f"Expected Resend API error message, got: {err}"
        
    # 3. Fallback Path
    # If Resend fails, and SMTP credentials exist, verify it falls back to SMTP
    with patch("httpx.AsyncClient.post", return_value=mock_response_fail) as mock_post, \
         patch("services.email_service.SMTP") as mock_smtp_class, \
         patch.object(email_service, "resend_api_key", "re_test_key_abc123"), \
         patch.object(email_service, "smtp_user", "smtp_user_val"), \
         patch.object(email_service, "smtp_password", "smtp_password_val"):
        
        mock_smtp = mock_smtp_class.return_value
        mock_smtp.connect = AsyncMock()
        mock_smtp.starttls = AsyncMock()
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()
        mock_smtp.quit = AsyncMock()
        
        success, err = await email_service.send_email(test_email, test_subject, test_body)
        assert success, f"Expected SMTP fallback to succeed, got error: {err}"
        assert mock_post.called, "Expected Resend HTTP post to be attempted first"
        assert mock_smtp.connect.called, "Expected SMTP fallback to be invoked on Resend failure"
        
    print("[OK] Resend HTTP API integration and SMTP fallback tested successfully.")


async def main():
    print("Starting OTP verification run...")
    await connect_db()
    db = get_database()

    test_email = "test_otp_verify@by8labs.com"
    test_name = "Test OTP User"

    # Setup test user
    await db.users.delete_one({"email": test_email})
    await db.users.insert_one({
        "email": test_email,
        "name": test_name,
        "role": "developer",
        "email_verified": False,
        "verification_token": None,
        "verification_token_expires": None,
        "verification_attempts": 0,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })

    try:
        await test_otp_generation()
        await test_email_sending(test_email, test_name)
        await test_otp_storage_security(db, test_email, test_name)
        await test_expired_otp_rejection(db, test_email, test_name)
        await test_verify_succeeds_only_with_latest(db, test_email, test_name)
        await test_invalid_otp_attempts_lockout(db, test_email, test_name)
        await test_rate_limiting(db, test_email, test_name)
        await test_rollback_on_email_failure(db, test_email, test_name)
        await test_mongodb_rate_limit_fallback(db)
        await test_first_request_always_allowed(db)
        await test_email_ip_isolation(db)
        await test_resend_integration(db)
        print("\n[SUCCESS] ALL 12 TESTS COMPLETED SUCCESSFULLY!")
    except AssertionError as e:
        print(f"\n[FAILURE] TEST FAILURE: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERROR] UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(2)
    finally:
        await db.users.delete_one({"email": test_email})
        await db.otp_requests.delete_many({"email": test_email})
        await db.rate_limits.delete_many({"_id": "ratelimit:/api/test-route:9.9.9.9"})
        await close_db()

if __name__ == "__main__":
    asyncio.run(main())
