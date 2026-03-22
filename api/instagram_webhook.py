"""
Instagram DM webhook endpoint.

Receives incoming Instagram DMs forwarded by LAD_backend's InstagramWebhookHandler.
Constructs a synthetic WhatsAppAccount with channel='instagram' and routes through
the existing message handler pipeline (lead/conversation management + AI reply).

Endpoint:
  POST /api/instagram-webhook

Expected payload (from LAD_backend):
  {
    "account_id": "unipile_account_id",
    "tenant_id": "uuid",
    "unipile_conversation_id": "instagram_conv_id",
    "sender_id": "instagram_profile_id",
    "sender_name": "Jane Doe",
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

router = APIRouter(tags=["instagram-webhook"])

LAD_BACKEND_URL = os.getenv("LAD_BACKEND_URL", "http://localhost:3004")
CHANNEL_INSTAGRAM = "instagram"


@router.post("/api/instagram-webhook")
async def receive_instagram_dm(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Receive an incoming Instagram DM from LAD_backend."""
    try:
        data = await request.json()
    except Exception:
        return {"status": "error", "error": "Invalid JSON"}, 400

    account_id = data.get("account_id", "")
    tenant_id = data.get("tenant_id", "")
    unipile_conversation_id = data.get("unipile_conversation_id", "")
    sender_id = data.get("sender_id", "")
    sender_name = data.get("sender_name") or "Instagram User"
    message_text = data.get("message_text", "").strip()
    message_id = data.get("message_id") or f"ig-{account_id[:8]}-{sender_id[:8]}-{int(time.time())}"

    if not sender_id or not message_text:
        logger.warning("[InstagramWebhook] Missing sender_id or message_text")
        return {"status": "ignored", "reason": "missing sender_id or message_text"}

    if not tenant_id:
        logger.error("[InstagramWebhook] Missing tenant_id — cannot route message")
        return {"status": "error", "error": "tenant_id required"}

    # Build a synthetic account object for the Instagram channel.
    base_account = get_account_by_tenant_id(tenant_id)

    if base_account:
        account = replace(
            base_account,
            conversation_flow_template="linkedin",  # Reuse LinkedIn professional flow for Instagram
            metadata={
                **base_account.metadata,
                "channel": CHANNEL_INSTAGRAM,
                "unipile_account_id": account_id,
                "unipile_conversation_id": unipile_conversation_id,
                "lad_backend_url": LAD_BACKEND_URL,
            },
        )
        logger.info(
            f"[InstagramWebhook] Using base account '{base_account.slug}' for tenant {tenant_id[:8]}"
        )
    else:
        account = WhatsAppAccount(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            slug=f"instagram-{account_id[:8] if account_id else 'default'}",
            display_name="Instagram",
            ai_model="gemini-2.5-flash",
            conversation_flow_template="linkedin",
            metadata={
                "channel": CHANNEL_INSTAGRAM,
                "unipile_account_id": account_id,
                "unipile_conversation_id": unipile_conversation_id,
                "lad_backend_url": LAD_BACKEND_URL,
            },
        )
        logger.warning(
            f"[InstagramWebhook] No base WhatsApp account found for tenant {tenant_id[:8]}"
        )

    logger.info(
        f"[InstagramWebhook] Queueing Instagram DM from {sender_id[:12]} "
        f"for tenant {tenant_id[:8]}"
    )

    background_tasks.add_task(
        _process_instagram_message,
        sender_id,
        message_text,
        sender_name,
        message_id,
        account,
    )

    return {"status": "received", "message_id": message_id}


async def _process_instagram_message(
    sender_id: str,
    message_text: str,
    sender_name: str,
    message_id: str,
    account: WhatsAppAccount,
):
    """Background processing of an incoming Instagram DM."""
    try:
        await handle_incoming_message(
            phone_number=sender_id,       # Instagram: sender's profile ID as identifier
            message_text=message_text,
            contact_name=sender_name,
            external_message_id=message_id,
            account=account,
        )
    except Exception as e:
        logger.error(
            f"[{account.slug}] Error processing Instagram DM from {sender_id[:12]}: {e}",
            exc_info=True,
        )
