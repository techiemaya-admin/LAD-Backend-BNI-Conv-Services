"""
WhatsApp webhook endpoints.

Supports multi-tenant account routing:
  GET  /webhook/{slug} — Facebook verification handshake (per-account token)
  POST /webhook/{slug} — Receive incoming WhatsApp messages for an account

Backward-compatible:
  GET  /webhook — uses default account
  POST /webhook — uses default account
"""
from __future__ import annotations

import logging
import os
import time

from fastapi import APIRouter, Request, BackgroundTasks, Query, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse

from services.message_handler import handle_incoming_message
from services.account_registry import (
    get_account_by_slug,
    get_default_account,
    WhatsAppAccount,
)
from db.connection import AsyncDBConnection, reload_tenant_config

logger = logging.getLogger(__name__)

# Fallback verify token for backward compat
_FALLBACK_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "BNI_Rising_Phoenix_2026")

router = APIRouter(tags=["webhook"])

_LAST_TENANT_CONFIG_REFRESH = 0.0
_TENANT_CONFIG_REFRESH_INTERVAL_SECONDS = int(os.getenv("TENANT_CONFIG_REFRESH_INTERVAL_SECONDS", "300"))


async def _maybe_reload_tenant_config() -> None:
    """Reload tenant config periodically instead of every webhook call."""
    global _LAST_TENANT_CONFIG_REFRESH
    now = time.time()
    if now - _LAST_TENANT_CONFIG_REFRESH < _TENANT_CONFIG_REFRESH_INTERVAL_SECONDS:
        return
    await reload_tenant_config()
    _LAST_TENANT_CONFIG_REFRESH = now


# ====================
# Multi-tenant webhook (primary)
# ====================

@router.get("/webhook/{chapter_slug}")
async def verify_chapter_webhook(
    chapter_slug: str,
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """Facebook webhook verification for a specific chapter."""
    chapter = get_account_by_slug(chapter_slug)
    if not chapter:
        raise HTTPException(404, f"Chapter '{chapter_slug}' not found")

    if hub_mode == "subscribe" and hub_verify_token == chapter.whatsapp_verify_token:
        logger.info(f"Webhook verified for chapter: {chapter_slug}")
        return PlainTextResponse(content=hub_challenge)

    logger.warning(f"Webhook verification failed for chapter: {chapter_slug}")
    return PlainTextResponse(content="Verification failed", status_code=403)


@router.post("/webhook/{chapter_slug}")
async def receive_chapter_webhook(
    chapter_slug: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Receive incoming WhatsApp messages for a specific chapter."""
    chapter = get_account_by_slug(chapter_slug)
    if not chapter:
        raise HTTPException(404, f"Chapter '{chapter_slug}' not found")

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "invalid payload"}, status_code=400)

    # Periodic refresh avoids heavy config DB calls for every incoming message.
    await _maybe_reload_tenant_config()
    background_tasks.add_task(process_webhook_payload, data, chapter)
    return JSONResponse({"status": "accepted"})


# ====================
# Backward-compatible webhook (defaults to first chapter)
# ====================

@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """Facebook webhook verification (backward compat — uses default chapter)."""
    # Try default chapter token first, then env var fallback
    chapter = get_default_account()
    verify_token = chapter.whatsapp_verify_token if chapter else _FALLBACK_VERIFY_TOKEN

    if hub_mode == "subscribe" and hub_verify_token == verify_token:
        logger.info("Webhook verified (default chapter)")
        return PlainTextResponse(content=hub_challenge)

    logger.warning("Webhook verification failed (default chapter)")
    return PlainTextResponse(content="Verification failed", status_code=403)


@router.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive incoming WhatsApp messages (backward compat — uses default chapter)."""
    chapter = get_default_account()
    if not chapter:
        logger.error("No default chapter configured")
        return JSONResponse({"status": "no chapter configured"}, status_code=500)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "invalid payload"}, status_code=400)

    # Periodic refresh avoids heavy config DB calls for every incoming message.
    await _maybe_reload_tenant_config()
    background_tasks.add_task(process_webhook_payload, data, chapter)
    return JSONResponse({"status": "accepted"})


# ====================
# Shared Processing
# ====================

async def process_webhook_payload(data: dict, chapter: WhatsAppAccount):
    """Background processing of webhook payload for a specific chapter."""
    try:
        if data.get("object") != "whatsapp_business_account":
            return

        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                # Handle delivery status updates
                if "statuses" in value:
                    for status in value["statuses"]:
                        await _update_message_status(status, chapter)

                # Handle incoming messages
                if "messages" in value:
                    messages = value["messages"]
                    contacts = value.get("contacts", [])

                    for msg in messages:
                        if msg.get("type") != "text":
                            continue

                        phone_number = msg.get("from", "")
                        message_text = msg.get("text", {}).get("body", "")
                        external_message_id = msg.get("id", "")
                        contact_name = ""
                        if contacts:
                            profile = contacts[0].get("profile", {})
                            contact_name = profile.get("name", "")

                        if phone_number and message_text:
                            await handle_incoming_message(
                                phone_number=phone_number,
                                message_text=message_text,
                                contact_name=contact_name,
                                external_message_id=external_message_id,
                                chapter=chapter,
                            )

    except Exception as e:
        logger.error(f"Error processing webhook payload for {chapter.slug}: {e}", exc_info=True)


async def _update_message_status(status: dict, chapter: WhatsAppAccount):
    """Update message delivery status from WhatsApp status webhook."""
    try:
        wa_message_id = status.get("id")
        wa_status = status.get("status")

        if not wa_message_id or not wa_status:
            return

        status_map = {
            "sent": "sent",
            "delivered": "delivered",
            "read": "read",
            "failed": "failed",
        }
        db_status = status_map.get(wa_status)
        if not db_status:
            return

        async with AsyncDBConnection(chapter.tenant_id) as conn:
            result = await conn.execute(
                """
                UPDATE messages SET message_status = $1
                WHERE external_message_id = $2
                  AND message_status != 'read'
                """,
                db_status,
                wa_message_id,
            )
            if "UPDATE 1" in result:
                logger.info(f"[{chapter.slug}] Message {wa_message_id} status → {db_status}")

    except Exception as e:
        logger.error(f"Error updating message status for {chapter.slug}: {e}")
