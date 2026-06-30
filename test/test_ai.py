"""Test AI client directly."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import get_settings
from ai_client import ai_client

async def test():
    try:
        content = await ai_client.chat_completion(
            system_prompt="You are a helpful assistant.",
            user_prompt="Say 'Hello' in one word.",
            provider="openrouter",
            model="poolside/laguna-m.1:free",
        )
        print(f"Success! Content type: {type(content)}, value: {content!r}")
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}")

if __name__ == "__main__":
    asyncio.run(test())
