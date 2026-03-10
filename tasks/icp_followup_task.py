"""
ICP discovery followup task — runs on a configurable interval via APScheduler.

Checks for members who started onboarding but have not completed ICP Discovery
and have been idle for a configurable number of hours (default 23).
Sends a nudge message via WhatsApp to re-engage them.
"""
import json
import logging
from datetime import datetime, timedelta

from db.connection import ClientDBConnection
from services import whatsapp_client

logger = logging.getLogger(__name__)

# Statuses that indicate ICP discovery is not yet complete
INCOMPLETE_ICP_STATUSES = (
    "onboarding_greeting",
    "onboarding_profile",
    "icp_discovery",
)

DEFAULT_IDLE_HOURS = 23
DEFAULT_INTERVAL_MINUTES = 60
DEFAULT_MAX_ATTEMPTS = 3

DEFAULT_TEMPLATE_MESSAGE = (
    "Hi {member_name}! We noticed you haven't completed your profile yet. "
    "Finishing your Ideal Customer Profile helps us find the best 1-to-1 "
    "matches for you. It only takes a couple of minutes — just reply here "
    "to continue where you left off!"
)

# In-memory config cache (loaded from DB on each run)
_config_cache: dict | None = None


async def _load_config(conn) -> dict:
    """Load followup config from DB, falling back to defaults."""
    global _config_cache
    try:
        row = await conn.fetchrow(
            """
            SELECT config FROM followup_config
            WHERE config_key = 'icp_followup'
            LIMIT 1
            """
        )
        if row and row["config"]:
            cfg = json.loads(row["config"]) if isinstance(row["config"], str) else row["config"]
            _config_cache = cfg
            return cfg
    except Exception as e:
        # Table might not exist yet — use defaults
        logger.debug(f"Could not load followup config: {e}")

    if _config_cache:
        return _config_cache

    return {
        "enabled": True,
        "idle_hours": DEFAULT_IDLE_HOURS,
        "interval_minutes": DEFAULT_INTERVAL_MINUTES,
        "max_attempts": DEFAULT_MAX_ATTEMPTS,
        "message_type": "template",  # "template" or "custom"
        "template_message": DEFAULT_TEMPLATE_MESSAGE,
        "custom_message": "",
    }


async def send_icp_followups():
    """Check for idle members who haven't completed ICP and send nudge messages."""
    try:
        async with ClientDBConnection() as conn:
            config = await _load_config(conn)

            if not config.get("enabled", True):
                logger.debug("ICP followup task is disabled")
                return

            idle_hours = config.get("idle_hours", DEFAULT_IDLE_HOURS)
            max_attempts = config.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
            cutoff = datetime.utcnow() - timedelta(hours=idle_hours)

            # Find members with incomplete ICP who have been idle
            members = await conn.fetch(
                """
                SELECT
                    bcm.id,
                    bcm.member_phone,
                    bcm.member_name,
                    bcm.context_status,
                    bcm.metadata,
                    bcm.updated_at,
                    c.id AS conversation_id,
                    l.id AS lead_id
                FROM bni_conversation_manager bcm
                LEFT JOIN conversations c
                    ON c.lead_id = (
                        SELECT id FROM leads WHERE phone = bcm.member_phone LIMIT 1
                    )
                    AND c.status = 'active'
                LEFT JOIN leads l ON l.phone = bcm.member_phone
                WHERE bcm.context_status = ANY($1::text[])
                  AND bcm.updated_at < $2
                ORDER BY bcm.updated_at ASC
                """,
                list(INCOMPLETE_ICP_STATUSES),
                cutoff,
            )

            sent_count = 0
            for member in members:
                metadata = member["metadata"] or {}
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)

                # Check followup attempt count
                icp_followup = metadata.get("icp_followup", {})
                attempt_count = icp_followup.get("attempt_count", 0)

                if attempt_count >= max_attempts:
                    logger.debug(
                        f"Skipping {member['member_phone']}: "
                        f"max attempts ({max_attempts}) reached"
                    )
                    continue

                # Build the message — use first name only
                full_name = member["member_name"] or "there"
                member_name = full_name.split()[0] if full_name != "there" else "there"
                message_type = config.get("message_type", "template")

                if message_type == "custom" and config.get("custom_message"):
                    message = config["custom_message"].replace(
                        "{member_name}", member_name
                    )
                else:
                    template = config.get("template_message", DEFAULT_TEMPLATE_MESSAGE)
                    message = template.replace("{member_name}", member_name)

                # Send the nudge
                phone = member["member_phone"]
                conversation_id = str(member["conversation_id"]) if member["conversation_id"] else None
                lead_id = str(member["lead_id"]) if member["lead_id"] else None

                wa_id = await whatsapp_client.send_message(
                    phone_number=phone,
                    text=message,
                    conversation_id=conversation_id,
                    lead_id=lead_id,
                )

                if wa_id:
                    # Update metadata with followup tracking
                    icp_followup["attempt_count"] = attempt_count + 1
                    icp_followup["last_sent_at"] = datetime.utcnow().isoformat()
                    metadata["icp_followup"] = icp_followup

                    await conn.execute(
                        """
                        UPDATE bni_conversation_manager
                        SET metadata = $1::jsonb,
                            updated_at = NOW()
                        WHERE id = $2::uuid
                        """,
                        json.dumps(metadata),
                        str(member["id"]),
                    )
                    sent_count += 1
                    logger.info(
                        f"Sent ICP followup #{attempt_count + 1} to {phone}"
                    )

            if sent_count:
                logger.info(f"ICP followup task: sent {sent_count} nudge(s)")

    except Exception as e:
        logger.error(f"Error in ICP followup task: {e}", exc_info=True)
