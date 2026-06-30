from fastapi import APIRouter, HTTPException, Depends
from typing import List, Optional
from bson import ObjectId
from datetime import datetime, timezone
from database import get_database
from models.comment import CommentCreate, CommentUpdate
from dependencies import get_current_active_user
from helpers.notification_sender import send_notification, notify_role_watchers

router = APIRouter(prefix="/comments", tags=["Comments"])


def _get_db():
    return get_database()


async def _can_moderate_comment(existing_comment: dict, current_user: dict, db) -> bool:
    if existing_comment["author_id"] == current_user["id"]:
        return True
    
    role = current_user.get("role", "").lower()
    if role in ["admin", "hr"]:
        return True
        
    if role == "team_lead":
        entity_type = existing_comment.get("entity_type")
        entity_id = existing_comment.get("entity_id")
        if not entity_type or not entity_id:
            return False
            
        entity = None
        try:
            if entity_type == "task":
                entity = await db.tasks.find_one({"_id": ObjectId(entity_id)})
            elif entity_type == "bug":
                entity = await db.bugs.find_one({"_id": ObjectId(entity_id)})
            elif entity_type == "sprint":
                entity = await db.sprints.find_one({"_id": ObjectId(entity_id)})
        except Exception:
            return False
            
        if entity and entity.get("project_id"):
            try:
                project = await db.projects.find_one({"_id": ObjectId(entity["project_id"])})
                if project and current_user["id"] in project.get("team", []):
                    return True
            except Exception:
                return False
    return False



@router.post("", response_model=dict)
async def create_comment(
    comment: CommentCreate,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()

    # Verify entity exists
    entity = None
    if comment.entity_type == "task":
        entity = await db.tasks.find_one({"_id": ObjectId(comment.entity_id)})
    elif comment.entity_type == "bug":
        entity = await db.bugs.find_one({"_id": ObjectId(comment.entity_id)})
    elif comment.entity_type == "sprint":
        entity = await db.sprints.find_one({"_id": ObjectId(comment.entity_id)})

    if not entity:
        raise HTTPException(
            status_code=404, detail=f"{comment.entity_type.capitalize()} not found"
        )

    comment_doc = {
        "content": comment.content,
        "entity_type": comment.entity_type,
        "entity_id": comment.entity_id,
        "parent_id": comment.parent_id,
        "mentions": comment.mentions,
        "author_id": current_user["id"],
        "author_name": current_user.get("name", "Unknown"),
        "author_email": current_user.get("email", ""),
        "author_role": current_user.get("role", ""),
        "edited": False,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    result = await db.comments.insert_one(comment_doc)

    # Create notifications for mentions
    for mentioned_user_id in comment.mentions:
        if mentioned_user_id != current_user["id"]:
            await send_notification(
                user_id=mentioned_user_id,
                type_=f"{comment.entity_type}_commented",
                title=f"New mention in {comment.entity_type}",
                message=f"{current_user.get('name', 'Someone')} mentioned you in a comment",
                entity_type=comment.entity_type,
                entity_id=comment.entity_id,
                link=f"/dashboard/{'tasks' if comment.entity_type == 'task' else ('bugs' if comment.entity_type == 'bug' else 'sprints')}/{comment.entity_id}",
            )

    # Notify entity owner/assignee (if not the commenter)
    if comment.entity_type == "task" and entity.get("assigned_to"):
        assignee_user = await db.users.find_one({"name": entity["assigned_to"]}, {"_id": 1})
        if assignee_user and str(assignee_user["_id"]) != current_user["id"]:
            await send_notification(
                user_id=str(assignee_user["_id"]),
                type_="task_commented",
                title=f"New comment on task: {entity.get('title', 'Untitled')}",
                message=f"{current_user.get('name', 'Someone')} commented on your task",
                entity_type="task",
                entity_id=comment.entity_id,
                link=f"/dashboard/tasks/{comment.entity_id}",
            )
    elif comment.entity_type == "bug":
        notified_bug_owners = set()
        for owner_field in ["reporter", "assignee"]:
            owner_name = entity.get(owner_field)
            if owner_name:
                owner_user = await db.users.find_one({"name": owner_name}, {"_id": 1})
                if owner_user and str(owner_user["_id"]) != current_user["id"] and str(owner_user["_id"]) not in notified_bug_owners:
                    notified_bug_owners.add(str(owner_user["_id"]))
                    await send_notification(
                        user_id=str(owner_user["_id"]),
                        type_="bug_commented",
                        title=f"New comment on bug: {entity.get('title', 'Untitled')}",
                        message=f"{current_user.get('name', 'Someone')} commented on your bug",
                        entity_type="bug",
                        entity_id=comment.entity_id,
                        link=f"/dashboard/bugs/{comment.entity_id}",
                    )

    # Notify admin/team_lead/hr (in-app only)
    entity_title = entity.get('title') or entity.get('name', 'Untitled')
    await notify_role_watchers(
        notify_roles=["admin", "team_lead", "hr"],
        type_=f"{comment.entity_type}_commented",
        title=f"New comment on {comment.entity_type}: {entity_title}",
        message=f"{current_user.get('name', 'Someone')} commented on {comment.entity_type} '{entity_title}'",
        entity_type=comment.entity_type,
        entity_id=comment.entity_id,
        link=f"/dashboard/{'tasks' if comment.entity_type == 'task' else ('bugs' if comment.entity_type == 'bug' else 'sprints')}/{comment.entity_id}",
        exclude_user_id=current_user["id"],
    )

    # Log activity
    await db.activity_logs.insert_one(
        {
            "user_id": current_user["id"],
            "action": f"{comment.entity_type}_commented",
            "entity_type": comment.entity_type,
            "entity_id": comment.entity_id,
            "metadata": {"comment_preview": comment.content[:100]},
            "created_at": datetime.now(timezone.utc),
        }
    )

    return {"id": str(result.inserted_id), "message": "Comment added successfully"}


@router.get("")
async def list_comments(
    entity_type: str,
    entity_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()

    # Verify entity exists
    entity = None
    if entity_type == "task":
        entity = await db.tasks.find_one({"_id": ObjectId(entity_id)})
    elif entity_type == "bug":
        entity = await db.bugs.find_one({"_id": ObjectId(entity_id)})
    elif entity_type == "sprint":
        entity = await db.sprints.find_one({"_id": ObjectId(entity_id)})

    if not entity:
        raise HTTPException(
            status_code=404, detail=f"{entity_type.capitalize()} not found"
        )

    # Get all top-level comments
    cursor = db.comments.find(
        {"entity_type": entity_type, "entity_id": entity_id, "parent_id": None}
    ).sort("created_at", -1)

    top_level = []
    top_level_ids = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        top_level_ids.append(doc["_id"])
        top_level.append(doc)

    # Batch fetch all replies in one query
    replies_by_parent: dict[str, list] = {}
    if top_level_ids:
        reply_cursor = db.comments.find({"parent_id": {"$in": top_level_ids}}).sort("created_at", 1)
        async for reply_doc in reply_cursor:
            reply_doc["_id"] = str(reply_doc["_id"])
            parent_id = reply_doc.get("parent_id", "")
            if parent_id not in replies_by_parent:
                replies_by_parent[parent_id] = []
            replies_by_parent[parent_id].append(reply_doc)

    for doc in top_level:
        doc["replies"] = replies_by_parent.get(doc["_id"], [])

    return top_level


@router.put("/{comment_id}")
async def update_comment(
    comment_id: str,
    comment_update: CommentUpdate,
    current_user: dict = Depends(get_current_active_user),
):
    db = _get_db()
    existing = await db.comments.find_one({"_id": ObjectId(comment_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Comment not found")

    if not await _can_moderate_comment(existing, current_user, db):
        raise HTTPException(
            status_code=403, detail="Only comment author, admin, HR, or project team leads can update"
        )


    update_data = {
        "content": comment_update.content,
        "edited": True,
        "updated_at": datetime.now(timezone.utc),
    }

    await db.comments.update_one({"_id": ObjectId(comment_id)}, {"$set": update_data})

    return {"message": "Comment updated successfully"}


@router.delete("/{comment_id}")
async def delete_comment(
    comment_id: str, current_user: dict = Depends(get_current_active_user)
):
    db = _get_db()
    existing = await db.comments.find_one({"_id": ObjectId(comment_id)})
    if not existing:
        raise HTTPException(status_code=404, detail="Comment not found")

    if not await _can_moderate_comment(existing, current_user, db):
        raise HTTPException(
            status_code=403, detail="Only comment author, admin, HR, or project team leads can delete"
        )


    # Delete all replies as well
    await db.comments.delete_many({"parent_id": comment_id})
    await db.comments.delete_one({"_id": ObjectId(comment_id)})

    return {"message": "Comment deleted successfully"}
