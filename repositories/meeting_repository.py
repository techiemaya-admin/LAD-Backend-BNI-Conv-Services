"""
Meeting repository — data access for scheduled_meetings and meeting_reminders.

Database: salesmaya_bni (client DB).
"""
import json
import logging
import uuid

from db.connection import ClientDBConnection

logger = logging.getLogger(__name__)


async def get_pending_meeting_phones(phone_number: str) -> set[str]:
    """Get phone numbers that have pending meetings with the given member."""
    async with ClientDBConnection() as conn:
        rows = await conn.fetch(
            """
            SELECT member_b_phone FROM scheduled_meetings
            WHERE member_a_phone = $1 AND status NOT IN ('completed', 'cancelled')
            UNION
            SELECT member_a_phone FROM scheduled_meetings
            WHERE member_b_phone = $1 AND status NOT IN ('completed', 'cancelled')
            """,
            phone_number,
        )
        return {r[0] for r in rows}


async def create_meeting(meeting_id: str, member_a_phone: str, member_a_name: str,
                          member_b_phone: str, member_b_name: str) -> None:
    """Create a new scheduled_meetings record."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            """
            INSERT INTO scheduled_meetings
                (id, member_a_phone, member_a_name, member_b_phone, member_b_name,
                 status, created_at, updated_at)
            VALUES ($1::uuid, $2, $3, $4, $5, 'pending_a_availability', NOW(), NOW())
            """,
            meeting_id,
            member_a_phone,
            member_a_name,
            member_b_phone,
            member_b_name,
        )


async def find_active_meeting(phone_number: str, statuses: list[str]) -> dict | None:
    """Find the most recent meeting for a member in the given statuses."""
    placeholders = ", ".join(f"${i+2}" for i in range(len(statuses)))
    async with ClientDBConnection() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT * FROM scheduled_meetings
            WHERE (member_a_phone = $1 OR member_b_phone = $1)
              AND status IN ({placeholders})
            ORDER BY created_at DESC LIMIT 1
            """,
            phone_number,
            *statuses,
        )
        return dict(row) if row else None


async def update_meeting_slots(meeting_id: str, member: str, slots_json: str) -> None:
    """Store availability slots for member_a or member_b."""
    col = "member_a_slots" if member == "a" else "member_b_slots"
    status_update = "status = 'pending_b_availability'," if member == "a" else ""
    async with ClientDBConnection() as conn:
        await conn.execute(
            f"""
            UPDATE scheduled_meetings
            SET {col} = $1::jsonb,
                {status_update}
                updated_at = NOW()
            WHERE id = $2::uuid
            """,
            slots_json,
            meeting_id,
        )


async def propose_meeting_time(meeting_id: str, proposed_time) -> None:
    """Set proposed overlap time and advance to overlap_proposed."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            """
            UPDATE scheduled_meetings
            SET proposed_time = $1,
                status = 'overlap_proposed',
                updated_at = NOW()
            WHERE id = $2::uuid
            """,
            proposed_time,
            meeting_id,
        )


async def reset_meeting_availability(meeting_id: str) -> None:
    """Reset a meeting to re-collect availability."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            """
            UPDATE scheduled_meetings
            SET status = 'pending_a_availability',
                member_a_slots = NULL,
                member_b_slots = NULL,
                proposed_time = NULL,
                member_a_confirmed = false, member_b_confirmed = false,
                updated_at = NOW()
            WHERE id = $1::uuid
            """,
            meeting_id,
        )


async def set_meeting_confirmation(meeting_id: str, is_member_a: bool) -> None:
    """Set the confirmation flag for one member."""
    col = "member_a_confirmed" if is_member_a else "member_b_confirmed"
    async with ClientDBConnection() as conn:
        await conn.execute(
            f"""
            UPDATE scheduled_meetings
            SET {col} = true, updated_at = NOW()
            WHERE id = $1::uuid
            """,
            meeting_id,
        )


async def get_meeting_by_id(meeting_id: str) -> dict | None:
    """Fetch a meeting by ID."""
    async with ClientDBConnection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM scheduled_meetings WHERE id = $1::uuid", meeting_id
        )
        return dict(row) if row else None


async def confirm_meeting(meeting_id: str) -> None:
    """Finalize a meeting as confirmed."""
    async with ClientDBConnection() as conn:
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


async def complete_meeting(meeting_id: str) -> None:
    """Mark a meeting as completed."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            """
            UPDATE scheduled_meetings
            SET status = 'completed', updated_at = NOW()
            WHERE id = $1::uuid
            """,
            meeting_id,
        )


async def set_meeting_followup_status(meeting_id: str) -> None:
    """Mark a meeting as post_meeting_followup."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            """
            UPDATE scheduled_meetings
            SET status = 'post_meeting_followup', updated_at = NOW()
            WHERE id = $1::uuid
            """,
            meeting_id,
        )


async def get_meetings_past_confirmed_time(cutoff) -> list[dict]:
    """Get confirmed meetings whose confirmed_time is before the cutoff."""
    async with ClientDBConnection() as conn:
        rows = await conn.fetch(
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
        return [dict(r) for r in rows]


async def create_meeting_reminder(meeting_id: str, phone: str,
                                   reminder_type: str, scheduled_time) -> None:
    """Create a meeting reminder entry."""
    reminder_id = str(uuid.uuid4())
    async with ClientDBConnection() as conn:
        await conn.execute(
            """
            INSERT INTO meeting_reminders
                (id, meeting_id, member_phone, reminder_type,
                 scheduled_time, sent, created_at)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, false, NOW())
            """,
            reminder_id,
            meeting_id,
            phone,
            reminder_type,
            scheduled_time,
        )


async def get_pending_reminders() -> list[dict]:
    """Get unsent reminders for confirmed meetings."""
    async with ClientDBConnection() as conn:
        rows = await conn.fetch(
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
        return [dict(r) for r in rows]


async def mark_reminder_sent(reminder_id: str) -> None:
    """Mark a reminder as sent."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            "UPDATE meeting_reminders SET sent = true WHERE id = $1::uuid",
            reminder_id,
        )
