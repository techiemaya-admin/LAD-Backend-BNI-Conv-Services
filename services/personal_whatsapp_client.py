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

# LAD backend personal WhatsApp feature is mounted at /api/personal-whatsapp/*
_PERSONAL_WA_SEND_PATH = "/api/personal-whatsapp/send"

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

    try:
        client = _get_client()

        # Use service-to-service tenant context so LAD backend auth middleware can
        # bypass JWT and route request under the correct tenant.
        headers = {
            "Content-Type": "application/json",
            "X-Service-Context": "conversation-service",
        }
        if account and account.tenant_id:
            headers["X-Tenant-ID"] = account.tenant_id

        # Resolve to an active Baileys session when provided account_id is stale
        # (for example, a DB social account UUID instead of an in-memory session ID).
        resolved_account_id = await _resolve_active_session_id(
            client=client,
            base_url=base_url,
            requested_account_id=personal_account_id,
            headers=headers,
            account=account,
        )

        payload = {
            "account_id": resolved_account_id,
            "to": phone_number,
            "text": text,
        }
        
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


async def _resolve_active_session_id(
    client: httpx.AsyncClient,
    base_url: str,
    requested_account_id: str,
    headers: dict,
    account: Optional[WhatsAppAccount],
) -> str:
    """Resolve a requested account_id to an active personal WhatsApp session ID.

    If the requested ID is not present among currently active sessions, fallback to
    the first connected tenant session exposed by LAD backend /accounts endpoint.
    """
    accounts_url = f"{base_url}/api/personal-whatsapp/accounts"
    slug = account.slug if account else "personal"

    try:
        resp = await client.get(accounts_url, headers=headers)
        if resp.status_code != 200:
            logger.warning(
                f"[{slug}] Failed to fetch personal WA accounts for session resolution: "
                f"HTTP {resp.status_code}"
            )
            return requested_account_id

        data = resp.json() if resp.content else {}
        accounts = data.get("accounts") if isinstance(data, dict) else None
        if not isinstance(accounts, list) or not accounts:
            return requested_account_id

        connected = [a for a in accounts if a.get("status") == "connected"]
        if not connected:
            return requested_account_id

        # Keep requested ID if it already matches any known session ID.
        for acc in connected:
            if requested_account_id in {acc.get("id"), acc.get("gateway_account_id")}:
                return requested_account_id

        fallback_id = connected[0].get("id")
        if fallback_id:
            logger.warning(
                f"[{slug}] Using active personal WA session fallback",
                extra={
                    "requested_account_id": requested_account_id,
                    "resolved_account_id": fallback_id,
                    "connected_sessions": len(connected),
                },
            )
            return str(fallback_id)
    except Exception as e:
        logger.warning(
            f"[{slug}] Error resolving active personal WA session id: {e}",
            exc_info=True,
        )

    return requested_account_id


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
