"""AI Chat — conversational assistant with project context."""

from fastapi import APIRouter, Depends, HTTPException, Request
from datetime import datetime, timezone
from typing import Optional, List
from bson import ObjectId
import json

from models.ai_chat import ChatMessage, ChatConversation, MessageRole, ConversationStatus
from dependencies import get_current_user, get_current_active_user, validate_object_id
from database import get_database
from ai_client import ai_client

router = APIRouter(prefix="/ai/chat", tags=["ai-chat"])


def _sanitize_prompt(text: str) -> str:
    if not text:
        return ""
    return text.replace("\x00", "")[:4000]


def _get_user_id(user: dict) -> str:
    """Extract user ID from current_user dict (returned by get_current_user)."""
    # User dict from auth_service has 'id' field (string)
    return str(user["id"])


async def _build_project_context(project_id: Optional[str], user: dict) -> str:
    """Build system context from project data if project_id is set, else all user projects."""
    db = get_database()
    context_parts = []
    user_id = _get_user_id(user)
    
    if project_id:
        # Single project context
        proj = await db.projects.find_one({"_id": validate_object_id(project_id)})
        if not proj:
            return ""
        
        context_parts.append(f"Project: {proj.get('name', 'Unknown')}")
        context_parts.append(f"Description: {proj.get('description', 'No description')}")
        
        # Members
        member_ids = proj.get("team", [])
        if member_ids:
            member_object_ids = []
            for m in member_ids:
                try:
                    member_object_ids.append(validate_object_id(m))
                except Exception as e:
                    logger.warning(f"Skipping invalid member ID '{m}' in project {project_id}: {e}")
            
            if member_object_ids:
                members = await db.users.find({"_id": {"$in": member_object_ids}}).to_list(length=None)
                member_names = [m.get("name", "Unknown") for m in members]
            else:
                member_names = []
            context_parts.append(f"Team Members: {', '.join(member_names)}")

        
        # Tasks
        tasks = await db.tasks.find({"project_id": project_id}).to_list(100)
        if tasks:
            # Batch fetch assignee names
            assignee_ids = list(set(t.get("assignee_id") for t in tasks if t.get("assignee_id") and t["assignee_id"] != "unassigned"))
            assignee_name_map = {}
            if assignee_ids:
                valid_oids = []
                for aid in assignee_ids:
                    try:
                        valid_oids.append(ObjectId(aid))
                    except Exception:
                        pass
                if valid_oids:
                    async for u in db.users.find({"_id": {"$in": valid_oids}}, {"name": 1}):
                        assignee_name_map[str(u["_id"])] = u.get("name", "?")

            context_parts.append(f"\nTasks ({len(tasks)}):")
            for t in tasks[:30]:
                status = t.get("status", "?")
                priority = t.get("priority", "?")
                assignee = t.get("assignee_id", "unassigned")
                assignee_name = assignee_name_map.get(assignee, "?") if assignee != "unassigned" else "unassigned"
                title = t.get("title", "Untitled")
                if len(title) > 80:
                    title = title[:77] + "..."
                context_parts.append(f"  - [{status}] {title} (priority={priority}, assignee={assignee_name})")
        
        # Bugs
        bugs = await db.bugs.find({"project_id": project_id}).to_list(50)
        if bugs:
            context_parts.append(f"\nBugs ({len(bugs)}):")
            for b in bugs[:15]:
                status = b.get("status", "?")
                severity = b.get("severity", "?")
                title = b.get("title", "Untitled")
                if len(title) > 80:
                    title = title[:77] + "..."
                context_parts.append(f"  - [{severity}] {title} (status={status})")
    else:
        # Global context — all user projects summary
        # Find all projects user is member of
        projects = await db.projects.find({"team": user_id}).to_list(100)
        context_parts.append(f"You have access to {len(projects)} projects:")
        
        project_ids = [str(p["_id"]) for p in projects]
        task_counts = {}
        bug_counts = {}
        if project_ids:
            task_pipeline = [
                {"$match": {"project_id": {"$in": project_ids}}},
                {"$group": {"_id": "$project_id", "count": {"$sum": 1}}}
            ]
            async for res in db.tasks.aggregate(task_pipeline):
                task_counts[res["_id"]] = res["count"]
                
            bug_pipeline = [
                {"$match": {"project_id": {"$in": project_ids}}},
                {"$group": {"_id": "$project_id", "count": {"$sum": 1}}}
            ]
            async for res in db.bugs.aggregate(bug_pipeline):
                bug_counts[res["_id"]] = res["count"]

        for proj in projects:
            pname = proj.get("name", "Unknown")
            pid = str(proj["_id"])
            task_count = task_counts.get(pid, 0)
            bug_count = bug_counts.get(pid, 0)
            context_parts.append(f"  • {pname} — {task_count} tasks, {bug_count} bugs")
        
        context_parts.append("\nYou can ask about any of these projects, their tasks, bugs, or team members.")
    
    return "\n".join(context_parts)


def _generate_title(first_message: str) -> str:
    """Generate a short title from the first user message."""
    words = first_message.strip().split()
    if len(words) <= 5:
        return first_message[:60]
    return " ".join(words[:5]) + "..."


# ── SSE Streaming endpoint ──
@router.post("/stream")
async def stream_chat(
    request: Request,
    project_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    user: dict = Depends(get_current_active_user),
):
    """
    SSE streaming chat endpoint.
    Body: { "message": "user's message" }
    If conversation_id is provided, continues that conversation.
    If not, creates a new conversation.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    user_message = body.get("message", "").strip()
    if not user_message:
        raise HTTPException(400, "Message is required")
    
    # Reject image data — the AI model only supports text input
    if "data:image/" in user_message or user_message.startswith("iVBOR") or user_message.startswith("/9j/"):
        raise HTTPException(400, "Image input is not supported. Please send text only.")
    
    db = get_database()
    user_id = _get_user_id(user)
    
    # Load or create conversation
    conversation = None
    if conversation_id:
        conv_doc = await db.chat_conversations.find_one({"_id": validate_object_id(conversation_id), "user_id": user_id})
        if not conv_doc:
            raise HTTPException(404, "Conversation not found")
        conversation = ChatConversation(**conv_doc)
    else:
        # New conversation — create with first user message
        title = _generate_title(user_message)
        conversation = ChatConversation(
            user_id=user_id,
            title=title,
            project_id=project_id,
            messages=[],
        )
    
    # Append user message
    user_msg = ChatMessage(role=MessageRole.USER, content=_sanitize_prompt(user_message), project_id=project_id)
    conversation.messages.append(user_msg)
    
    # Build system prompt with project context
    system_context = await _build_project_context(project_id, user)
    system_prompt = (
        "You are an AI assistant for a project management platform. "
        "Answer questions about the user's projects, tasks, bugs, and team. "
        "Be concise and helpful. If you don't know, say so.\n\n"
        f"{system_context}"
    )
    
    # Build messages list for AI (convert ChatMessage → dict, exclude any stale system messages)
    ai_messages = [{"role": "system", "content": system_prompt}]
    for m in conversation.messages:
        if m.role.value != "system":  # skip any system messages stored in history
            ai_messages.append({"role": m.role.value, "content": m.content})
    
    # Use user's preferred model or fallback to system default
    from config import get_settings
    settings_obj = get_settings()
    user_settings = user.get("settings", {}) or {}
    ai_model = user_settings.get("ai_model") or settings_obj.openrouter_default_model or "poolside/laguna-m.1:free"

    # Stream response
    full_response = ""
    
    async def event_generator():
        nonlocal full_response
        try:
            async for token in ai_client.chat_completion_stream(
                system_prompt=system_prompt,
                messages=ai_messages[1:],  # exclude system (already included)
                temperature=0.3,
                model=ai_model,
            ):
                # Abort early if the client disconnected to free the upstream httpx connection
                if await request.is_disconnected():
                    return

                # Check for model error responses
                if token.startswith("[Error:") or token.startswith("ERROR:"):
                    yield f"event: error\ndata: {json.dumps({'error': token})}\n\n"
                    return
                full_response += token
                yield f"event: token\ndata: {json.dumps({'token': token})}\n\n"
            
            # Done — save assistant message to DB (only if we got a real response)
            if full_response.strip():
                assistant_msg = ChatMessage(role=MessageRole.ASSISTANT, content=full_response, project_id=project_id)
                conversation.messages.append(assistant_msg)
                conversation.updated_at = datetime.now(timezone.utc)
                
                # Save or create conversation
                if conversation.id:
                    await db.chat_conversations.update_one(
                        {"_id": validate_object_id(conversation.id)},
                        {"$set": {"messages": [m.model_dump(by_alias=True) for m in conversation.messages], "updated_at": conversation.updated_at}}
                    )
                else:
                    conversation.created_at = datetime.now(timezone.utc)
                    res = await db.chat_conversations.insert_one(conversation.model_dump(by_alias=True, exclude={"id"}))
                    conversation.id = str(res.inserted_id)
            
            yield f"event: done\ndata: {json.dumps({'conversation_id': conversation.id, 'title': conversation.title})}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
    
    from fastapi.responses import StreamingResponse
    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ── Create conversation ──
@router.post("/conversation")
async def create_conversation(
    title: str,
    project_id: Optional[str] = None,
    user: dict = Depends(get_current_active_user),
):
    """Create a new empty conversation."""
    db = get_database()
    user_id = _get_user_id(user)
    conv = ChatConversation(
        user_id=user_id,
        title=title,
        project_id=project_id,
        messages=[],
    )
    res = await db.chat_conversations.insert_one(conv.model_dump(by_alias=True, exclude={"id"}))
    conv.id = str(res.inserted_id)
    return conv


# ── Get history ──
@router.get("/history")
async def get_history(
    include_archived: bool = False,
    user: dict = Depends(get_current_active_user),
) -> List[dict]:
    """Get user's chat conversations, newest first."""
    db = get_database()
    user_id = _get_user_id(user)
    query = {"user_id": user_id}
    if not include_archived:
        query["status"] = ConversationStatus.ACTIVE
    
    cursor = db.chat_conversations.find(query).sort("updated_at", -1)
    convos = await cursor.to_list(100)
    
    # Return lightweight summaries
    result = []
    for c in convos:
        result.append({
            "id": str(c["_id"]),
            "title": c["title"],
            "project_id": c.get("project_id"),
            "status": c.get("status", "active"),
            "message_count": len(c.get("messages", [])),
            "updated_at": c["updated_at"],
        })
    return result


# ── Get single conversation ──
@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    user: dict = Depends(get_current_active_user),
) -> ChatConversation:
    """Fetch full conversation with messages."""
    db = get_database()
    user_id = _get_user_id(user)
    doc = await db.chat_conversations.find_one({"_id": validate_object_id(conversation_id), "user_id": user_id})
    if not doc:
        raise HTTPException(404, "Conversation not found")
    doc["_id"] = str(doc["_id"])  # Convert ObjectId to string for Pydantic
    return ChatConversation(**doc)


# ── Delete conversation ──
@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    user: dict = Depends(get_current_active_user),
):
    """Delete a conversation permanently."""
    db = get_database()
    user_id = _get_user_id(user)
    res = await db.chat_conversations.delete_one({"_id": validate_object_id(conversation_id), "user_id": user_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "Conversation not found")
    return {"ok": True}


# ── Archive conversation ──
@router.patch("/{conversation_id}")
async def archive_conversation(
    conversation_id: str,
    user: dict = Depends(get_current_active_user),
):
    """Archive a conversation (hide from history)."""
    db = get_database()
    user_id = _get_user_id(user)
    res = await db.chat_conversations.update_one(
        {"_id": validate_object_id(conversation_id), "user_id": user_id},
        {"$set": {"status": ConversationStatus.ARCHIVED, "updated_at": datetime.now(timezone.utc)}}
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Conversation not found")
    return {"ok": True}


