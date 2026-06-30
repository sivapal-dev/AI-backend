"""
Code Fixer Service — safely applies code changes to files.
Implements safety checks: backup, verification, atomic writes.
"""
import os
import shutil
import tempfile
from typing import List, Optional, Tuple
from pathlib import Path
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Base directory for the project - adjust as needed
PROJECT_ROOT = Path(__file__).parent.parent.parent  # backend/ -> project root


class CodeFixer:
    """
    Safely applies code fixes by:
    1. Reading current file
    2. Verifying expected old_code exists (prevent stale patches)
    3. Writing to temp file
    4. Atomic rename (backup original)
    """

    def __init__(self, allowed_roots: Optional[List[str]] = None):
        """
        allowed_roots: list of directory paths the fixer is allowed to modify.
        Defaults to the frontend app directory.
        """
        WORKSPACE_ROOT = PROJECT_ROOT.parent
        self.allowed_roots = allowed_roots or [
            str(WORKSPACE_ROOT / "by8flow" / "by8flow" / "app"),
        ]
        self.backup_dir = PROJECT_ROOT / "backups" / "code_fixer"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _is_allowed_path(self, file_path: str) -> Tuple[bool, str]:
        """Check if the file path is within allowed roots."""
        target = Path(file_path).resolve()
        for root in self.allowed_roots:
            allowed_root = Path(root).resolve()
            try:
                target.relative_to(allowed_root)
                return True, str(allowed_root)
            except ValueError:
                continue
        return False, ""

    def replace_code(self, file_path: str, old_code: str, new_code: str) -> bool:
        """
        Replace old_code with new_code in the given file.
        Returns True if successful, raises detailed exceptions on validation failures.
        """
        # Security: reject absolute paths
        if Path(file_path).is_absolute() or file_path.startswith("/") or file_path.startswith("\\"):
            msg = f"Access denied: absolute path not allowed: {file_path}"
            logger.error(msg)
            raise PermissionError(msg)

        full_path = (PROJECT_ROOT.parent / file_path).resolve()

        # Security: restrict file access
        allowed, root = self._is_allowed_path(full_path)
        if not allowed:
            msg = f"Access denied: {file_path} is not within allowed roots: {self.allowed_roots}"
            logger.error(msg)
            raise PermissionError(msg)

        if not full_path.exists():
            msg = f"File not found: {file_path}"
            logger.error(msg)
            raise FileNotFoundError(msg)

        # Read current content with encoding fallback
        encoding = "utf-8"
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                current_content = f.read()
        except UnicodeDecodeError:
            try:
                with open(full_path, "r", encoding="latin-1") as f:
                    current_content = f.read()
                encoding = "latin-1"
            except Exception as e:
                msg = f"Failed to read {file_path} with fallback encoding: {e}"
                logger.error(msg)
                raise IOError(msg)
        except Exception as e:
            msg = f"Failed to read {file_path}: {e}"
            logger.error(msg)
            raise IOError(msg)

        # Verify old_code matches what we expect (prevent stale/incorrect patches)
        if old_code and old_code not in current_content:
            msg = f"Old code not found in {file_path}. Patch may be stale."
            logger.error(msg)
            raise ValueError(msg)

        if not old_code:
            msg = f"Rejecting complete file overwrite for {file_path}. old_code must not be empty."
            logger.error(msg)
            raise ValueError(msg)

        new_content = current_content.replace(old_code, new_code, 1)  # Replace first occurrence only

        if new_content == current_content:
            logger.warning(f"No changes applied to {file_path}")
            return True  # No-op is not an error

        # Create backup with folder hashing to prevent name collisions
        import hashlib
        dir_hash = hashlib.md5(str(full_path.parent).encode("utf-8")).hexdigest()[:8]
        backup_path = self.backup_dir / f"{full_path.name}.{dir_hash}.{int(datetime.now().timestamp())}.bak"
        try:
            shutil.copy2(full_path, backup_path)
        except Exception as e:
            logger.warning(f"Failed to create backup: {e}")

        # Write to temp file first using the detected encoding
        temp_fd, temp_path = tempfile.mkstemp(dir=full_path.parent, suffix=".tmp")
        try:
            with os.fdopen(temp_fd, "w", encoding=encoding) as tmp:
                tmp.write(new_content)
            
            # Atomic move
            shutil.move(temp_path, full_path)
            logger.info(f"Successfully patched {file_path} (backup: {backup_path.name})")
            return True
        except Exception as e:
            logger.error(f"Failed to write {file_path}: {e}")
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise IOError(f"Failed to write {file_path}: {e}")

    def read_file(self, file_path: str) -> Optional[str]:
        """Read a file safely (within allowed roots)."""
        # Security: reject absolute paths
        if Path(file_path).is_absolute() or file_path.startswith("/") or file_path.startswith("\\"):
            msg = f"Access denied: absolute path not allowed: {file_path}"
            logger.error(msg)
            raise PermissionError(msg)

        full_path = (PROJECT_ROOT.parent / file_path).resolve()
        allowed, _ = self._is_allowed_path(full_path)
        if not allowed:
            msg = f"Access denied: {file_path} is not within allowed roots"
            logger.error(msg)
            raise PermissionError(msg)

        if not full_path.exists():
            return None

        try:
            with open(full_path, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError:
            try:
                with open(full_path, "r", encoding="latin-1") as f:
                    return f.read()
            except Exception as e:
                logger.error(f"Failed to read {file_path} with fallback encoding: {e}")
                raise IOError(f"Failed to read {file_path}: {e}")
        except Exception as e:
            logger.error(f"Failed to read {file_path}: {e}")
            raise IOError(f"Failed to read {file_path}: {e}")


# Global instance
code_fixer = CodeFixer()

