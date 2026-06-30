"""Test AI generation endpoint directly."""
import asyncio
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ai_client import ai_client

async def test():
    markdown = """# Test Project
    ## Features
    - User login
    - Dashboard
    """
    
    SYSTEM_PROMPT = """You are a project management AI assistant..."""
    
    try:
        content = await ai_client.chat_completion(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=f"Generate tasks for the following project description:\n\n{markdown}",
            provider="openrouter",
            model="poolside/laguna-m.1:free",
        )
        print(f"Content type: {type(content)}")
        print(f"Content: {content!r}")
        
        if content:
            parsed = json.loads(content)
            print(f"Parsed: {parsed}")
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test())
