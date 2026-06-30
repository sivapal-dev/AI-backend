from fastapi import APIRouter, Depends, HTTPException
from database import get_database
from bson import ObjectId
from dependencies import get_current_active_user

router = APIRouter(prefix="/users", tags=["Users"])

@router.get("")
async def list_users(current_user: dict = Depends(get_current_active_user)):
    db = get_database()
    is_admin = current_user.get("role", "").lower() == "admin"
    users = []
    async for u in db.users.find({}):
        u["_id"] = str(u["_id"])
        u.pop("password", None)
        u.pop("verification_token", None)
        u.pop("verification_token_expires", None)
        u.pop("email_credentials", None)
        u.pop("github_access_token", None)
        if not is_admin:
            # Return minimal profiles for collaboration features
            users.append({
                "_id": u["_id"],
                "name": u.get("name"),
                "email": u.get("email"),
                "role": u.get("role", "developer"),
                "position": u.get("position"),
                "avatar": u.get("avatar"),
            })
        else:
            users.append(u)
    return users

