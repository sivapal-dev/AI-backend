import logging
import httpx
import asyncio
from database import get_database

logger = logging.getLogger(__name__)

class WebhookService:
    async def trigger_webhooks(self, event_type: str, payload: dict) -> None:
        """
        Dispatches webhooks asynchronously in the background.
        """
        asyncio.create_task(self._dispatch_webhooks(event_type, payload))

    async def _dispatch_webhooks(self, event_type: str, payload: dict) -> None:
        try:
            db = get_database()
            cursor = db.webhooks.find({"active": {"$ne": False}})
            webhooks = await cursor.to_list(length=100)
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                tasks = []
                for wh in webhooks:
                    url = wh.get("url")
                    events = wh.get("events", [])
                    
                    # Check if this webhook is registered for this event
                    if events and "*" not in events and event_type not in events:
                        continue
                    
                    tasks.append(self._send_payload(client, url, event_type, payload, wh.get("secret")))
                
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"Error in background webhook dispatcher: {e}")

    async def _send_payload(self, client: httpx.AsyncClient, url: str, event_type: str, payload: dict, secret: str = None) -> None:
        headers = {
            "Content-Type": "application/json",
            "X-Event-Type": event_type,
        }
        if secret:
            headers["X-Webhook-Secret"] = secret
            
        data = {
            "event": event_type,
            "payload": payload,
        }
        
        try:
            resp = await client.post(url, json=data, headers=headers)
            if resp.status_code >= 400:
                logger.warning(f"Webhook dispatch to {url} failed with status {resp.status_code}")
            else:
                logger.info(f"Webhook dispatch to {url} succeeded for event {event_type}")
        except Exception as e:
            logger.error(f"Error dispatching webhook to {url}: {e}")

webhook_service = WebhookService()
