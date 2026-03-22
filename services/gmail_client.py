"""
Gmail channel client.

Sends AI-generated replies to Gmail email threads via LAD_backend,
which uses the stored OAuth token to send via Gmail API.

This service is called by message_handler._send_reply() when
account.metadata['channel'] == 'gmail'.
"""
from __future__ import annotations

import logging
import os
import httpx

logger = logging.getLogger(__name__)

LAD_BACKEND_URL = os.getenv("LAD_BACKEND_URL", "http://localhost:3004")
INTERNAL_SECRET = os.getenv("INTERNAL_SECRET", "")


async def send_reply(
    gmail_thread_id: str,
    message_text: str,
    gmail_account_email: str,
    tenant_id: str,
    subject: str | None = None,
    to_email: str | None = None,
    lad_backend_url: str | None = None,
) -> bool:
    """
    Send a reply to an existing Gmail email thread.

    Args:
        gmail_thread_id: Gmail thread ID to reply in
        message_text: Text to send
        gmail_account_email: The Gmail account email address (sender)
        tenant_id: Tenant UUID (required for account lookup in LAD_backend)
        subject: Email subject (will be prefixed with "Re: " if not already)
        to_email: Reply-to address (defaults to the original sender)
        lad_backend_url: Override LAD_backend URL (from account metadata)

    Returns:
        True if sent successfully, False otherwise
    """
    backend_url = (lad_backend_url or LAD_BACKEND_URL).rstrip("/")
    endpoint = f"{backend_url}/api/social-integration/reply/gmail"

    headers = {"Content-Type": "application/json"}
    if INTERNAL_SECRET:
        headers["X-Internal-Secret"] = INTERNAL_SECRET

    payload = {
        "thread_id": gmail_thread_id,
        "message_text": message_text,
        "account_id": gmail_account_email,  # SocialReplyController looks up by email_address
        "tenant_id": tenant_id,
    }
    if subject:
        payload["subject"] = subject
    if to_email:
        payload["to"] = to_email

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
            if response.status_code == 200:
                logger.info(
                    f"[gmail_client] Reply sent to thread {gmail_thread_id[:12]} "
                    f"for {gmail_account_email[:20]}"
                )
                return True
            else:
                logger.error(
                    f"[gmail_client] Reply failed: HTTP {response.status_code} — {response.text[:200]}"
                )
                return False
    except Exception as e:
        logger.error(f"[gmail_client] Error sending reply: {e}")
        return False
