"""
BNI Background Tasks — meeting reminders, post-meeting followups, ICP followups.

Multi-tenant: iterates over all BNI accounts and processes each tenant's DB.
Moved from tasks/reminder_task.py, tasks/followup_task.py, tasks/icp_followup_task.py.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from db.connection import AsyncDBConnection
from services import whatsapp_client
from services.account_registry import get_accounts_by_flow, WhatsAppAccount

logger = logging.getLogger(__name__)


# ── Meeting Reminders ────────────────────────────────────────────

async def send_meeting_reminders():
    """Check and send pending meeting reminders for all BNI accounts."""
    accounts = get_accounts_by_flow("bni")
    for account in accounts:
        try:
            await _send_reminders_for_tenant(account)
        except Exception as e:
            logger.error(f"[{account.slug}] Error in reminder task: {e}", exc_info=True)


async def _send_reminders_for_tenant(account: WhatsAppAccount):
    now = datetime.utcnow()
    async with AsyncDBConnection(account.tenant_id) as conn:
        reminders = await conn.fetch(
            """
            SELECT r.id, r.meeting_id, r.member_phone, r.reminder_type,
                   r.scheduled_time,
                   m.member_a_phone, m.member_a_name,
                   m.member_b_phone, m.member_b_name,
                   m.confirmed_time
            FROM meeting_reminders r
            JOIN scheduled_meetings m ON m.id = r.meeting_id
            WHERE r.sent = false
              AND m.status = 'confirmed'
              AND m.confirmed_time IS NOT NULL
            """
        )

        for r in reminders:
            confirmed_time = r["confirmed_time"]
            reminder_type = r["reminder_type"]

            should_send = False
            if reminder_type == "24h_before":
                should_send = now >= confirmed_time - timedelta(hours=24)
            elif reminder_type == "1h_before":
                should_send = now >= confirmed_time - timedelta(hours=1)

            if not should_send:
                continue

            phone = r["member_phone"]
            if phone == r["member_a_phone"]:
                other_name = r["member_b_name"] or "your fellow member"
            else:
                other_name = r["member_a_name"] or "your fellow member"

            time_str = confirmed_time.strftime("%A, %B %d at %I:%M %p")

            if reminder_type == "24h_before":
                message = (
                    f"Reminder: You have a 1-to-1 meeting tomorrow with "
                    f"{other_name} at {time_str}. Looking forward to a productive meeting!"
                )
            else:
                message = (
                    f"Reminder: Your 1-to-1 with {other_name} starts in about 1 hour "
                    f"at {time_str}. Good luck!"
                )

            await whatsapp_client.send_message(
                phone_number=phone, text=message, chapter=account
            )

            await conn.execute(
                "UPDATE meeting_reminders SET sent = true WHERE id = $1::uuid",
                str(r["id"]),
            )
            logger.info(f"[{account.slug}] Sent {reminder_type} reminder to {phone}")


# ── Post-Meeting Followups ───────────────────────────────────────

FOLLOWUP_DELAY_HOURS = 1


async def send_post_meeting_followups():
    """Check for completed meetings and initiate followup conversations for all BNI accounts."""
    accounts = get_accounts_by_flow("bni")
    for account in accounts:
        try:
            await _send_followups_for_tenant(account)
        except Exception as e:
            logger.error(f"[{account.slug}] Error in followup task: {e}", exc_info=True)


async def _send_followups_for_tenant(account: WhatsAppAccount):
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=FOLLOWUP_DELAY_HOURS)

    async with AsyncDBConnection(account.tenant_id) as conn:
        meetings = await conn.fetch(
            """
            SELECT id, member_a_phone, member_a_name,
                   member_b_phone, member_b_name, confirmed_time
            FROM scheduled_meetings
            WHERE status = 'confirmed'
              AND confirmed_time IS NOT NULL
              AND confirmed_time < $1
            """,
            cutoff,
        )

        for meeting in meetings:
            meeting_id = str(meeting["id"])
            time_str = meeting["confirmed_time"].strftime("%B %d")

            await conn.execute(
                """
                UPDATE scheduled_meetings
                SET status = 'post_meeting_followup', updated_at = NOW()
                WHERE id = $1::uuid
                """,
                meeting_id,
            )

            for phone, other_name in [
                (meeting["member_a_phone"], meeting["member_b_name"]),
                (meeting["member_b_phone"], meeting["member_a_name"]),
            ]:
                other_name = other_name or "your fellow member"

                message = (
                    f"Hi! How did your 1-to-1 meeting with {other_name} on {time_str} go? "
                    f"Could you share:\n"
                    f"1. Did the meeting happen?\n"
                    f"2. Any referrals exchanged?\n"
                    f"3. Any TYFCB (Thank You For Closed Business)?\n"
                    f"4. Any key takeaways?"
                )

                await whatsapp_client.send_message(
                    phone_number=phone, text=message, chapter=account
                )

                await conn.execute(
                    """
                    UPDATE conversation_states
                    SET context_status = 'post_meeting_followup',
                        metadata = jsonb_set(
                            COALESCE(metadata, '{}')::jsonb,
                            '{meeting_json}',
                            $1::jsonb
                        ),
                        updated_at = NOW()
                    WHERE phone = $2
                    """,
                    json.dumps({
                        "meeting_id": meeting_id,
                        "other_member_name": other_name,
                        "meeting_date": time_str,
                    }),
                    phone,
                )

            logger.info(f"[{account.slug}] Sent followup for meeting {meeting_id}")


# ── ICP Discovery Followups ──────────────────────────────────────

INCOMPLETE_ICP_STATUSES = (
    "onboarding_greeting",
    "onboarding_profile",
    "icp_discovery",
)

DEFAULT_IDLE_HOURS = 23
DEFAULT_MAX_ATTEMPTS = 3

DEFAULT_TEMPLATE_MESSAGE = (
    "Hi {member_name}! We noticed you haven't completed your profile yet. "
    "Finishing your Ideal Customer Profile helps us find the best 1-to-1 "
    "matches for you. It only takes a couple of minutes — just reply here "
    "to continue where you left off!"
)

_config_cache: dict | None = None


async def _load_config(conn) -> dict:
    """Load followup config from DB, falling back to defaults."""
    global _config_cache
    try:
        row = await conn.fetchrow(
            "SELECT config FROM followup_config WHERE config_key = 'icp_followup' LIMIT 1"
        )
        if row and row["config"]:
            cfg = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
            _config_cache = cfg
            return cfg
    except Exception as e:
        logger.debug(f"Could not load followup config: {e}")

    if _config_cache:
        return _config_cache

    return {
        "enabled": True,
        "idle_hours": DEFAULT_IDLE_HOURS,
        "max_attempts": DEFAULT_MAX_ATTEMPTS,
        "message_type": "template",
        "template_message": DEFAULT_TEMPLATE_MESSAGE,
        "custom_message": "",
    }


async def send_icp_followups():
    """Check for idle members who haven't completed ICP and send nudges for all BNI accounts."""
    accounts = get_accounts_by_flow("bni")
    for account in accounts:
        try:
            await _send_icp_followups_for_tenant(account)
        except Exception as e:
            logger.error(f"[{account.slug}] Error in ICP followup task: {e}", exc_info=True)


async def _send_icp_followups_for_tenant(account: WhatsAppAccount):
    async with AsyncDBConnection(account.tenant_id) as conn:
        config = await _load_config(conn)

        if not config.get("enabled", True):
            logger.debug(f"[{account.slug}] ICP followup task is disabled")
            return

        idle_hours = config.get("idle_hours", DEFAULT_IDLE_HOURS)
        max_attempts = config.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
        cutoff = datetime.utcnow() - timedelta(hours=idle_hours)

        members = await conn.fetch(
            """
            SELECT
                cs.id,
                cs.phone,
                cs.contact_name,
                cs.context_status,
                cs.metadata,
                cs.updated_at,
                c.id AS conversation_id,
                l.id AS lead_id
            FROM conversation_states cs
            LEFT JOIN leads l ON l.phone = cs.phone
            LEFT JOIN conversations c
                ON c.lead_id = l.id AND c.status = 'active'
            WHERE cs.context_status = ANY($1::text[])
              AND cs.updated_at < $2
            ORDER BY cs.updated_at ASC
            """,
            list(INCOMPLETE_ICP_STATUSES),
            cutoff,
        )

        sent_count = 0
        for member in members:
            metadata = member["metadata"] or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)

            icp_followup = metadata.get("icp_followup", {})
            attempt_count = icp_followup.get("attempt_count", 0)

            if attempt_count >= max_attempts:
                continue

            full_name = member["contact_name"] or "there"
            member_name = full_name.split()[0] if full_name != "there" else "there"
            message_type = config.get("message_type", "template")

            if message_type == "custom" and config.get("custom_message"):
                message = config["custom_message"].replace("{member_name}", member_name)
            else:
                template = config.get("template_message", DEFAULT_TEMPLATE_MESSAGE)
                message = template.replace("{member_name}", member_name)

            phone = member["phone"]
            conversation_id = str(member["conversation_id"]) if member["conversation_id"] else None
            lead_id = str(member["lead_id"]) if member["lead_id"] else None

            wa_id = await whatsapp_client.send_message(
                phone_number=phone,
                text=message,
                conversation_id=conversation_id,
                lead_id=lead_id,
                chapter=account,
            )

            if wa_id:
                icp_followup["attempt_count"] = attempt_count + 1
                icp_followup["last_sent_at"] = datetime.utcnow().isoformat()
                metadata["icp_followup"] = icp_followup

                await conn.execute(
                    """
                    UPDATE conversation_states
                    SET metadata = $1::jsonb, updated_at = NOW()
                    WHERE id = $2::uuid
                    """,
                    json.dumps(metadata),
                    str(member["id"]),
                )
                sent_count += 1
                logger.info(f"[{account.slug}] Sent ICP followup #{attempt_count + 1} to {phone}")

        if sent_count:
            logger.info(f"[{account.slug}] ICP followup task: sent {sent_count} nudge(s)")
