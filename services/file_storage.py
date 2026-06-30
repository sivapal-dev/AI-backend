import os
import shutil
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone
import logging
import re

logger = logging.getLogger(__name__)

# Base directory for all generated documents
UPLOADS_ROOT = Path(__file__).parent.parent / "uploads" / "documents"
# Quarantine directory for soft-deleted files (W198)
TRASH_ROOT = UPLOADS_ROOT / ".trash"


# =====================================================================
# B2 STORAGE INTEGRATION HOOKS (W195)
# To migrate to Backblaze B2 or Google Drive cloud storage:
# 1. Install b2sdk: `pip install b2sdk`
# 2. Initialize the B2 API client in a helper or service module.
# 3. In `save_file`, after saving locally or directly from memory, upload:
#    `b2_api.upload_local_file(bucket_name, local_file_path, b2_file_name)`
# 4. In `delete_file`, call `b2_api.delete_file_version` or move to a trash bucket.
# =====================================================================


def _ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def get_project_dir(project_id: Optional[str] = None) -> Path:
    if project_id:
        d = UPLOADS_ROOT / project_id
    else:
        d = UPLOADS_ROOT / "global"
    _ensure_dir(d)
    return d


def generate_file_name(project_name: Optional[str], format_ext: str) -> str:
    """Generate a safe, unique filename with strict sanitization (W195)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    raw_name = project_name or "global"
    # Replace all non-alphanumeric, non-underscore, non-dash characters with underscore
    safe_name = re.sub(r'[^\w\-]', '_', raw_name)
    # Deduplicate underscores and strip leading/trailing underscores
    safe_name = re.sub(r'_+', '_', safe_name).strip('_')[:30]
    return f"by8flow_{safe_name}_{ts}.{format_ext}"


ALLOWED_EXTENSIONS = {'.txt', '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.docx', '.pptx', '.csv'}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

def validate_mime_type(content: bytes, ext: str) -> bool:
    if not content:
        return True
    
    # Signature maps
    signatures = {
        '.pdf': [b'%PDF'],
        '.png': [b'\x89PNG\r\n\x1a\n'],
        '.jpg': [b'\xff\xd8\xff'],
        '.jpeg': [b'\xff\xd8\xff'],
        '.gif': [b'GIF87a', b'GIF89a'],
        '.docx': [b'PK\x03\x04'],
        '.pptx': [b'PK\x03\x04'],
        '.zip': [b'PK\x03\x04'],
    }
    
    if ext not in signatures:
        if ext in {'.txt', '.csv'}:
            # Heuristic check for plaintext: avoid binary NUL bytes
            return b'\x00' not in content[:1024]
        return True
        
    expected_sigs = signatures[ext]
    return any(content.startswith(sig) for sig in expected_sigs)

def save_file(project_id: Optional[str], file_name: str, content: bytes) -> str:
    if len(content) > MAX_FILE_SIZE:
        raise ValueError(f"File exceeds maximum size of {MAX_FILE_SIZE // (1024*1024)}MB")
        
    # Sanitize file name to prevent directory traversal and invalid characters
    safe_file_name = re.sub(r'[^\w\-\.]', '_', file_name)
    safe_file_name = safe_file_name.strip('_')
    
    # Validate extension
    ext = ""
    if '.' in safe_file_name:
        ext = safe_file_name[safe_file_name.rindex('.'):].lower()
    
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"File extension not allowed: {ext}")

    if not validate_mime_type(content, ext):
        raise ValueError(f"File content does not match extension: {ext}")

    d = get_project_dir(project_id)
    file_path = d / safe_file_name
    with open(file_path, "wb") as f:
        f.write(content)
    logger.info(f"Saved file: {file_path}")
    return str(file_path)


def read_file(file_path: str) -> Optional[bytes]:
    p = Path(file_path)
    if not p.exists():
        logger.error(f"File not found: {file_path}")
        return None
    with open(p, "rb") as f:
        return f.read()


def delete_file(file_path: str) -> bool:
    """Soft-delete a file by moving it to the .trash quarantine directory first (W198)."""
    if not file_path or file_path.strip() in ["", ".", ".."]:
        return False
    p = Path(file_path).resolve()
    if p.exists() and p.is_file():
        try:
            _ensure_dir(TRASH_ROOT)
            timestamp = int(datetime.now(timezone.utc).timestamp())
            trash_path = TRASH_ROOT / f"{p.stem}_{timestamp}{p.suffix}"
            
            # Move the file to quarantine instead of unlinking it
            shutil.move(str(p), str(trash_path))
            logger.info(f"Soft-deleted and quarantined file: {file_path} -> {trash_path}")
            return True
        except PermissionError as e:
            logger.error(f"Permission error soft-deleting {file_path}: {e}")
            return False
        except Exception as e:
            logger.error(f"Error soft-deleting {file_path}: {e}")
            return False
    return False


def get_file_size(file_path: str) -> int:
    p = Path(file_path)
    return p.stat().st_size if p.exists() else 0
