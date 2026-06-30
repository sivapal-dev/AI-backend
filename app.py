import os
import uvicorn
from main import app

if __name__ == "__main__":
    reload_enabled = os.getenv("BACKEND_RELOAD", "false").lower() == "true"
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=reload_enabled,
    )
