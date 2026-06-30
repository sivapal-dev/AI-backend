import os
import io
import logging
from typing import Optional
from b2sdk.v2 import B2Api, Bucket, FileVersion
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BACKBLAZE_KEY_ID = os.getenv("BACKBLAZE_KEY_ID", "")
BACKBLAZE_KEY_SECRET = os.getenv("BACKBLAZE_KEY_SECRET", "")
BACKBLAZE_BUCKET_NAME = os.getenv("BACKBLAZE_BUCKET_NAME", "by8flow")
B2_API: Optional[B2Api] = None
BUCKET: Optional[Bucket] = None


def _get_b2_api() -> B2Api:
    global B2_API
    if B2_API is None:
        B2_API = B2Api()
        B2_API.authorize_account("production", BACKBLAZE_KEY_ID, BACKBLAZE_KEY_SECRET)
    return B2_API


def _get_bucket() -> Bucket:
    global BUCKET
    if BUCKET is None:
        api = _get_b2_api()
        BUCKET = api.get_bucket_by_name(BACKBLAZE_BUCKET_NAME)
    return BUCKET


def build_direct_image_url(file_id: str) -> str:
    return f"/api/drive/image/{file_id}"


def ensure_public_read(file_id: str) -> None:
    pass


def upload_to_drive(file_content: bytes, filename: str, mime_type: str) -> dict:
    bucket = _get_bucket()
    file_id = None
    web_view_link = None
    
    try:
        file_version: FileVersion = bucket.upload_bytes(
            file_content,
            filename,
            content_type=mime_type,
        )
        file_id = file_version.id_
        web_view_link = f"/api/drive/file/{file_id}"
        
        result = {
            "file_id": file_id,
            "name": filename,
            "webViewLink": web_view_link,
            "directUrl": build_direct_image_url(file_id),
        }
        logger.debug(f"Backblaze upload result: {result}")
        return result
    except Exception as e:
        logger.error(f"Backblaze upload failed: {e}")
        raise


def delete_from_drive(file_id: str, file_name: str = "") -> bool:
    try:
        api = _get_b2_api()
        bucket = _get_bucket()
        if not file_name:
            file_version = bucket.get_file_info_by_id(file_id)
            file_name = file_version.file_name
        bucket.delete_file_version(file_id, file_name)
        return True
    except Exception as e:
        logger.error(f"Failed to delete file {file_id} from Backblaze: {e}")
        return False


def get_file_content(file_id: str) -> tuple[bytes, str]:
    api = _get_b2_api()
    bucket = _get_bucket()
    downloaded = bucket.download_file_by_id(file_id)
    content = downloaded.response.content
    mime_type = downloaded.download_version.content_type or "application/octet-stream"
    return content, mime_type