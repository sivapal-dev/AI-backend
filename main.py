import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import get_settings
from database import close_db, connect_db
from redis_client import close_redis, connect_redis
from routers import (
    activity,
    admin,
    ai,
    ai_chat,
    ai_daily_checkin,
    ai_task_monitor,
    ams,
    auth,
    bugs,
    comments,
    custom_fields,
    employee_holidays,
    epics,
    github,
    holidays,
    issue_links,
    leave_settings,
    leaves,
    meetings,
    notifications,
    off_project_tasks,
    projects,
    sprints,
    tasks,
    upload,
    users,
    workflows,
)
from routers import bugfix
from routers.documents import router as documents_router
from routers.google_calendar import router as google_calendar
from routers.inbox import router as inbox
from routers.whiteboards import router as whiteboards_router
from routers.drive_proxy import router as drive_proxy_router
from routers.teams import router as teams_router
from scheduler import create_scheduler, register_checkin_jobs

settings = get_settings()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

_scheduler = None
_lifespan_executed = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler, _lifespan_executed
    if _lifespan_executed:
        logger.warning("Lifespan already executed. Skipping to prevent recursive/multiple executions.")
        yield
        return
    _lifespan_executed = True

    # 1. Verify required environment variables for Render deployment readiness
    logger.info("Verifying required environment variables...")
    from services.email_service import log_email_config_summary
    log_email_config_summary()
    
    # Critical variables check and graceful fallbacks/warnings
    if not settings.jwt_secret:
        import secrets
        settings.jwt_secret = secrets.token_hex(32)
        logger.error(
            "CRITICAL: JWT_SECRET environment variable is missing or empty! "
            "A temporary secure fallback secret has been generated. "
            "Sessions will invalidate when the server restarts. Please set JWT_SECRET on Render."
        )
        
    if not settings.mongodb_uri:
        logger.error("CRITICAL: MONGODB_URI environment variable is missing or empty!")

    # Check REDIS variables
    redis_vars = ["redis_host", "redis_port", "redis_username"]
    for v in redis_vars:
        val = getattr(settings, v, None)
        if val is None or val == "":
            logger.warning(f"Environment warning: REDIS_{v.replace('redis_', '').upper()} is not set.")

    # Check SMTP variables
    smtp_vars = ["smtp_host", "smtp_port", "smtp_user", "smtp_password", "smtp_from_name", "smtp_from_email"]
    for v in smtp_vars:
        val = getattr(settings, v, None)
        if val is None or val == "":
            logger.warning(f"Environment warning: {v.upper()} is not set.")

    # Check OpenRouter
    if not settings.openrouter_api_key:
         logger.warning("Environment warning: OPENROUTER_API_KEY is not set. AI tasks will be disabled.")

    # Check GOOGLE variables
    google_vars = [
        "google_client_id", "google_client_secret", "google_calendar_id", "google_redirect_uri", 
        "google_drive_client_id", "google_drive_client_secret", "google_drive_redirect_uri", "google_drive_refresh_token"
    ]
    for v in google_vars:
        val = getattr(settings, v, None)
        if val is None or val == "":
            logger.warning(f"Environment warning: {v.upper()} is not set.")

    # Check GITHUB variables
    github_vars = ["github_client_id", "github_client_secret"]
    for v in github_vars:
        val = getattr(settings, v, None)
        if val is None or val == "":
            logger.warning(f"Environment warning: {v.upper()} is not set.")

    # Check MSAL variables
    msal_vars = ["msal_client_id", "msal_tenant_id", "msal_client_secret"]
    for v in msal_vars:
        val = getattr(settings, v, None)
        if val is None or val == "":
            logger.warning(f"Environment warning: {v.upper()} is not set.")

    # 2. Database & Redis connection
    try:
        await connect_db()
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB during lifespan startup: {e}")
        
    try:
        await connect_redis()
    except Exception as e:
        logger.error(f"Failed to connect to Redis during lifespan startup: {e}")
    
    # 3. Scheduler startup
    try:
        _scheduler = create_scheduler()
        register_checkin_jobs(_scheduler)
        _scheduler.start()
        logger.info("Scheduler started")
    except Exception as e:
        logger.error(f"Failed to start scheduler during lifespan startup: {e}")
        
    yield
    
    # Teardown
    try:
        if _scheduler:
            _scheduler.shutdown()
            logger.info("Scheduler shut down")
    except Exception as e:
        logger.error(f"Failed to stop scheduler during lifespan shutdown: {e}")
        
    try:
        await close_redis()
    except Exception as e:
        logger.error(f"Failed to close Redis during lifespan shutdown: {e}")
        
    try:
        await close_db()
    except Exception as e:
        logger.error(f"Failed to close database during lifespan shutdown: {e}")


app = FastAPI(
    title="By8flow API",
    description="Issue and Bug Tracking System API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.frontend_url,
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://by8flow-frontend.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi import HTTPException

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    response = JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )
    origin = request.headers.get("origin")
    if origin:
        allowed_origins = [
            settings.frontend_url,
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "https://by8flow-frontend.vercel.app",
        ]
        if origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, Accept, Origin, Cookie"
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    response = JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(exc.errors())},
    )
    origin = request.headers.get("origin")
    if origin:
        allowed_origins = [
            settings.frontend_url,
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "https://by8flow-frontend.vercel.app",
        ]
        if origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, Accept, Origin, Cookie"
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception")
    response = JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
    origin = request.headers.get("origin")
    if origin:
        allowed_origins = [
            settings.frontend_url,
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "https://by8flow-frontend.vercel.app",
        ]
        if origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With, Accept, Origin, Cookie"
    return response


# Health check
@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "by8flow-api"}


# Include routers
app.include_router(auth, prefix="/api")
app.include_router(projects, prefix="/api")
app.include_router(tasks, prefix="/api")
app.include_router(bugs, prefix="/api")
app.include_router(users, prefix="/api")
app.include_router(activity, prefix="/api")
app.include_router(ai, prefix="/api")
app.include_router(admin, prefix="/api")
app.include_router(meetings, prefix="/api")
app.include_router(comments, prefix="/api")
app.include_router(upload, prefix="/api")
app.include_router(notifications, prefix="/api")
app.include_router(sprints, prefix="/api")
app.include_router(issue_links, prefix="/api")
app.include_router(epics, prefix="/api")
app.include_router(workflows, prefix="/api")
app.include_router(ams, prefix="/api")
app.include_router(custom_fields, prefix="/api")
app.include_router(off_project_tasks, prefix="/api")
app.include_router(leaves, prefix="/api")
app.include_router(holidays, prefix="/api")
app.include_router(leave_settings, prefix="/api")
app.include_router(employee_holidays, prefix="/api")
app.include_router(whiteboards_router, prefix="/api")
app.include_router(google_calendar, prefix="/api")
app.include_router(inbox, prefix="/api")
app.include_router(github, prefix="/api")
app.include_router(ai_task_monitor, prefix="/api")
app.include_router(ai_daily_checkin, prefix="/api")
app.include_router(ai_chat, prefix="/api")
app.include_router(bugfix)
app.include_router(documents_router, prefix="/api")
app.include_router(drive_proxy_router, prefix="/api")
app.include_router(teams_router, prefix="/api")

from fastapi.responses import RedirectResponse
from typing import Optional

@app.get("/auth/github/callback")
async def auth_github_callback(
    state: Optional[str] = None,
    code: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    error_uri: Optional[str] = None,
):
    """Redirect alternative /auth/github/callback calls to the actual /api/github/callback handler"""
    import urllib.parse
    params = {}
    if state:
        params["state"] = state
    if code:
        params["code"] = code
    if error:
        params["error"] = error
    if error_description:
        params["error_description"] = error_description
    if error_uri:
        params["error_uri"] = error_uri
    query = urllib.parse.urlencode(params)
    return RedirectResponse(url=f"/api/github/callback?{query}")


if __name__ == "__main__":
    import uvicorn
    from config import get_settings
    from pathlib import Path

    settings = get_settings()
    ssl_key = settings.ssl_keyfile
    ssl_cert = settings.ssl_certfile
    port = int(os.environ.get("PORT", 8000))

    uvicorn_kwargs = {
        "app": "main:app",
        "host": "0.0.0.0",
        "port": port,
    }

    if ssl_key and ssl_cert and Path(ssl_key).exists() and Path(ssl_cert).exists():
        uvicorn_kwargs["ssl_keyfile"] = ssl_key
        uvicorn_kwargs["ssl_certfile"] = ssl_cert
        logger.info("Starting server with HTTPS/SSL enabled")
    else:
        logger.warning(
            f"Starting server on HTTP (unsecure) at 0.0.0.0:{port}. "
            "For production, configure SSL certs or run behind an SSL-terminating reverse proxy."
        )

    uvicorn.run(**uvicorn_kwargs)
