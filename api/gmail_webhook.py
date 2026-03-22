"""
Gmail webhook endpoint.

Receives incoming Gmail messages forwarded by LAD_backend's GmailWebhookHandler.
Constructs a synthetic WhatsAppAccount with channel='gmail' and routes through
the existing message handler pipeline (lead/conversation management + AI reply).

Endpoint:
  POST /api/gmail-webhook

Expected payload (from LAD_backend):
  {
    "account_id": "db_account_uuid",
    "tenant_id": "uuid",
    "gmail_thread_id": "thread_id",
    "gmail_message_id": "message_id",
    "from_email": "sender@example.com",
    "from_name": "Sender Name",
    "subject": "Email subject",
    "message_text": "Email body",
    "timestamp": 1234567890,
    "account_email": "youraccount@gmail.com"
  }
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import replace

from fastapi import APIRouter, Request, BackgroundTasks

from services.message_handler import handle_incoming_message
from services.account_registry import (
    get_account_by_tenant_id,
    WhatsAppAccount,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["gmail-webhook"])

LAD_BACKEND_URL = os.getenv("LAD_BACKEND_URL", "http://localhost:3004")
CHANNEL_GMAIL = "gmail"


@router.post("/api/gmail-webhook")
async def receive_gmail_message(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Receive an incoming Gmail message from LAD_backend."""
    try:
        data = await request.json()
    except Exception:
        return {"status": "error", "error": "Invalid JSON"}, 400

    account_id = data.get("account_id", "")        # DB account UUID
    tenant_id = data.get("tenant_id", "")
    gmail_thread_id = data.get("gmail_thread_id", "")
    gmail_message_id = data.get("gmail_message_id", "")
    from_email = data.get("from_email", "").lower().strip()
    from_name = data.get("from_name") or from_email
    message_text = data.get("message_text", "").strip()
    subject = data.get("subject", "")
    account_email = data.get("account_email", "")  # The Gmail account receiving the email

    if not from_email or not message_text:
        logger.warning("[GmailWebhook] Missing from_email or message_text")
        return {"status": "ignored", "reason": "missing from_email or message_text"}

    if not tenant_id:
        logger.error("[GmailWebhook] Missing tenant_id")
        return {"status": "error", "error": "tenant_id required"}

    # External message ID for dedup (use Gmail message ID)
    external_message_id = gmail_message_id or f"gmail-{gmail_thread_id[:12]}-{from_email[:8]}"

    # Build a synthetic account object for the Gmail channel.
    base_account = get_account_by_tenant_id(tenant_id)

    if base_account:
        account = replace(
            base_account,
            conversation_flow_template="generic",   # Use generic flow for Gmail
            metadata={
                **base_account.metadata,
                "channel": CHANNEL_GMAIL,
                "gmail_account_id": account_id,
                "gmail_account_email": account_email,
                "gmail_thread_id": gmail_thread_id,
                "email_subject": subject,
                "lad_backend_url": LAD_BACKEND_URL,
                # For reply routing: tenant_id and account_email are needed
                "gmail_tenant_id": tenant_id,
            },
        )
        logger.info(
            f"[GmailWebhook] Using base account '{base_account.slug}' for tenant {tenant_id[:8]}"
        )
    else:
        account = WhatsAppAccount(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            slug=f"gmail-{account_id[:8] if account_id else 'default'}",
            display_name="Gmail",
            ai_model="gemini-2.5-flash",
            conversation_flow_template="generic",
            metadata={
                "channel": CHANNEL_GMAIL,
                "gmail_account_id": account_id,
                "gmail_account_email": account_email,
                "gmail_thread_id": gmail_thread_id,
                "email_subject": subject,
                "lad_backend_url": LAD_BACKEND_URL,
                "gmail_tenant_id": tenant_id,
            },
        )
        logger.warning(
            f"[GmailWebhook] No base WhatsApp account found for tenant {tenant_id[:8]}"
        )

    logger.info(
        f"[GmailWebhook] Queueing email from {from_email[:25]} "
        f"for tenant {tenant_id[:8]}, thread={gmail_thread_id[:12] if gmail_thread_id else 'N/A'}"
    )

    background_tasks.add_task(
        _process_gmail_message,
        from_email,
        message_text,
        from_name,
        external_message_id,
        account,
    )

    return {"status": "received", "message_id": external_message_id}


async def _process_gmail_message(
    from_email: str,
    message_text: str,
    from_name: str,
    external_message_id: str,
    account: WhatsAppAccount,
):
    """Background processing of an incoming Gmail message."""
    try:
        await handle_incoming_message(
            phone_number=from_email,      # Gmail: sender's email as channel identifier
            message_text=message_text,
            contact_name=from_name,
            external_message_id=external_message_id,
            account=account,
        )
    except Exception as e:
        logger.error(
            f"[{account.slug}] Error processing Gmail message from {from_email[:25]}: {e}",
            exc_info=True,
        )
