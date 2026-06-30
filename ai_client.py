"""Unified AI client supporting OpenRouter (primary) and Mistral (fallback)."""

import json
import logging
import random
import httpx
from typing import Optional, List, AsyncGenerator
from config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# Available OpenRouter free models
OPENROUTER_MODELS = [
    "poolside/laguna-m.1:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "poolside/laguna-xs.2:free",
]

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MISTRAL_BASE_URL = "https://api.mistral.ai/v1"


class AIClientError(Exception):
    """Base exception for AI client errors."""
    pass


class AIClient:
    """Unified AI client with OpenRouter primary and Mistral fallback."""

    def __init__(self):
        self.openrouter_key = settings.openrouter_api_key
        self.mistral_key = settings.mistral_api_key
        self.default_model = (
            settings.openrouter_default_model.strip()
            if settings.openrouter_default_model and settings.openrouter_default_model.strip()
            else (OPENROUTER_MODELS[0] if OPENROUTER_MODELS else "poolside/laguna-m.1:free")
        )
        self.site_url = settings.openrouter_site_url or "https://by8flow.com"
        self.site_name = settings.openrouter_site_name or "By8flow"

    def _pick_model(self, model: Optional[str] = None) -> str:
        """Pick model: explicit > default > first model from pool."""
        if model and model.strip():
            return model.strip()
        if self.default_model and self.default_model.strip():
            return self.default_model.strip()
        if OPENROUTER_MODELS:
            return OPENROUTER_MODELS[0]
        return "poolside/laguna-m.1:free"

    async def _call_openrouter(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.3,
        timeout: float = 60.0,
        response_format: Optional[dict] = None,
    ) -> dict:
        """Call OpenRouter API using OpenAI-compatible endpoint."""
        chosen_model = self._pick_model(model)

        headers = {
            "Authorization": f"Bearer {self.openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.site_url,
            "X-Title": self.site_name,
        }

        payload = {
            "model": chosen_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            )

        if response.status_code == 400 and response_format:
            # Fallback for models not supporting response_format
            logger.warning("OpenRouter returned 400 with response_format, retrying without it.")
            payload_no_format = dict(payload)
            payload_no_format.pop("response_format", None)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload_no_format,
                )

        if response.status_code != 200:
            err_text = response.text[:1000]
            raise AIClientError(
                f"OpenRouter API error ({response.status_code}): {err_text}"
            )

        result = response.json()
        if "choices" not in result or not result["choices"]:
            raise AIClientError("OpenRouter returned no choices")

        # Check for error in the response
        choice = result["choices"][0]
        if "error" in choice:
            error_info = choice["error"]
            error_msg = error_info.get("message", str(error_info)) if isinstance(error_info, dict) else str(error_info)
            logger.error(f"OpenRouter error in response: {error_msg}, full result: {result}")
            raise AIClientError(f"OpenRouter error: {error_msg}")

        if "message" not in choice:
            logger.error(f"OpenRouter response missing 'message' field. Choice: {choice}")
            raise AIClientError("OpenRouter response missing 'message' field")

        content = choice["message"]["content"]
        if content is None:
            logger.error(f"OpenRouter returned None content. Full choice: {choice}")
            raise AIClientError("OpenRouter returned None content (provider may have failed)")
        if not isinstance(content, str):
            logger.error(f"OpenRouter returned non-string content type: {type(content)}, value: {content}")
            raise AIClientError(f"OpenRouter returned non-string content type: {type(content)}")

        logger.debug(f"OpenRouter content[:200]: {content[:200]}")

        return result

    async def _call_mistral(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.3,
        timeout: float = 60.0,
    ) -> dict:
        """Call Mistral API as fallback."""
        headers = {
            "Authorization": f"Bearer {self.mistral_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": "mistral-medium",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{MISTRAL_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            )

        if response.status_code != 200:
            err_text = response.text[:1000]
            raise AIClientError(
                f"Mistral API error ({response.status_code}): {err_text}"
            )

        result = response.json()
        if "choices" not in result or not result["choices"]:
            raise AIClientError("Mistral returned no choices")

        # Check for error in the response
        choice = result["choices"][0]
        if "error" in choice:
            error_info = choice["error"]
            error_msg = error_info.get("message", str(error_info)) if isinstance(error_info, dict) else str(error_info)
            raise AIClientError(f"Mistral error: {error_msg}")

        if "message" not in choice:
            raise AIClientError("Mistral response missing 'message' field")

        content = choice["message"]["content"]
        if content is None:
            raise AIClientError("Mistral returned None content")
        if not isinstance(content, str):
            raise AIClientError(f"Mistral returned non-string content type: {type(content)}")

        return result

    async def chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        temperature: float = 0.3,
        timeout: float = 60.0,
        response_format: Optional[dict] = None,
    ) -> str:
        """
        Generate a chat completion.
        
        Args:
            provider: "openrouter" | "mistral" | None (auto: tries OpenRouter then Mistral)
            model: model override (e.g. "poolside/laguna-m.1:free" or "mistral-medium")
            response_format: Optional dict for structured output (e.g. JSON format)
        
        Returns the raw text content from the AI response.
        """
        last_error = None
        chosen_provider = (provider or "auto").lower().strip()

        logger.debug(f"provider={provider}, chosen_provider={chosen_provider}, openrouter_key_exists={bool(self.openrouter_key)}, mistral_key_exists={bool(self.mistral_key)}")

        if chosen_provider in ("auto", "openrouter"):
            # Try OpenRouter first if key is configured
            if self.openrouter_key and not any(self.openrouter_key.startswith(p) for p in ("placeholder", "your-", "<your-")):
                try:
                    chosen_model = self._pick_model(model)
                    logger.debug(f"Trying OpenRouter with model {chosen_model}")
                    result = await self._call_openrouter(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        model=model,
                        temperature=temperature,
                        timeout=timeout,
                        response_format=response_format,
                    )
                    content = result["choices"][0]["message"]["content"]
                    logger.debug(f"OpenRouter success, content length: {len(content)}")
                    return content
                except Exception as e:
                    last_error = e
                    logger.debug(f"OpenRouter failed: {e}")
                    if chosen_provider == "openrouter":
                        raise AIClientError(f"OpenRouter failed: {last_error}")
                    # auto mode: fall through to Mistral

        if chosen_provider in ("auto", "mistral"):
            # Try Mistral
            if self.mistral_key and self.mistral_key.strip():
                try:
                    logger.debug("Trying Mistral")
                    result = await self._call_mistral(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        temperature=temperature,
                        timeout=timeout,
                    )
                    content = result["choices"][0]["message"]["content"]
                    logger.debug(f"Mistral success, content length: {len(content)}")
                    return content
                except Exception as e:
                    last_error = e
                    logger.debug(f"Mistral failed: {e}")
                    if chosen_provider == "mistral":
                        raise AIClientError(f"Mistral failed: {last_error}")

        # Both failed (auto mode) or provider not available
        raise AIClientError(
            f"All AI providers failed. Last error: {last_error}"
        )

    async def chat_completion_stream(
        self,
        system_prompt: str,
        messages: List[dict],
        model: Optional[str] = None,
        provider: Optional[str] = None,
        temperature: float = 0.3,
        timeout: float = 60.0,
    ) -> AsyncGenerator[str, None]:
        """
        Stream a chat completion via Server-Sent Events (SSE).
        
        Args:
            system_prompt: system context
            messages: conversation history [{"role": "user|assistant", "content": "..."}]
            model: optional model override
            provider: "openrouter" | "mistral" | "auto"
        
        Yields:
            Token strings as they arrive (accumulate on client side)
        
        Note: OpenRouter supports streaming. Mistral fallback currently does not — 
        will fall back to non-streaming chat_completion and yield the full response.
        """
        chosen_provider = (provider or "auto").lower().strip()

        if chosen_provider in ("auto", "openrouter"):
            if self.openrouter_key and not any(self.openrouter_key.startswith(p) for p in ("placeholder", "your-", "<your-")):
                try:
                    chosen_model = self._pick_model(model)
                    headers = {
                        "Authorization": f"Bearer {self.openrouter_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": self.site_url,
                        "X-Title": self.site_name,
                    }
                    payload = {
                        "model": chosen_model,
                        "messages": [{"role": "system", "content": system_prompt}] + messages,
                        "temperature": temperature,
                        "stream": True,  # enable SSE
                    }
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        async with client.stream(
                            "POST",
                            f"{OPENROUTER_BASE_URL}/chat/completions",
                            headers=headers,
                            json=payload,
                        ) as resp:
                            if resp.status_code != 200:
                                raise AIClientError(f"OpenRouter streaming error: {resp.status_code}")
                            try:
                                async for line in resp.aiter_lines():
                                    if line.startswith("data: "):
                                        data = line[6:].strip()
                                        if data == "[DONE]":
                                            break
                                        try:
                                            chunk = json.loads(data)
                                            if "error" in chunk:
                                                err_msg = chunk["error"]
                                                if isinstance(err_msg, dict):
                                                    err_msg = err_msg.get("message", str(err_msg))
                                                logger.error(f"OpenRouter stream error: {err_msg}")
                                                yield "[Error: An error occurred during the streaming request.]"
                                                yield "[DONE]"
                                                return
                                            delta = chunk["choices"][0].get("delta", {})
                                            token = delta.get("content")
                                            if token is not None and token != "":
                                                yield token
                                        except (json.JSONDecodeError, KeyError, IndexError):
                                            continue
                            finally:
                                await resp.aclose()
                    return
                except Exception as e:
                    logger.error(f"OpenRouter stream failed: {e}")
                    if chosen_provider == "openrouter":
                        yield "[Error: An error occurred during the streaming request.]"
                        yield "[DONE]"
                        return

        # Fallback: use non-streaming Mistral and yield full response at once
        if chosen_provider in ("auto", "mistral"):
            if self.mistral_key and self.mistral_key.strip():
                # Format messages for Mistral manually since chat_completion only takes string user_prompt
                headers = {
                    "Authorization": f"Bearer {self.mistral_key}",
                    "Content-Type": "application/json",
                }
                
                # Combine system prompt with messages
                mistral_messages = []
                if system_prompt:
                    mistral_messages.append({"role": "system", "content": system_prompt})
                mistral_messages.extend(messages)
                
                payload = {
                    "model": "mistral-medium",
                    "messages": mistral_messages,
                    "temperature": temperature,
                }
                
                import httpx
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(
                        f"{MISTRAL_BASE_URL}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    
                if response.status_code == 200:
                    result = response.json()
                    full = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                else:
                    full = f"[Error: Mistral fallback failed with status {response.status_code}]"
                    
                yield full
                return

        raise AIClientError("Streaming failed: no provider available")


# Global singleton client
ai_client = AIClient()
