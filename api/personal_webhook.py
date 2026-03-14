"""
Personal WhatsApp webhook — receives messages from LAD_backend's Baileys bridge.

Endpoints:
  POST /webhook/personal-whatsapp — Receive normalized personal WhatsApp messages
  GET  /api/personal-whatsapp/auto-assign — Get auto-assign config
  PUT  /api/personal-whatsapp/auto-assign — Update auto-assign config
  POST /api/personal-whatsapp/contacts/sync — Bulk upsert synced contacts
  GET  /api/personal-whatsapp/contacts — List synced contacts

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

import json
import logging
from dataclasses import replace
from typing import List, Optional

from fastapi import APIRouter, Request, BackgroundTasks, HTTPException, Depends
from pydantic import BaseModel

from db.connection import AsyncDBConnection
from services.message_handler import handle_incoming_message
from services.account_registry import (
    get_account_by_tenant_and_channel,
    CHANNEL_PERSONAL,
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

    # Resolve tenant's personal WhatsApp account config.
    # Use channel-aware lookup so we get the personal-specific account
    # even when the tenant also has a business account.
    account = None
    if tenant_id:
        account = get_account_by_tenant_and_channel(tenant_id, CHANNEL_PERSONAL)
    else:
        logger.warning("No X-Tenant-ID header provided in personal webhook request")

    if not account:
        error_msg = f"No WhatsApp account configured for tenant: {tenant_id or 'unknown'}"
        logger.error(f"[personal_webhook] {error_msg}")
        raise HTTPException(500, error_msg)

    # Ensure personal channel metadata is set on the account.
    # If the account was already configured as personal in the DB, its metadata
    # already has channel=personal_whatsapp. We still inject the runtime fields
    # (personal_account_id, lad_backend_url) from the incoming request.
    personal_metadata = {
        **account.metadata,
        "channel": "personal_whatsapp",
        "personal_account_id": personal_account_id,
        "lad_backend_url": data.get("lad_backend_url", ""),
    }
    personal_account = replace(account, metadata=personal_metadata)

    contact_name = data.get("contact_name", "")
    is_saved_contact = data.get("is_saved_contact", False)

    # Process in background (same as business WhatsApp webhook)
    background_tasks.add_task(
        _process_personal_message,
        contact_phone,
        text,
        contact_name,
        external_message_id,
        personal_account,
        is_saved_contact,
    )

    return {"status": "received", "message_id": external_message_id}


async def _process_personal_message(
    phone_number: str,
    message_text: str,
    contact_name: str,
    external_message_id: str,
    account: WhatsAppAccount,
    is_saved_contact: bool = False,
):
    """Background processing of a personal WhatsApp message."""
    try:
        await handle_incoming_message(
            phone_number=phone_number,
            message_text=message_text,
            contact_name=contact_name,
            external_message_id=external_message_id,
            chapter=account,
            is_saved_contact=is_saved_contact,
        )
    except Exception as e:
        logger.error(
            f"[{account.slug}] Error processing personal WA message from {phone_number}: {e}",
            exc_info=True,
        )


# ── Auto-assign settings ─────────────────────────────────────────

AUTO_ASSIGN_CONFIG_KEY = "auto_assign_contacts"
AUTO_ASSIGN_DEFAULTS = {
    "enabled": False,
    "saved_contacts_to": "human_agent",
    "unsaved_contacts_to": "AI",
}


class AutoAssignConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    saved_contacts_to: Optional[str] = None   # "human_agent" or "AI"
    unsaved_contacts_to: Optional[str] = None  # "AI" or "human_agent"


@router.get("/api/personal-whatsapp/auto-assign")
async def get_auto_assign_config(
    tenant_id: Optional[str] = Depends(get_tenant_id),
):
    """Get auto-assign configuration for personal WhatsApp contacts."""
    if not tenant_id:
        raise HTTPException(401, "Tenant context required")

    try:
        async with AsyncDBConnection(tenant_id) as conn:
            row = await conn.fetchrow(
                "SELECT config FROM followup_config WHERE config_key = $1 AND tenant_id = $2::uuid",
                AUTO_ASSIGN_CONFIG_KEY,
                tenant_id,
            )

            if row:
                cfg = row["config"]
                config = json.loads(cfg) if isinstance(cfg, str) else cfg
            else:
                config = dict(AUTO_ASSIGN_DEFAULTS)

            return {"success": True, "data": config}
    except Exception as e:
        logger.error(f"Error fetching auto-assign config: {e}", exc_info=True)
        raise HTTPException(500, "Failed to fetch auto-assign config")


@router.put("/api/personal-whatsapp/auto-assign")
async def update_auto_assign_config(
    body: AutoAssignConfigUpdate,
    tenant_id: Optional[str] = Depends(get_tenant_id),
):
    """Update auto-assign configuration for personal WhatsApp contacts."""
    if not tenant_id:
        raise HTTPException(401, "Tenant context required")

    # Validate owner values
    valid_owners = {"AI", "human_agent"}
    if body.saved_contacts_to and body.saved_contacts_to not in valid_owners:
        raise HTTPException(400, "saved_contacts_to must be 'AI' or 'human_agent'")
    if body.unsaved_contacts_to and body.unsaved_contacts_to not in valid_owners:
        raise HTTPException(400, "unsaved_contacts_to must be 'AI' or 'human_agent'")

    try:
        async with AsyncDBConnection(tenant_id) as conn:
            # Get current config or defaults
            row = await conn.fetchrow(
                """
                SELECT config FROM followup_config
                WHERE config_key = $1 AND tenant_id = $2::uuid
                """,
                AUTO_ASSIGN_CONFIG_KEY,
                tenant_id,
            )

            if row:
                current = row["config"]
                current = json.loads(current) if isinstance(current, str) else current
            else:
                current = dict(AUTO_ASSIGN_DEFAULTS)

            # Merge updates
            updates = body.model_dump(exclude_none=True)
            current.update(updates)

            # Upsert
            await conn.execute(
                """
                INSERT INTO followup_config (config_key, config, tenant_id, updated_at)
                VALUES ($1, $2::jsonb, $3::uuid, NOW())
                ON CONFLICT (config_key, tenant_id)
                DO UPDATE SET config = $2::jsonb, updated_at = NOW()
                """,
                AUTO_ASSIGN_CONFIG_KEY,
                json.dumps(current),
                tenant_id,
            )

            logger.info(f"[personal_webhook] Auto-assign config updated for tenant {tenant_id}: {current}")
            return {"success": True, "data": current}
    except Exception as e:
        logger.error(f"Error updating auto-assign config: {e}", exc_info=True)
        raise HTTPException(500, "Failed to update auto-assign config")


# ── Synced contacts ──────────────────────────────────────────────

class ContactItem(BaseModel):
    phone: str
    name: Optional[str] = None
    whatsapp_id: Optional[str] = None  # raw JID from Baileys


class ContactsSyncRequest(BaseModel):
    contacts: List[ContactItem]


@router.post("/api/personal-whatsapp/contacts/sync")
async def sync_contacts(
    body: ContactsSyncRequest,
    tenant_id: Optional[str] = Depends(get_tenant_id),
):
    """Bulk upsert contacts synced from the connected WhatsApp device."""
    if not tenant_id:
        raise HTTPException(401, "Tenant context required")

    if not body.contacts:
        return {"success": True, "synced": 0}

    try:
        async with AsyncDBConnection(tenant_id) as conn:
            # Create table if not exists
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS whatsapp_contacts (
                    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                    tenant_id UUID NOT NULL,
                    phone TEXT NOT NULL,
                    name TEXT,
                    whatsapp_id TEXT,
                    synced_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE (tenant_id, phone)
                )
                """
            )

            synced = 0
            for c in body.contacts:
                await conn.execute(
                    """
                    INSERT INTO whatsapp_contacts (tenant_id, phone, name, whatsapp_id, synced_at)
                    VALUES ($1::uuid, $2, $3, $4, NOW())
                    ON CONFLICT (tenant_id, phone)
                    DO UPDATE SET name = EXCLUDED.name, whatsapp_id = EXCLUDED.whatsapp_id, synced_at = NOW()
                    """,
                    tenant_id,
                    c.phone,
                    c.name,
                    c.whatsapp_id,
                )
                synced += 1

            logger.info(f"[personal_webhook] Synced {synced} contacts for tenant {tenant_id}")
            return {"success": True, "synced": synced}
    except Exception as e:
        logger.error(f"Error syncing contacts: {e}", exc_info=True)
        raise HTTPException(500, "Failed to sync contacts")


@router.get("/api/personal-whatsapp/contacts")
async def list_contacts(
    tenant_id: Optional[str] = Depends(get_tenant_id),
):
    """List all synced WhatsApp contacts for the tenant."""
    if not tenant_id:
        raise HTTPException(401, "Tenant context required")

    try:
        async with AsyncDBConnection(tenant_id) as conn:
            # Check if table exists
            table_exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'whatsapp_contacts'
                )
                """
            )
            if not table_exists:
                return {"success": True, "data": [], "total": 0}

            rows = await conn.fetch(
                """
                SELECT phone, name, whatsapp_id, synced_at
                FROM whatsapp_contacts
                WHERE tenant_id = $1::uuid
                ORDER BY name ASC NULLS LAST, phone ASC
                """,
                tenant_id,
            )

            contacts = [
                {
                    "phone": r["phone"],
                    "name": r["name"],
                    "whatsapp_id": r["whatsapp_id"],
                    "synced_at": r["synced_at"].isoformat() if r["synced_at"] else None,
                    "is_saved": bool(r["name"]),
                }
                for r in rows
            ]

            return {"success": True, "data": contacts, "total": len(contacts)}
    except Exception as e:
        logger.error(f"Error listing contacts: {e}", exc_info=True)
        raise HTTPException(500, "Failed to list contacts")
