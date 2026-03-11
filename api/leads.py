from __future__ import annotations
"""
Leads API — import, list, and manage leads with multi-channel support.

Each lead can have contact info across multiple channels (WhatsApp, LinkedIn,
Instagram, Email). When imported, a conversation is auto-created for each
channel so the lead appears in the correct channel tab.
"""
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from db.connection import AsyncDBConnection
from middleware.tenant import get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/leads", tags=["leads"])


# ── Models ───────────────────────────────────────────────────────

class LeadImportItem(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    phone: str | None = None
    email: str | None = None
    company: str | None = None
    linkedin_url: str | None = None
    instagram_url: str | None = None
    source: str | None = None
    stage: str | None = None
    chat_group_ids: list[str] | None = None


class LeadImportRequest(BaseModel):
    leads: list[LeadImportItem]
    chat_group_ids: list[str] | None = None  # Apply to all leads


# Channel detection helpers
CHANNEL_FIELDS = {
    "whatsapp": "phone",
    "linkedin": "linkedin_url",
    "instagram": "instagram_url",
    "gmail": "email",
}


def _detect_channels(lead: LeadImportItem) -> list[str]:
    """Return list of channels this lead has data for."""
    channels = []
    if lead.phone:
        channels.append("whatsapp")
    if lead.linkedin_url:
        channels.append("linkedin")
    if lead.instagram_url:
        channels.append("instagram")
    if lead.email:
        channels.append("gmail")
    return channels or ["whatsapp"]  # Default to whatsapp


# ── Import leads ─────────────────────────────────────────────────

@router.post("/import")
async def import_leads(body: LeadImportRequest, tenant_id: Optional[str] = Depends(get_tenant_id)):
    """Import one or more leads, auto-creating conversations per channel."""
    if not tenant_id:
        return {"success": False, "error": "Missing X-Tenant-ID header"}

    results = {"imported": 0, "conversations_created": 0, "errors": [], "leads": []}

    try:
        async with AsyncDBConnection(tenant_id) as conn:
            for idx, lead_item in enumerate(body.leads):
                try:
                    channels = _detect_channels(lead_item)
                    primary_channel = channels[0]

                    # Build metadata with multi-channel info
                    metadata = {}
                    if lead_item.linkedin_url:
                        metadata["linkedin_url"] = lead_item.linkedin_url
                    if lead_item.instagram_url:
                        metadata["instagram_url"] = lead_item.instagram_url
                    if lead_item.source:
                        metadata["source"] = lead_item.source

                    # Check if lead already exists (by phone or email)
                    existing_lead = None
                    if lead_item.phone:
                        existing_lead = await conn.fetchrow(
                            "SELECT id FROM leads WHERE phone = $1 AND tenant_id = $2",
                            lead_item.phone, tenant_id,
                        )
                    if not existing_lead and lead_item.email:
                        existing_lead = await conn.fetchrow(
                            "SELECT id FROM leads WHERE email = $1 AND tenant_id = $2",
                            lead_item.email, tenant_id,
                        )

                    if existing_lead:
                        lead_id = str(existing_lead["id"])
                        # Update existing lead with new info
                        await conn.execute("""
                            UPDATE leads SET
                                name = COALESCE($1, name),
                                company = COALESCE($2, company),
                                email = COALESCE($3, email),
                                metadata = metadata || $4::jsonb,
                                updated_at = NOW()
                            WHERE id = $5::uuid
                        """, lead_item.name, lead_item.company, lead_item.email,
                            _json_str(metadata), lead_id)
                    else:
                        # Create new lead
                        lead_row = await conn.fetchrow("""
                            INSERT INTO leads (name, phone, email, company, channel, stage, status, metadata, tenant_id)
                            VALUES ($1, $2, $3, $4, $5, $6, 'active', $7::jsonb, $8)
                            RETURNING id
                        """, lead_item.name, lead_item.phone, lead_item.email,
                            lead_item.company, primary_channel,
                            lead_item.stage or 'new',
                            _json_str(metadata), tenant_id)
                        lead_id = str(lead_row["id"])

                    results["imported"] += 1

                    # Create conversations per channel
                    created_conv_ids = []
                    for channel in channels:
                        # Check if conversation already exists for this lead + channel
                        existing_conv = await conn.fetchrow("""
                            SELECT id FROM conversations
                            WHERE lead_id = $1::uuid AND channel = $2
                              AND (is_deleted IS NULL OR is_deleted = false)
                        """, lead_id, channel)

                        if existing_conv:
                            created_conv_ids.append(str(existing_conv["id"]))
                            continue

                        conv_row = await conn.fetchrow("""
                            INSERT INTO conversations (lead_id, channel, status, owner, tenant_id)
                            VALUES ($1::uuid, $2, 'active', 'AI', $3)
                            RETURNING id
                        """, lead_id, channel, tenant_id)
                        conv_id = str(conv_row["id"])
                        created_conv_ids.append(conv_id)
                        results["conversations_created"] += 1

                    # Assign to chat groups
                    group_ids = lead_item.chat_group_ids or body.chat_group_ids or []
                    for group_id in group_ids:
                        for conv_id in created_conv_ids:
                            await conn.execute("""
                                INSERT INTO chat_group_conversations (group_id, conversation_id)
                                VALUES ($1::uuid, $2::uuid)
                                ON CONFLICT DO NOTHING
                            """, group_id, conv_id)

                    # Create conversation_states entry if phone provided
                    if lead_item.phone:
                        await conn.execute("""
                            INSERT INTO conversation_states (lead_id, phone, contact_name, context_status, tenant_id)
                            VALUES ($1::uuid, $2, $3, 'greeting', $4)
                            ON CONFLICT (phone) DO UPDATE SET
                                contact_name = COALESCE(EXCLUDED.contact_name, conversation_states.contact_name),
                                updated_at = NOW()
                        """, lead_id, lead_item.phone, lead_item.name, tenant_id)

                    results["leads"].append({
                        "lead_id": lead_id,
                        "name": lead_item.name,
                        "channels": channels,
                        "conversation_ids": created_conv_ids,
                    })

                except Exception as e:
                    logger.error(f"Error importing lead #{idx} ({lead_item.name}): {e}")
                    results["errors"].append({"index": idx, "name": lead_item.name, "error": str(e)})

        return {"success": True, "data": results}

    except Exception as e:
        logger.error(f"Error in lead import: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ── List leads ───────────────────────────────────────────────────

@router.get("")
async def list_leads(
    search: str = Query(None),
    channel: str = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    tenant_id: Optional[str] = Depends(get_tenant_id),
):
    """List leads with optional search and channel filter."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            where = ["1=1"]
            params = []
            idx = 1

            if search:
                where.append(f"(l.name ILIKE ${idx} OR l.phone ILIKE ${idx} OR l.email ILIKE ${idx})")
                params.append(f"%{search}%")
                idx += 1

            if channel:
                where.append(f"l.channel = ${idx}")
                params.append(channel)
                idx += 1

            params.extend([limit, offset])

            rows = await conn.fetch(f"""
                SELECT l.id, l.name, l.phone, l.email, l.company, l.channel,
                       l.stage, l.status, l.metadata, l.created_at,
                       COUNT(DISTINCT c.id) AS conversation_count
                FROM leads l
                LEFT JOIN conversations c ON c.lead_id = l.id
                    AND (c.is_deleted IS NULL OR c.is_deleted = false)
                WHERE {' AND '.join(where)}
                GROUP BY l.id
                ORDER BY l.created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}
            """, *params)

            data = []
            for r in rows:
                meta = r["metadata"] if isinstance(r["metadata"], dict) else {}
                data.append({
                    "id": str(r["id"]),
                    "name": r["name"],
                    "phone": r["phone"],
                    "email": r["email"],
                    "company": r["company"],
                    "channel": r["channel"],
                    "stage": r["stage"],
                    "status": r["status"],
                    "linkedin_url": meta.get("linkedin_url"),
                    "instagram_url": meta.get("instagram_url"),
                    "conversation_count": r["conversation_count"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                })

            return {"success": True, "data": data}

    except Exception as e:
        logger.error(f"Error listing leads: {e}", exc_info=True)
        return {"success": False, "data": [], "error": str(e)}


def _json_str(obj: dict) -> str:
    import json
    return json.dumps(obj)
