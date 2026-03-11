"""
Personal WhatsApp webhook — receives messages from LAD_backend's Baileys bridge.

Endpoint:
  POST /webhook/personal-whatsapp — Receive normalized personal WhatsApp messages

Flow:
  1. LAD_backend receives message via Baileys (personal WhatsApp)
  2. LAD_backend normalizes and POSTs here with X-Tenant-ID header
  3. We resolve the tenant's WhatsAppAccount config (AI model, flow template, etc.)
  4. Inject personal channel metadata into the account object
  5. Route through existing message handler pipeline
  6. Reply is sent back via personal_whatsapp_client → LAD_backend → Baileys

Reuses: leads, conversations, messages, conversation_states, prompts tables.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Optional

from fastapi import APIRouter, Request, BackgroundTasks, HTTPException, Depends

from services.message_handler import handle_incoming_message
from services.account_registry import (
    get_account_by_tenant_id,
    get_default_account,
    WhatsAppAccount,
)
from middleware.tenant import get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["personal-whatsapp"])


@router.post("/api/personal-whatsapp/webhook")
async def receive_personal_whatsapp_message(
    request: Request,
    background_tasks: BackgroundTasks,
    tenant_id: Optional[str] = Depends(get_tenant_id),
):
    """Receive an incoming personal WhatsApp message from LAD_backend.

    Expected payload:
    {
        "account_id": "personal-account-uuid",
        "contact_phone": "+1234567890",
        "text": "Hello",
        "external_message_id": "msg-uuid",
        "contact_name": "John Doe",
        "metadata": {}
    }
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    # Validate required fields
    contact_phone = data.get("contact_phone") or data.get("from", "")
    text = data.get("text", "")
    external_message_id = data.get("external_message_id") or data.get("gateway_message_id", "")
    personal_account_id = data.get("account_id", "")

    if not contact_phone or not text:
        raise HTTPException(400, "contact_phone and text are required")

    if not external_message_id:
        raise HTTPException(400, "external_message_id is required")

    # Resolve tenant's WhatsApp account config (for AI model, flow template, etc.)
    account = None
    if tenant_id:
        account = get_account_by_tenant_id(tenant_id)
    else:
        logger.warning("No X-Tenant-ID header provided in personal webhook request")

    if not account:
        error_msg = f"No WhatsApp account configured for tenant: {tenant_id or 'unknown'}"
        logger.error(f"[personal_webhook] {error_msg}")
        raise HTTPException(500, error_msg)

    # Create an augmented account with personal WhatsApp channel metadata.
    # This tells the message handler to route replies through the personal
    # WhatsApp client instead of the Meta Cloud API.
    personal_metadata = {
        **account.metadata,
        "channel": "personal_whatsapp",
        "personal_account_id": personal_account_id,
        "lad_backend_url": data.get("lad_backend_url", ""),
    }
    personal_account = replace(account, metadata=personal_metadata)

    contact_name = data.get("contact_name", "")

    # Process in background (same as business WhatsApp webhook)
    background_tasks.add_task(
        _process_personal_message,
        contact_phone,
        text,
        contact_name,
        external_message_id,
        personal_account,
    )

    return {"status": "received", "message_id": external_message_id}


async def _process_personal_message(
    phone_number: str,
    message_text: str,
    contact_name: str,
    external_message_id: str,
    account: WhatsAppAccount,
):
    """Background processing of a personal WhatsApp message."""
    try:
        await handle_incoming_message(
            phone_number=phone_number,
            message_text=message_text,
            contact_name=contact_name,
            external_message_id=external_message_id,
            chapter=account,
        )
    except Exception as e:
        logger.error(
            f"[{account.slug}] Error processing personal WA message from {phone_number}: {e}",
            exc_info=True,
        )
