from fastapi import APIRouter, HTTPException, Depends, Request, Query, Cookie
from fastapi.responses import StreamingResponse
from typing import List, AsyncGenerator, Optional
from bson import ObjectId
from datetime import datetime, timezone
from database import get_database
from dependencies import get_current_active_user
from helpers.notification_sender import send_notification, add_connection, remove_connection
from utils.security import decode_token
import asyncio
import json

router = APIRouter(prefix="/notifications", tags=["Notifications"])


def _get_db():
    return get_database()


async def event_stream(user_id: str) -> AsyncGenerator[str, None]:
    """
    SSE event stream for a specific user.
    Yields formatted SSE messages when notifications are pushed.
    """
    queue: asyncio.Queue = asyncio.Queue()
    add_connection(user_id, queue)

    try:
        # Send initial connection confirmation
        yield f"event: connected\ndata: {{\"status\": \"connected\"}}\n\n"

        while True:
            try:
                # Wait for a notification (with timeout to keep connection alive)
                data = await asyncio.wait_for(queue.get(), timeout=30.0)
                # Format as SSE
                data_str = await asyncio.to_thread(json.dumps, data)
                yield f"event: notification\ndata: {data_str}\n\n"
            except asyncio.TimeoutError:
                # Send heartbeat to keep connection alive
                yield f"event: heartbeat\ndata: {{\"ts\": {datetime.now(timezone.utc).timestamp()}}}\n\n"
    except GeneratorExit:
        remove_connection(user_id, queue)
    except Exception as e:
        remove_connection(user_id, queue)
        raise e


@router.get("/stream")
async def stream_notifications(
    request: Request,
    access_token: Optional[str] = Cookie(None),
):
    """
    SSE endpoint for real-time notifications.
    Connects client to persistent stream; pushes new notifications instantly.
    Authentication via access_token HttpOnly cookie.
    """
    token = access_token
    if not token:
        raise HTTPException(status_code=401, detail="No access token cookie provided")
    from jose import jwt
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token: no user identified")

    async def generate():
        try:
            async for event in event_stream(user_id):
                if await request.is_disconnected():
                    break
                yield event
        except Exception:
            pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable buffering for Nginx
        },
    )


@router.get("")
async def list_notifications(
    unread_only: bool = False,
    type: Optional[str] = None,
    current_user: dict = Depends(get_current_active_user),
    limit: int = Query(50, ge=1, le=100, description="Maximum number of notifications to return"),
    skip: int = Query(0, ge=0, description="Number of notifications to skip"),
):
    db = _get_db()
    query = {"user_id": current_user["id"]}

    if unread_only:
        query["read"] = False
    if type:
        query["type"] = type

    cursor = db.notifications.find(query).sort("created_at", -1).skip(skip).limit(limit)
    notifications = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        notifications.append(doc)
    return notifications


@router.put("/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    try:
        oid = ObjectId(notification_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid notification ID format")

    res = await db.notifications.update_one(
        {"_id": oid, "user_id": current_user["id"]},
        {"$set": {"read": True}}
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")

    return {"message": "Notification marked as read"}


@router.put("/read-all")
async def mark_all_notifications_read(
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    await db.notifications.update_many(
        {"user_id": current_user["id"], "read": False},
        {"$set": {"read": True}},
    )
    return {"message": "All notifications marked as read"}


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    try:
        oid = ObjectId(notification_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid notification ID format")

    res = await db.notifications.delete_one({"_id": oid, "user_id": current_user["id"]})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"message": "Notification deleted"}


@router.get("/unread-count")
async def get_unread_count(
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    count = await db.notifications.count_documents(
        {"user_id": current_user["id"], "read": False}
    )
    return {"unread_count": count}