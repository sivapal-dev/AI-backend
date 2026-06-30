import io
import logging
from fastapi import APIRouter, HTTPException, Request, Response, Depends
from fastapi.responses import StreamingResponse
from helpers.backblaze import get_file_content
from dependencies import get_current_active_user
from config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/drive", tags=["Drive Proxy"])


@router.get("/image/{file_id}")
async def proxy_b2_image(
    file_id: str,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Proxy Backblaze B2 images through our backend to avoid sharing private B2 credentials.
    Requires authentication. Returns the raw image binary with proper CORS headers.
    """
    import re
    if not file_id or len(file_id) < 10 or len(file_id) > 100:
        raise HTTPException(status_code=400, detail="Invalid file_id format")

    if not re.match(r'^[a-zA-Z0-9_-]+$', file_id):
        raise HTTPException(status_code=400, detail="Invalid file_id characters")

    try:
        content, mime_type = get_file_content(file_id)

        if not mime_type.startswith("image/"):
            raise HTTPException(
                status_code=400,
                detail=f"File is not an image (MIME: {mime_type})"
            )

        return StreamingResponse(
            io.BytesIO(content),
            media_type=mime_type,
            headers={
                "Cache-Control": "public, max-age=86400",
                "Access-Control-Allow-Origin": get_settings().frontend_url,
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Content-Disposition": "inline",
                "X-Content-Type-Options": "nosniff",
            }
        )

    except Exception as e:
        logger.error(f"Failed to fetch B2 file {file_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch image: {str(e)}")


@router.get("/file/{file_id}")
async def proxy_b2_file(
    file_id: str,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    """
    Proxy any Backblaze B2 file through our backend.
    Requires authentication. Returns the raw file binary with proper CORS headers.
    """
    import re
    if not file_id or len(file_id) < 10 or len(file_id) > 100:
        raise HTTPException(status_code=400, detail="Invalid file_id format")

    if not re.match(r'^[a-zA-Z0-9_-]+$', file_id):
        raise HTTPException(status_code=400, detail="Invalid file_id characters")

    try:
        content, mime_type = get_file_content(file_id)

        return StreamingResponse(
            io.BytesIO(content),
            media_type=mime_type,
            headers={
                "Cache-Control": "public, max-age=86400",
                "Access-Control-Allow-Origin": get_settings().frontend_url,
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Content-Disposition": "inline",
                "X-Content-Type-Options": "nosniff",
            }
        )

    except Exception as e:
        logger.error(f"Failed to fetch B2 file {file_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch file: {str(e)}")


@router.options("/image/{file_id}")
@router.options("/file/{file_id}")
async def options_b2_proxy(file_id: str):
    """Handle CORS preflight requests."""
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Access-Control-Max-Age": "86400",
        }
    )