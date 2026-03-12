"""
Personal WhatsApp client — sends messages via LAD_backend's Baileys bridge.

Instead of calling the Meta Cloud API directly, this client POSTs to
LAD_backend's personal-whatsapp send endpoint, which routes the message
through Baileys to the user's personal WhatsApp number.

Reuses the same DB save logic as whatsapp_client for message persistence.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Optional

import httpx

from db.connection import AsyncDBConnection
from services.account_registry import WhatsAppAccount

logger = logging.getLogger(__name__)

# LAD_backend base URL (where Baileys bridge lives)
_LAD_BACKEND_URL = os.getenv(
    "LAD_BACKEND_URL",
    os.getenv("NEXT_PUBLIC_BACKEND_URL", "http://localhost:3001"),
)

_PERSONAL_WA_SEND_PATH = "/api/features/personal-whatsapp/send"

# Shared HTTP client
_http_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=15.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client


async def send_message(
    phone_number: str,
    text: str,
    personal_account_id: str,
    conversation_id: str | None = None,
    lead_id: str | None = None,
    account: Optional[WhatsAppAccount] = None,
    lad_backend_url: str | None = None,
) -> str | None:
    """Send a message via personal WhatsApp (Baileys bridge).

    Args:
        phone_number: Recipient phone number.
        text: Message text.
        personal_account_id: The personal_whatsapp_accounts.id in the tenant DB.
        conversation_id: Conversation UUID for DB persistence.
        lead_id: Lead UUID for DB persistence.
        account: WhatsAppAccount for tenant context.
        lad_backend_url: Override for LAD_backend URL.

    Returns:
        gateway_message_id on success, None on failure.
    """
    base_url = lad_backend_url or _LAD_BACKEND_URL
    send_url = f"{base_url}{_PERSONAL_WA_SEND_PATH}"
    slug = account.slug if account else "personal"
    t0 = time.time()

    payload = {
        "account_id": personal_account_id,
        "to": phone_number,
        "text": text,
    }

    try:
        client = _get_client()

        # Build headers - include auth if we have it
        headers = {"Content-Type": "application/json"}
        
        # For development: use bypass token or dummy token
        # In production, LAD_backend should have JWT_SECRET configured
        dev_token = os.getenv("LAD_SERVICE_TOKEN") or os.getenv("JWT_TOKEN")
        if dev_token:
            headers["Authorization"] = f"Bearer {dev_token}"
        else:
            # Development bypass: use a service account marker
            # This assumes LAD_backend has dev mode or skips auth for localhost
            headers["X-Service-Context"] = "conversation-service"
        
        response = await client.post(
            send_url,
            json=payload,
            headers=headers,
        )
        logger.info(f"[{slug}][TIMING] personal_wa_send: {time.time()-t0:.3f}s")

        if response.status_code in (200, 201):
            resp_data = response.json()
            gateway_message_id = resp_data.get("gateway_message_id", "")
            internal_id = str(uuid.uuid4())

            if conversation_id and lead_id and account:
                t1 = time.time()
                await _save_outgoing_message(
                    internal_id, gateway_message_id, conversation_id,
                    lead_id, text, account,
                )
                logger.info(f"[{slug}][TIMING] save_outgoing_message_db: {time.time()-t1:.3f}s")

            logger.info(f"[{slug}] Personal WA message sent to {phone_number}: {gateway_message_id}")
            return gateway_message_id
        else:
            logger.error(f"Personal WA send failed ({response.status_code}): {response.text}")
            return None

    except Exception as e:
        logger.error(f"Error sending personal WA message to {phone_number}: {e}")
        return None


async def _save_outgoing_message(
    internal_id: str,
    external_message_id: str,
    conversation_id: str,
    lead_id: str,
    content: str,
    account: WhatsAppAccount,
):
    """Save outgoing AI message to the messages table."""
    try:
        async with AsyncDBConnection(account.tenant_id) as conn:
            await conn.execute(
                """
                INSERT INTO messages (id, conversation_id, lead_id, role, content,
                    message_status, external_message_id, tenant_id, created_at)
                VALUES ($1::uuid, $2::uuid, $3::uuid, 'agent', $4, 'sent', $5, $6::uuid, NOW())
                """,
                internal_id, conversation_id, lead_id, content,
                external_message_id, account.tenant_id,
            )
    except Exception as e:
        logger.error(f"Error saving outgoing personal WA message: {e}")
