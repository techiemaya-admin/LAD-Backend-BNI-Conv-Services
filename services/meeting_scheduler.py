"""
Meeting scheduler — two-member async coordination.

Manages the lifecycle of a 1-to-1 meeting between two BNI members:
  1. Match accepted → ask member_a for availability
  2. Member_a provides slots → proactively message member_b
  3. Member_b provides slots → find overlap → propose time
  4. Both confirm → create reminders
  5. After meeting → trigger followup

Members are in SEPARATE WhatsApp conversations. Cross-member messaging
goes through whatsapp_client directly (not through webhook path).
"""
import json
import logging
import uuid
from datetime import datetime

from db.connection import ClientDBConnection
from services import whatsapp_client

logger = logging.getLogger(__name__)


async def initiate_meeting_from_match(
    member_a_phone: str, conversation_id: str, lead_id: str
):
    """Create a scheduled_meetings row and set member_a's state to collect availability.

    Called when member_a accepts a match suggestion.
    """
    try:
        # Get match info from member_a's conversation state
        async with ClientDBConnection() as conn:
            state = await conn.fetchrow(
                "SELECT metadata FROM bni_conversation_manager WHERE member_phone = $1",
                member_a_phone,
            )
            if not state:
                return

            metadata = state["metadata"]
            if isinstance(metadata, str):
                metadata = json.loads(metadata)

            match_info = metadata.get("match_json")
            if isinstance(match_info, str):
                match_info = json.loads(match_info)

            if not match_info:
                logger.error(f"No match_json in metadata for {member_a_phone}")
                return

            member_b_phone = match_info.get("phone")
            member_b_name = match_info.get("name")

            if not member_b_phone:
                logger.error("Match info missing phone number")
                return

            # Create the meeting record
            meeting_id = str(uuid.uuid4())
            await conn.execute(
                """
                INSERT INTO scheduled_meetings
                    (id, member_a_phone, member_a_name, member_b_phone, member_b_name,
                     status, created_at, updated_at)
                VALUES ($1::uuid, $2, $3, $4, $5, 'pending_a_availability', NOW(), NOW())
                """,
                meeting_id,
                member_a_phone,
                metadata.get("member_name", member_a_phone),
                member_b_phone,
                member_b_name,
            )

            # Store meeting_id in member_a's metadata
            await conn.execute(
                """
                UPDATE bni_conversation_manager
                SET metadata = jsonb_set(
                    COALESCE(metadata, '{}')::jsonb,
                    '{pending_meeting_id}',
                    $1::jsonb
                ),
                context_status = 'coordination_a_availability'
                WHERE member_phone = $2
                """,
                json.dumps(meeting_id),
                member_a_phone,
            )

            logger.info(
                f"Meeting {meeting_id} initiated: {member_a_phone} <-> {member_b_phone}"
            )

    except Exception as e:
        logger.error(f"Error initiating meeting: {e}", exc_info=True)


async def handle_availability_response(
    phone_number: str, slots: list[dict]
) -> str | None:
    """Process availability slots from a member.

    Returns a status message or None. Triggers cross-member messaging when needed.
    """
    try:
        async with ClientDBConnection() as conn:
            # Find the active meeting for this member
            meeting = await conn.fetchrow(
                """
                SELECT * FROM scheduled_meetings
                WHERE (member_a_phone = $1 OR member_b_phone = $1)
                  AND status IN ('pending_a_availability', 'pending_b_availability')
                ORDER BY created_at DESC LIMIT 1
                """,
                phone_number,
            )

            if not meeting:
                return None

            meeting_id = str(meeting["id"])
            is_member_a = meeting["member_a_phone"] == phone_number
            slots_json = json.dumps(slots)

            if is_member_a and meeting["status"] == "pending_a_availability":
                # Member A provided slots → store and message member B
                await conn.execute(
                    """
                    UPDATE scheduled_meetings
                    SET member_a_slots = $1::jsonb,
                        status = 'pending_b_availability',
                        updated_at = NOW()
                    WHERE id = $2::uuid
                    """,
                    slots_json,
                    meeting_id,
                )

                # Proactively message member B
                member_b_phone = meeting["member_b_phone"]
                member_a_name = meeting["member_a_name"] or phone_number

                await _notify_member_b_for_availability(
                    member_b_phone, member_a_name, meeting_id
                )

                return "availability_stored"

            elif not is_member_a and meeting["status"] == "pending_b_availability":
                # Member B provided slots → find overlap
                await conn.execute(
                    """
                    UPDATE scheduled_meetings
                    SET member_b_slots = $1::jsonb,
                        updated_at = NOW()
                    WHERE id = $2::uuid
                    """,
                    slots_json,
                    meeting_id,
                )

                # Try to find overlapping time
                member_a_slots = meeting["member_a_slots"]
                if isinstance(member_a_slots, str):
                    member_a_slots = json.loads(member_a_slots)

                overlap = _find_time_overlap(member_a_slots, slots)

                if overlap:
                    # Propose the overlapping time to both
                    await conn.execute(
                        """
                        UPDATE scheduled_meetings
                        SET proposed_time = $1,
                            status = 'overlap_proposed',
                            updated_at = NOW()
                        WHERE id = $2::uuid
                        """,
                        overlap,
                        meeting_id,
                    )
                    await _propose_time_to_both(meeting, overlap)
                    return "overlap_found"
                else:
                    # No overlap — ask both to try again
                    await conn.execute(
                        """
                        UPDATE scheduled_meetings
                        SET status = 'pending_a_availability',
                            member_a_slots = NULL,
                            member_b_slots = NULL,
                            updated_at = NOW()
                        WHERE id = $2::uuid
                        """,
                        meeting_id,
                    )
                    return "no_overlap"

            return None

    except Exception as e:
        logger.error(f"Error handling availability: {e}", exc_info=True)
        return None


async def handle_meeting_confirmation(
    phone_number: str, confirmed: bool
) -> str | None:
    """Process meeting time confirmation from a member."""
    try:
        async with ClientDBConnection() as conn:
            meeting = await conn.fetchrow(
                """
                SELECT * FROM scheduled_meetings
                WHERE (member_a_phone = $1 OR member_b_phone = $1)
                  AND status = 'overlap_proposed'
                ORDER BY created_at DESC LIMIT 1
                """,
                phone_number,
            )

            if not meeting:
                return None

            meeting_id = str(meeting["id"])
            is_member_a = meeting["member_a_phone"] == phone_number

            if not confirmed:
                # Reset to collect availability again
                await conn.execute(
                    """
                    UPDATE scheduled_meetings
                    SET status = 'pending_a_availability',
                        member_a_slots = NULL, member_b_slots = NULL,
                        proposed_time = NULL,
                        member_a_confirmed = false, member_b_confirmed = false,
                        updated_at = NOW()
                    WHERE id = $1::uuid
                    """,
                    meeting_id,
                )
                return "declined"

            # Update confirmation flag
            col = "member_a_confirmed" if is_member_a else "member_b_confirmed"
            await conn.execute(
                f"""
                UPDATE scheduled_meetings
                SET {col} = true, updated_at = NOW()
                WHERE id = $1::uuid
                """,
                meeting_id,
            )

            # Check if both confirmed
            updated = await conn.fetchrow(
                "SELECT * FROM scheduled_meetings WHERE id = $1::uuid", meeting_id
            )

            if updated["member_a_confirmed"] and updated["member_b_confirmed"]:
                # Both confirmed → finalize
                await conn.execute(
                    """
                    UPDATE scheduled_meetings
                    SET status = 'confirmed',
                        confirmed_time = proposed_time,
                        updated_at = NOW()
                    WHERE id = $1::uuid
                    """,
                    meeting_id,
                )

                # Create reminders
                await _create_meeting_reminders(updated)

                # Update both members' conversation state to idle
                for ph in [meeting["member_a_phone"], meeting["member_b_phone"]]:
                    await conn.execute(
                        """
                        UPDATE bni_conversation_manager
                        SET context_status = 'idle', updated_at = NOW()
                        WHERE member_phone = $1
                        """,
                        ph,
                    )

                return "both_confirmed"

            return "waiting_other"

    except Exception as e:
        logger.error(f"Error handling confirmation: {e}", exc_info=True)
        return None


async def _notify_member_b_for_availability(
    member_b_phone: str, member_a_name: str, meeting_id: str
):
    """Proactively message member B to collect their availability."""
    try:
        # Update member B's conversation state
        async with ClientDBConnection() as conn:
            # Ensure member B has a conversation state row
            existing = await conn.fetchrow(
                "SELECT id FROM bni_conversation_manager WHERE member_phone = $1",
                member_b_phone,
            )

            if existing:
                await conn.execute(
                    """
                    UPDATE bni_conversation_manager
                    SET context_status = 'coordination_b_availability',
                        metadata = jsonb_set(
                            COALESCE(metadata, '{}')::jsonb,
                            '{pending_meeting_id}',
                            $1::jsonb
                        ),
                        updated_at = NOW()
                    WHERE member_phone = $2
                    """,
                    json.dumps(meeting_id),
                    member_b_phone,
                )
            else:
                state_id = str(uuid.uuid4())
                await conn.execute(
                    """
                    INSERT INTO bni_conversation_manager
                        (id, member_phone, context_status, metadata, created_at, updated_at)
                    VALUES ($1::uuid, $2, 'coordination_b_availability',
                            $3::jsonb, NOW(), NOW())
                    """,
                    state_id,
                    member_b_phone,
                    json.dumps({"pending_meeting_id": meeting_id}),
                )

        # Send WhatsApp message to member B
        message = (
            f"Hi! {member_a_name} from BNI Rising Phoenix would like to schedule "
            f"a 1-to-1 meeting with you. Could you share a few time slots that "
            f"work for you this week? (e.g., 'Tuesday 2-4pm, Wednesday 10am-12pm')"
        )
        await whatsapp_client.send_message(phone_number=member_b_phone, text=message)
        logger.info(f"Notified member B ({member_b_phone}) for availability")

    except Exception as e:
        logger.error(f"Error notifying member B: {e}", exc_info=True)


def _find_time_overlap(
    slots_a: list[dict], slots_b: list[dict]
) -> datetime | None:
    """Find the first overlapping time slot between two members.

    Each slot is expected as {"date": "YYYY-MM-DD", "start": "HH:MM", "end": "HH:MM"}.
    Returns the proposed meeting datetime or None.
    """
    try:
        for sa in slots_a:
            for sb in slots_b:
                if sa.get("date") != sb.get("date"):
                    continue

                # Same date — check time overlap
                a_start = _parse_time(sa["start"])
                a_end = _parse_time(sa["end"])
                b_start = _parse_time(sb["start"])
                b_end = _parse_time(sb["end"])

                overlap_start = max(a_start, b_start)
                overlap_end = min(a_end, b_end)

                if overlap_start < overlap_end:
                    # There's an overlap — propose the start of the overlap
                    date_str = sa["date"]
                    return datetime.strptime(
                        f"{date_str} {overlap_start}", "%Y-%m-%d %H:%M"
                    )
    except Exception as e:
        logger.error(f"Error finding time overlap: {e}")

    return None


def _parse_time(time_str: str) -> str:
    """Normalize time string to HH:MM format for comparison."""
    time_str = time_str.strip()
    if len(time_str) <= 5:
        return time_str
    # Handle "2:00 PM" style
    try:
        dt = datetime.strptime(time_str, "%I:%M %p")
        return dt.strftime("%H:%M")
    except ValueError:
        return time_str


async def _propose_time_to_both(meeting: dict, proposed_time: datetime):
    """Send proposed meeting time to both members."""
    time_str = proposed_time.strftime("%A, %B %d at %I:%M %p")

    for phone, other_name in [
        (meeting["member_a_phone"], meeting["member_b_name"]),
        (meeting["member_b_phone"], meeting["member_a_name"]),
    ]:
        message = (
            f"Great news! I found a time that works for your 1-to-1 with {other_name}: "
            f"{time_str}. Does this work for you? (Reply 'yes' to confirm or 'no' to reschedule)"
        )
        await whatsapp_client.send_message(phone_number=phone, text=message)

    logger.info(f"Proposed time {time_str} to both members")


async def _create_meeting_reminders(meeting: dict):
    """Create reminder entries for a confirmed meeting."""
    try:
        confirmed_time = meeting["confirmed_time"] or meeting["proposed_time"]
        if not confirmed_time:
            return

        async with ClientDBConnection() as conn:
            for phone in [meeting["member_a_phone"], meeting["member_b_phone"]]:
                for reminder_type in ["24h_before", "1h_before"]:
                    reminder_id = str(uuid.uuid4())
                    await conn.execute(
                        """
                        INSERT INTO meeting_reminders
                            (id, meeting_id, member_phone, reminder_type,
                             scheduled_time, sent, created_at)
                        VALUES ($1::uuid, $2::uuid, $3, $4, $5, false, NOW())
                        """,
                        reminder_id,
                        str(meeting["id"]),
                        phone,
                        reminder_type,
                        confirmed_time,
                    )

        logger.info(f"Created reminders for meeting {meeting['id']}")

    except Exception as e:
        logger.error(f"Error creating reminders: {e}", exc_info=True)


async def complete_meeting(meeting_id: str):
    """Mark a meeting as completed (called by followup task)."""
    try:
        async with ClientDBConnection() as conn:
            await conn.execute(
                """
                UPDATE scheduled_meetings
                SET status = 'completed', updated_at = NOW()
                WHERE id = $1::uuid
                """,
                meeting_id,
            )
    except Exception as e:
        logger.error(f"Error completing meeting: {e}")
