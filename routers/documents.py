from fastapi import APIRouter, HTTPException, status, Depends
from fastapi.responses import FileResponse
from typing import Optional
from bson import ObjectId
from datetime import datetime, timezone
from database import get_database
from dependencies import get_current_active_user, validate_object_id
from models.document import DocumentCreate, DocumentFormat, DocumentStatus, DocumentResponse
from services.document_generator import generate_document
from services.file_storage import read_file, delete_file
import logging
import os

logger = logging.getLogger(__name__)


def _resolve_file_path(file_path: str) -> str:
    if not file_path:
        return ""
    if os.path.exists(file_path):
        return file_path
    
    # Fallback to local workspace relative uploads directory
    if "uploads" in file_path:
        relative_part = file_path.split("uploads", 1)[1].lstrip("\\/")
        current_uploads_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "uploads"))
        fallback_path = os.path.join(current_uploads_dir, relative_part)
        if os.path.exists(fallback_path):
            return fallback_path
            
    return file_path


router = APIRouter(prefix="/documents", tags=["Documents"])


@router.post("/generate")
async def create_document(
    doc: DocumentCreate,
    current_user: dict = Depends(get_current_active_user),
):
    """Generate a new document from a prompt."""
    db = get_database()

    # Validate project if specified
    project_name = None
    project_context = ""
    if doc.project_id:
        project = await db.projects.find_one({"_id": ObjectId(doc.project_id)})
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        project_name = project.get("name", "")
        project_context = project.get("markdown_content", "") or project.get("description", "")

    # Create pending doc record
    doc_record = {
        "user_id": current_user["id"],
        "generated_by_name": current_user.get("name", current_user.get("email", "")),
        "project_id": doc.project_id,
        "project_name": project_name,
        "format": doc.format.value,
        "prompt": doc.prompt,
        "file_name": "",
        "file_path": "",
        "pdf_path": None,
        "size_bytes": 0,
        "status": "pending",
        "error_message": None,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.documents.insert_one(doc_record)
    doc_id = str(result.inserted_id)

    # Update status to generating
    await db.documents.update_one(
        {"_id": ObjectId(doc_id)},
        {"$set": {"status": "generating"}},
    )

    try:
        # Generate document
        gen_result = await generate_document(
            prompt=doc.prompt,
            format_type=doc.format.value,
            project_id=doc.project_id,
            project_context=project_context,
        )

        # Update record with results
        await db.documents.update_one(
            {"_id": validate_object_id(doc_id)},
            {
                "$set": {
                    "status": "completed",
                    "file_name": gen_result["file_name"],
                    "file_path": gen_result["file_path"],
                    "pdf_path": gen_result.get("pdf_path"),
                    "size_bytes": gen_result["size_bytes"],
                }
            },
        )

        return {
            "success": True,
            "document_id": doc_id,
            "file_name": gen_result["file_name"],
            "download_url": f"/api/documents/{doc_id}/download",
            "preview_url": f"/api/documents/{doc_id}/preview",
        }

    except Exception as e:
        logger.error(f"Document generation failed: {e}", exc_info=True)
        await db.documents.update_one(
            {"_id": ObjectId(doc_id)},
            {
                "$set": {
                    "status": "failed",
                    "error_message": str(e)[:500],
                }
            },
        )
        raise HTTPException(
            status_code=500,
            detail=f"Document generation failed: {str(e)[:200]}",
        )


@router.get("")
async def list_documents(
    project_id: Optional[str] = None,
    current_user: dict = Depends(get_current_active_user),
):
    """List documents. Optional filter by project_id."""
    db = get_database()
    query = {}

    if project_id:
        query["project_id"] = project_id
    else:
        # Global docs (no project) or all docs user has access to
        if current_user.get("role", "").lower() != "admin":
            # Non-admin: only see own docs
            query["user_id"] = current_user["id"]

    cursor = db.documents.find(query).sort("created_at", -1)
    docs = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        docs.append(doc)
    return docs


@router.get("/{doc_id}")
async def get_document(
    doc_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Get document details."""
    db = get_database()
    doc = await db.documents.find_one({"_id": ObjectId(doc_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.get("user_id") != current_user["id"] and current_user.get("role", "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")
    doc["_id"] = str(doc["_id"])
    return doc


@router.get("/{doc_id}/download")
async def download_document(
    doc_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Download the original document file."""
    db = get_database()
    doc = await db.documents.find_one({"_id": validate_object_id(doc_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.get("user_id") != current_user["id"] and current_user.get("role", "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")

    file_path = _resolve_file_path(doc.get("file_path", ""))
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found on disk")

    file_name = doc.get("file_name", os.path.basename(file_path))
    media_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if doc.get("format") == "doc"
        else "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    return FileResponse(path=file_path, filename=file_name, media_type=media_type)


@router.get("/{doc_id}/preview")
async def preview_document(
    doc_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Preview document. Returns PDF for PPT, original DOCX for DOC."""
    db = get_database()
    doc = await db.documents.find_one({"_id": validate_object_id(doc_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.get("user_id") != current_user["id"] and current_user.get("role", "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")

    # For PPT: return PDF preview if available
    if doc.get("format") == "ppt" and doc.get("pdf_path"):
        pdf_path = _resolve_file_path(doc["pdf_path"])
        if os.path.exists(pdf_path):
            return FileResponse(
                path=pdf_path,
                filename=os.path.basename(pdf_path),
                media_type="application/pdf",
            )

    # Fallback: return original file
    file_path = _resolve_file_path(doc.get("file_path", ""))
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Preview not available")

    if doc.get("format") == "doc":
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        media_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

    return FileResponse(
        path=file_path,
        filename=os.path.basename(file_path),
        media_type=media_type,
    )


@router.delete("/{doc_id}")
async def delete_document(
    doc_id: str,
    current_user: dict = Depends(get_current_active_user),
):
    """Delete a document and its files."""
    db = get_database()
    doc = await db.documents.find_one({"_id": ObjectId(doc_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Only owner or admin can delete
    if doc.get("user_id") != current_user["id"] and current_user.get("role", "").lower() != "admin":
        raise HTTPException(status_code=403, detail="Not authorized")

    # Delete files
    delete_file(_resolve_file_path(doc.get("file_path", "")))
    if doc.get("pdf_path"):
        delete_file(_resolve_file_path(doc["pdf_path"]))

    # Delete record
    await db.documents.delete_one({"_id": validate_object_id(doc_id)})
    return {"message": "Document deleted"}
