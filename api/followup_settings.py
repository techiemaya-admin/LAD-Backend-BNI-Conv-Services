"""
Followup settings API — manage ICP followup configuration and view status.

Endpoints:
  GET  /api/followup-settings          — get current config
  PUT  /api/followup-settings          — update config
  GET  /api/followup-settings/status   — get idle member stats
  POST /api/followup-settings/trigger  — manually trigger followup for a member
"""
import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from db.connection import ClientDBConnection
from services import whatsapp_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/followup-settings", tags=["followup-settings"])

INCOMPLETE_ICP_STATUSES = (
    "onboarding_greeting",
    "onboarding_profile",
    "icp_discovery",
)

DEFAULT_CONFIG = {
    "enabled": True,
    "idle_hours": 23,
    "interval_minutes": 60,
    "max_attempts": 3,
    "message_type": "template",
    "template_message": (
        "Hi {member_name}! We noticed you haven't completed your profile yet. "
        "Finishing your Ideal Customer Profile helps us find the best 1-to-1 "
        "matches for you. It only takes a couple of minutes — just reply here "
        "to continue where you left off!"
    ),
    "custom_message": "",
}


# ── Pydantic models ──────────────────────────────────────────────

class FollowupConfigUpdate(BaseModel):
    enabled: bool | None = None
    idle_hours: int | None = Field(None, ge=1, le=168)
    interval_minutes: int | None = Field(None, ge=5, le=1440)
    max_attempts: int | None = Field(None, ge=1, le=10)
    message_type: str | None = Field(None, pattern=r"^(template|custom)$")
    template_message: str | None = None
    custom_message: str | None = None


class ManualTriggerRequest(BaseModel):
    member_phone: str
    message: str | None = None


class TemplateSendRequest(BaseModel):
    template_name: str
    language_code: str = "en_GB"
    parameters: list[str] | None = None
    member_phones: list[str]  # list of phones, or ["all"] to send to everyone


# ── Helpers ───────────────────────────────────────────────────────

async def _ensure_config_table(conn):
    """Create followup_config table if it doesn't exist."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS followup_config (
            id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
            config_key TEXT UNIQUE NOT NULL,
            config JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )


async def _get_config(conn) -> dict:
    """Load config from DB, creating defaults if needed."""
    await _ensure_config_table(conn)
    row = await conn.fetchrow(
        "SELECT config FROM followup_config WHERE config_key = 'icp_followup'"
    )
    if row:
        cfg = row["config"]
        return json.loads(cfg) if isinstance(cfg, str) else cfg

    # Insert defaults
    await conn.execute(
        """
        INSERT INTO followup_config (config_key, config)
        VALUES ('icp_followup', $1::jsonb)
        ON CONFLICT (config_key) DO NOTHING
        """,
        json.dumps(DEFAULT_CONFIG),
    )
    return dict(DEFAULT_CONFIG)


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("")
async def get_followup_config():
    """Return current ICP followup configuration."""
    try:
        async with ClientDBConnection() as conn:
            config = await _get_config(conn)
            return {"success": True, "data": config}
    except Exception as e:
        logger.error(f"Error fetching followup config: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.put("")
async def update_followup_config(body: FollowupConfigUpdate):
    """Update ICP followup configuration (partial update)."""
    try:
        async with ClientDBConnection() as conn:
            current = await _get_config(conn)

            # Merge only provided fields
            updates = body.model_dump(exclude_none=True)
            current.update(updates)

            await conn.execute(
                """
                UPDATE followup_config
                SET config = $1::jsonb, updated_at = NOW()
                WHERE config_key = 'icp_followup'
                """,
                json.dumps(current),
            )

            return {"success": True, "data": current}
    except Exception as e:
        logger.error(f"Error updating followup config: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/status")
async def get_followup_status():
    """Get stats on idle members eligible for ICP followup."""
    try:
        async with ClientDBConnection() as conn:
            config = await _get_config(conn)
            idle_hours = config.get("idle_hours", 23)
            max_attempts = config.get("max_attempts", 3)
            cutoff = datetime.utcnow() - timedelta(hours=idle_hours)

            # Get all incomplete ICP members
            members = await conn.fetch(
                """
                SELECT
                    bcm.member_phone,
                    bcm.member_name,
                    bcm.context_status,
                    bcm.metadata,
                    bcm.updated_at
                FROM bni_conversation_manager bcm
                WHERE bcm.context_status = ANY($1::text[])
                ORDER BY bcm.updated_at DESC
                """,
                list(INCOMPLETE_ICP_STATUSES),
            )

            idle_members = []
            total_incomplete = len(members)

            for m in members:
                metadata = m["metadata"] or {}
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)

                icp_followup = metadata.get("icp_followup", {})
                attempt_count = icp_followup.get("attempt_count", 0)
                last_sent = icp_followup.get("last_sent_at")
                is_idle = m["updated_at"] < cutoff if m["updated_at"] else False

                idle_members.append({
                    "member_phone": m["member_phone"],
                    "member_name": m["member_name"],
                    "context_status": m["context_status"],
                    "last_activity": m["updated_at"].isoformat() if m["updated_at"] else None,
                    "idle": is_idle,
                    "followup_attempts": attempt_count,
                    "last_followup_sent": last_sent,
                    "max_attempts_reached": attempt_count >= max_attempts,
                })

            eligible = [m for m in idle_members if m["idle"] and not m["max_attempts_reached"]]

            return {
                "success": True,
                "data": {
                    "config": config,
                    "total_incomplete_icp": total_incomplete,
                    "total_idle": len([m for m in idle_members if m["idle"]]),
                    "eligible_for_followup": len(eligible),
                    "members": idle_members,
                },
            }
    except Exception as e:
        logger.error(f"Error fetching followup status: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/trigger")
async def trigger_manual_followup(body: ManualTriggerRequest):
    """Manually send a followup message to a specific member."""
    try:
        async with ClientDBConnection() as conn:
            config = await _get_config(conn)

            member = await conn.fetchrow(
                """
                SELECT bcm.id, bcm.member_phone, bcm.member_name,
                       bcm.context_status, bcm.metadata,
                       c.id AS conversation_id, l.id AS lead_id
                FROM bni_conversation_manager bcm
                LEFT JOIN leads l ON l.phone = bcm.member_phone
                LEFT JOIN conversations c
                    ON c.lead_id = l.id AND c.status = 'active'
                WHERE bcm.member_phone = $1
                LIMIT 1
                """,
                body.member_phone,
            )

            if not member:
                raise HTTPException(status_code=404, detail="Member not found")

            full_name = member["member_name"] or "there"
            member_name = full_name.split()[0] if full_name != "there" else "there"

            # Use provided message or fall back to config
            if body.message:
                message = body.message.replace("{member_name}", member_name)
            elif config.get("message_type") == "custom" and config.get("custom_message"):
                message = config["custom_message"].replace("{member_name}", member_name)
            else:
                template = config.get("template_message", DEFAULT_CONFIG["template_message"])
                message = template.replace("{member_name}", member_name)

            conversation_id = str(member["conversation_id"]) if member["conversation_id"] else None
            lead_id = str(member["lead_id"]) if member["lead_id"] else None

            wa_id = await whatsapp_client.send_message(
                phone_number=body.member_phone,
                text=message,
                conversation_id=conversation_id,
                lead_id=lead_id,
            )

            if not wa_id:
                return {"success": False, "error": "Failed to send WhatsApp message"}

            # Update metadata
            metadata = member["metadata"] or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)

            icp_followup = metadata.get("icp_followup", {})
            icp_followup["attempt_count"] = icp_followup.get("attempt_count", 0) + 1
            icp_followup["last_sent_at"] = datetime.utcnow().isoformat()
            icp_followup["last_manual"] = True
            metadata["icp_followup"] = icp_followup

            await conn.execute(
                """
                UPDATE bni_conversation_manager
                SET metadata = $1::jsonb, updated_at = NOW()
                WHERE id = $2::uuid
                """,
                json.dumps(metadata),
                str(member["id"]),
            )

            return {
                "success": True,
                "data": {
                    "member_phone": body.member_phone,
                    "message_sent": message,
                    "whatsapp_id": wa_id,
                    "attempt_count": icp_followup["attempt_count"],
                },
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in manual followup trigger: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/templates")
async def list_whatsapp_templates():
    """Fetch approved WhatsApp message templates from Meta API."""
    try:
        templates = await whatsapp_client.get_message_templates()
        return {"success": True, "data": templates}
    except Exception as e:
        logger.error(f"Error listing templates: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/send-template")
async def send_template_to_members(body: TemplateSendRequest):
    """Send a WhatsApp template message to selected members (or all idle members)."""
    try:
        async with ClientDBConnection() as conn:
            # Resolve target members
            if body.member_phones == ["all"]:
                config = await _get_config(conn)
                idle_hours = config.get("idle_hours", 23)
                cutoff = datetime.utcnow() - timedelta(hours=idle_hours)

                rows = await conn.fetch(
                    """
                    SELECT bcm.member_phone, bcm.member_name,
                           c.id AS conversation_id, l.id AS lead_id
                    FROM bni_conversation_manager bcm
                    LEFT JOIN leads l ON l.phone = bcm.member_phone
                    LEFT JOIN conversations c
                        ON c.lead_id = l.id AND c.status = 'active'
                    WHERE bcm.context_status = ANY($1::text[])
                      AND bcm.updated_at < $2
                    """,
                    list(INCOMPLETE_ICP_STATUSES),
                    cutoff,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT bcm.member_phone, bcm.member_name,
                           c.id AS conversation_id, l.id AS lead_id
                    FROM bni_conversation_manager bcm
                    LEFT JOIN leads l ON l.phone = bcm.member_phone
                    LEFT JOIN conversations c
                        ON c.lead_id = l.id AND c.status = 'active'
                    WHERE bcm.member_phone = ANY($1::text[])
                    """,
                    body.member_phones,
                )

            sent = []
            failed = []

            for row in rows:
                phone = row["member_phone"]
                # Replace {member_name} in parameters with first name
                full_name = row["member_name"] or "there"
                first_name = full_name.split()[0] if full_name != "there" else "there"
                params = None
                if body.parameters:
                    params = [
                        p.replace("{member_name}", first_name)
                         .replace("{first_name}", first_name)
                        for p in body.parameters
                    ]

                conversation_id = str(row["conversation_id"]) if row["conversation_id"] else None
                lead_id = str(row["lead_id"]) if row["lead_id"] else None

                wa_id = await whatsapp_client.send_template_message(
                    phone_number=phone,
                    template_name=body.template_name,
                    language_code=body.language_code,
                    parameters=params,
                    conversation_id=conversation_id,
                    lead_id=lead_id,
                )

                if wa_id:
                    sent.append(phone)
                else:
                    failed.append(phone)

            return {
                "success": True,
                "data": {
                    "template_name": body.template_name,
                    "total_targeted": len(rows),
                    "sent_count": len(sent),
                    "failed_count": len(failed),
                    "sent": sent,
                    "failed": failed,
                },
            }
    except Exception as e:
        logger.error(f"Error sending template to members: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
