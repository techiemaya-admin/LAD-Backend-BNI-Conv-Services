"""
LinkedIn DM webhook endpoint.

Receives incoming LinkedIn DMs forwarded by LAD_backend's LinkedInWebhookHandler.
Constructs a synthetic WhatsAppAccount with channel='linkedin' and routes through
the existing message handler pipeline (lead/conversation management + AI reply).

Endpoint:
  POST /api/linkedin-webhook

Expected payload (from LAD_backend):
  {
    "account_id": "unipile_account_id",
    "tenant_id": "uuid",
    "unipile_conversation_id": "linkedin_conv_id",
    "sender_id": "linkedin_profile_id",
    "sender_name": "John Doe",
    "message_text": "Hello",
    "message_id": "msg_id",
    "timestamp": 1234567890
  }
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import replace

from fastapi import APIRouter, Request, BackgroundTasks

from services.message_handler import handle_incoming_message
from services.account_registry import (
    get_account_by_tenant_id,
    WhatsAppAccount,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["linkedin-webhook"])

LAD_BACKEND_URL = os.getenv("LAD_BACKEND_URL", "http://localhost:3004")
CHANNEL_LINKEDIN = "linkedin"


@router.post("/api/linkedin-webhook")
async def receive_linkedin_dm(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Receive an incoming LinkedIn DM from LAD_backend."""
    try:
        data = await request.json()
    except Exception:
        return {"status": "error", "error": "Invalid JSON"}, 400

    account_id = data.get("account_id", "")
    tenant_id = data.get("tenant_id", "")
    unipile_conversation_id = data.get("unipile_conversation_id", "")
    sender_id = data.get("sender_id", "")
    sender_name = data.get("sender_name") or "LinkedIn User"
    message_text = data.get("message_text", "").strip()
    message_id = data.get("message_id") or f"li-{account_id[:8]}-{sender_id[:8]}-{int(time.time())}"

    if not sender_id or not message_text:
        logger.warning("[LinkedInWebhook] Missing sender_id or message_text")
        return {"status": "ignored", "reason": "missing sender_id or message_text"}

    if not tenant_id:
        logger.error("[LinkedInWebhook] Missing tenant_id — cannot route message")
        return {"status": "error", "error": "tenant_id required"}

    # Build a synthetic account object for the LinkedIn channel.
    # Try to inherit AI model + API key from the tenant's existing WhatsApp account.
    base_account = get_account_by_tenant_id(tenant_id)

    if base_account:
        account = replace(
            base_account,
            conversation_flow_template="linkedin",
            metadata={
                **base_account.metadata,
                "channel": CHANNEL_LINKEDIN,
                "unipile_account_id": account_id,
                "unipile_conversation_id": unipile_conversation_id,
                "lad_backend_url": LAD_BACKEND_URL,
            },
        )
        logger.info(
            f"[LinkedInWebhook] Using base account '{base_account.slug}' for tenant {tenant_id[:8]} "
            f"(AI model: {base_account.ai_model})"
        )
    else:
        # No WhatsApp account for this tenant — create a minimal LinkedIn account
        account = WhatsAppAccount(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            slug=f"linkedin-{account_id[:8] if account_id else 'default'}",
            display_name="LinkedIn",
            ai_model="gemini-2.5-flash",
            conversation_flow_template="linkedin",
            metadata={
                "channel": CHANNEL_LINKEDIN,
                "unipile_account_id": account_id,
                "unipile_conversation_id": unipile_conversation_id,
                "lad_backend_url": LAD_BACKEND_URL,
            },
        )
        logger.warning(
            f"[LinkedInWebhook] No base WhatsApp account found for tenant {tenant_id[:8]} "
            f"— using default LinkedIn account config"
        )

    logger.info(
        f"[LinkedInWebhook] Queueing LinkedIn DM from {sender_id[:12]} "
        f"for tenant {tenant_id[:8]}, conv={unipile_conversation_id[:12] if unipile_conversation_id else 'N/A'}"
    )

    # Process in background (same pattern as personal_webhook.py)
    background_tasks.add_task(
        _process_linkedin_message,
        sender_id,
        message_text,
        sender_name,
        message_id,
        account,
    )

    return {"status": "received", "message_id": message_id}


async def _process_linkedin_message(
    sender_id: str,
    message_text: str,
    sender_name: str,
    message_id: str,
    account: WhatsAppAccount,
):
    """Background processing of an incoming LinkedIn DM."""
    try:
        await handle_incoming_message(
            phone_number=sender_id,       # LinkedIn: sender's profile ID as identifier
            message_text=message_text,
            contact_name=sender_name,
            external_message_id=message_id,
            account=account,
        )
    except Exception as e:
        logger.error(
            f"[{account.slug}] Error processing LinkedIn DM from {sender_id[:12]}: {e}",
            exc_info=True,
        )
