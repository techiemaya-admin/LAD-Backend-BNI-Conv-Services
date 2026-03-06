"""
WhatsApp Business API client for sending messages.
"""
import logging
import os
import time
import uuid

import httpx

from db.connection import ClientDBConnection

logger = logging.getLogger(__name__)

PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
API_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"

# Shared HTTP client for connection pooling (reuses TCP/TLS connections)
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Get or create the shared HTTP client."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=15.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client


def _get_headers() -> dict:
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


async def mark_as_read(message_id: str) -> None:
    """Mark an incoming message as read (shows blue ticks)."""
    if not PHONE_NUMBER_ID or not ACCESS_TOKEN:
        return
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    try:
        client = _get_client()
        await client.post(API_URL, headers=_get_headers(), json=payload)
    except Exception as e:
        logger.debug(f"mark_as_read failed: {e}")


WABA_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "")

# Cache for auto-discovered WABA ID
_resolved_waba_id: str | None = None


async def _get_waba_id() -> str:
    """Return WABA ID from env or auto-discover from phone number ID."""
    global _resolved_waba_id
    if WABA_ID:
        return WABA_ID
    if _resolved_waba_id:
        return _resolved_waba_id
    if not PHONE_NUMBER_ID or not ACCESS_TOKEN:
        return ""
    try:
        client = _get_client()
        # Meta Graph API: phone number → whatsapp_business_account field
        resp = await client.get(
            f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}",
            headers=_get_headers(),
            params={"fields": "whatsapp_business_account"},
        )
        if resp.status_code == 200:
            waba = resp.json().get("whatsapp_business_account", {})
            _resolved_waba_id = waba.get("id", "")
            if _resolved_waba_id:
                logger.info(f"Auto-discovered WABA ID: {_resolved_waba_id}")
                return _resolved_waba_id
        logger.warning(f"Could not auto-discover WABA ID (status={resp.status_code}). Set WHATSAPP_BUSINESS_ACCOUNT_ID in .env")
    except Exception as e:
        logger.error(f"Failed to auto-discover WABA ID: {e}")
    return ""


async def get_message_templates() -> list[dict]:
    """Fetch approved message templates from Meta Business API."""
    waba_id = await _get_waba_id()
    if not waba_id or not ACCESS_TOKEN:
        logger.warning("Cannot fetch templates: WABA ID not available. Set WHATSAPP_BUSINESS_ACCOUNT_ID in .env")
        return []
    templates_url = f"https://graph.facebook.com/v21.0/{waba_id}/message_templates"
    try:
        client = _get_client()
        response = await client.get(
            templates_url,
            headers=_get_headers(),
            params={"status": "APPROVED", "limit": 100},
        )
        if response.status_code == 200:
            data = response.json()
            templates = []
            for t in data.get("data", []):
                # Extract body text from components
                body_text = ""
                params = []
                for comp in t.get("components", []):
                    if comp.get("type") == "BODY":
                        body_text = comp.get("text", "")
                        # Extract parameter placeholders like {{1}}, {{2}}
                        import re
                        params = re.findall(r"\{\{(\d+)\}\}", body_text)

                templates.append({
                    "name": t["name"],
                    "language": t.get("language", "en_US"),
                    "status": t.get("status", ""),
                    "category": t.get("category", ""),
                    "body": body_text,
                    "parameter_count": len(params),
                })
            return templates
        else:
            logger.error(f"Failed to fetch templates ({response.status_code}): {response.text}")
            return []
    except Exception as e:
        logger.error(f"Error fetching message templates: {e}")
        return []


async def send_template_message(
    phone_number: str,
    template_name: str,
    language_code: str = "en_US",
    parameters: list[str] | None = None,
    conversation_id: str | None = None,
    lead_id: str | None = None,
) -> str | None:
    """Send a WhatsApp message using a pre-approved template.

    Returns the WhatsApp external message ID on success, None on failure.
    """
    components = []
    if parameters:
        components.append({
            "type": "body",
            "parameters": [
                {"type": "text", "text": p} for p in parameters
            ],
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": phone_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
        },
    }
    if components:
        payload["template"]["components"] = components

    try:
        client = _get_client()
        response = await client.post(API_URL, headers=_get_headers(), json=payload)

        if response.status_code in (200, 201):
            resp_data = response.json()
            wa_message_id = resp_data.get("messages", [{}])[0].get("id", "")
            internal_id = str(uuid.uuid4())

            # Build a readable content string for DB storage
            content = f"[Template: {template_name}]"
            if parameters:
                content += f" params={parameters}"

            if conversation_id and lead_id:
                await _save_outgoing_message(
                    internal_id, wa_message_id, conversation_id, lead_id, content
                )

            logger.info(f"Template '{template_name}' sent to {phone_number}: {wa_message_id}")
            return wa_message_id
        else:
            logger.error(
                f"Template send failed ({response.status_code}): {response.text}"
            )
            return None

    except Exception as e:
        logger.error(f"Error sending template to {phone_number}: {e}")
        return None


async def send_typing_indicator(phone_number: str) -> None:
    """Send typing indicator to show the bot is 'thinking'.

    Uses WhatsApp's reaction or read receipt to simulate typing.
    WhatsApp Business API doesn't have a native typing indicator,
    so we use the 'contacts' presence approach or just mark as read quickly.
    """
    # WhatsApp Cloud API doesn't support typing indicators directly.
    # The best UX signal is marking messages as "read" immediately (blue ticks).
    # This is already done via mark_as_read().
    pass


async def send_message(
    phone_number: str,
    text: str,
    conversation_id: str | None = None,
    lead_id: str | None = None,
) -> str | None:
    """Send a WhatsApp text message and save to DB.

    Returns the WhatsApp external message ID on success, None on failure.
    """
    t0 = time.time()
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone_number,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }

    try:
        client = _get_client()
        response = await client.post(API_URL, headers=_get_headers(), json=payload)
        logger.info(f"[TIMING] whatsapp_api_post: {time.time()-t0:.3f}s")

        if response.status_code in (200, 201):
            resp_data = response.json()
            wa_message_id = resp_data.get("messages", [{}])[0].get("id", "")
            internal_id = str(uuid.uuid4())

            # Save outgoing message to DB (don't block on it)
            if conversation_id and lead_id:
                t1 = time.time()
                await _save_outgoing_message(
                    internal_id, wa_message_id, conversation_id, lead_id, text
                )
                logger.info(f"[TIMING] save_outgoing_message_db: {time.time()-t1:.3f}s")

            logger.info(f"Message sent to {phone_number}: {wa_message_id}")
            return wa_message_id
        else:
            logger.error(
                f"WhatsApp send failed ({response.status_code}): {response.text}"
            )
            return None

    except Exception as e:
        logger.error(f"Error sending WhatsApp message to {phone_number}: {e}")
        return None


async def _save_outgoing_message(
    internal_id: str,
    external_message_id: str,
    conversation_id: str,
    lead_id: str,
    content: str,
):
    """Save an outgoing AI message to the messages table."""
    try:
        async with ClientDBConnection() as conn:
            await conn.execute(
                """
                INSERT INTO messages (id, conversation_id, lead_id, role, content,
                    message_status, external_message_id, created_at)
                VALUES ($1::uuid, $2::uuid, $3::uuid, 'AI', $4, 'sent', $5, NOW())
                """,
                internal_id,
                conversation_id,
                lead_id,
                content,
                external_message_id,
            )
    except Exception as e:
        logger.error(f"Error saving outgoing message: {e}")
