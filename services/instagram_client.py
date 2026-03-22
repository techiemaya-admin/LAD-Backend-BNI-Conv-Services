"""
Instagram channel client.

Sends AI-generated replies to Instagram DM conversations via LAD_backend,
which forwards to Unipile API: POST /api/v1/chats/{conversation_id}/messages

This service is called by message_handler._send_reply() when
account.metadata['channel'] == 'instagram'.
"""
from __future__ import annotations

import logging
import os
import httpx

logger = logging.getLogger(__name__)

LAD_BACKEND_URL = os.getenv("LAD_BACKEND_URL", "http://localhost:3004")
INTERNAL_SECRET = os.getenv("INTERNAL_SECRET", "")


async def send_message(
    unipile_conversation_id: str,
    message_text: str,
    unipile_account_id: str,
    lad_backend_url: str | None = None,
) -> bool:
    """
    Send a reply to an existing Instagram DM conversation.

    Args:
        unipile_conversation_id: Unipile's conversation/chat ID
        message_text: Text to send
        unipile_account_id: Unipile account ID owning the Instagram account
        lad_backend_url: Override LAD_backend URL (from account metadata)

    Returns:
        True if sent successfully, False otherwise
    """
    backend_url = (lad_backend_url or LAD_BACKEND_URL).rstrip("/")
    endpoint = f"{backend_url}/api/social-integration/reply/instagram"

    headers = {"Content-Type": "application/json"}
    if INTERNAL_SECRET:
        headers["X-Internal-Secret"] = INTERNAL_SECRET

    payload = {
        "conversation_id": unipile_conversation_id,
        "message_text": message_text,
        "account_id": unipile_account_id,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
            if response.status_code == 200:
                logger.info(
                    f"[instagram_client] Reply sent to conversation {unipile_conversation_id[:12]}"
                )
                return True
            else:
                logger.error(
                    f"[instagram_client] Reply failed: HTTP {response.status_code} — {response.text[:200]}"
                )
                return False
    except Exception as e:
        logger.error(f"[instagram_client] Error sending reply: {e}")
        return False
