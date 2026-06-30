"""
Technical role definitions for task assignment.

These roles are used for matching AI-generated tasks to developers.
The position field (e.g., "team lead", "hr") is separate and not used for matching.
"""

from typing import List
import json
from pathlib import Path

# Hardcoded fallback defaults
_DEFAULT_TECHNICAL_ROLES: List[str] = [
    # Frontend
    "frontend developer",
    "senior frontend developer",
    "junior frontend developer",
    "mid level frontend developer",
    # Backend
    "backend developer",
    "senior backend developer",
    "junior backend developer",
    "mid level backend developer",
    # Fullstack
    "full stack developer",
    "senior full stack developer",
    "junior full stack developer",
    "mid level full stack developer",
    # AI/ML
    "ai engineer",
    "senior ai engineer",
    "ml engineer",
    "senior ml engineer",
    # QA
    "qa engineer",
    "senior qa engineer",
    "junior qa engineer",
    # DevOps
    "devops engineer",
    "senior devops engineer",
    # Design
    "designer",
    "ui/ux designer",
    "senior ui/ux designer",
    # Other
    "mobile developer",
    "ios developer",
    "android developer",
    "data engineer",
    "database administrator",
    "security engineer",
]

_DEFAULT_ROLE_MAPPING: dict[str, str] = {
    "frontend": "frontend developer",
    "backend": "backend developer",
    "fullstack": "full stack developer",
    "ui_ux": "ui/ux designer",
    "qa": "qa engineer",
    "devops": "devops engineer",
    "ai": "ai engineer",
    "ml": "ml engineer",
    "mobile": "mobile developer",
    "design": "designer",
    "database": "database administrator",
    "security": "security engineer",
}

_DEFAULT_ROLE_ALIASES: dict[str, str] = {
    "full_stack": "fullstack",
    "full-stack": "fullstack",
    "frontend_developer": "frontend",
    "front-end": "frontend",
    "backend_developer": "backend",
    "back-end": "backend",
    "ux": "ui_ux",
    "ui": "ui_ux",
    "ui/ux": "ui_ux",
    "quality": "qa",
    "qc": "qa",
    "dev-ops": "devops",
    "sre": "devops",
    "machine_learning": "ml",
    "ios": "mobile",
    "android": "mobile",
}

# Dynamic Configuration Loader with Fallback
_config_path = Path(__file__).parent / "roles_config.json"
if _config_path.exists():
    try:
        with open(_config_path, "r", encoding="utf-8") as f:
            _config = json.load(f)
            TECHNICAL_ROLES = _config.get("TECHNICAL_ROLES", _DEFAULT_TECHNICAL_ROLES)
            ROLE_MAPPING = _config.get("ROLE_MAPPING", _DEFAULT_ROLE_MAPPING)
            ROLE_ALIASES = _config.get("ROLE_ALIASES", _DEFAULT_ROLE_ALIASES)
    except Exception:
        TECHNICAL_ROLES = _DEFAULT_TECHNICAL_ROLES
        ROLE_MAPPING = _DEFAULT_ROLE_MAPPING
        ROLE_ALIASES = _DEFAULT_ROLE_ALIASES
else:
    TECHNICAL_ROLES = _DEFAULT_TECHNICAL_ROLES
    ROLE_MAPPING = _DEFAULT_ROLE_MAPPING
    ROLE_ALIASES = _DEFAULT_ROLE_ALIASES
