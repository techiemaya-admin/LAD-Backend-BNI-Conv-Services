"""
Post-meeting followup task — runs every 15 minutes via APScheduler.

Checks for meetings whose confirmed_time has passed and triggers
post-meeting followup conversations to collect outcomes (referrals, TYFCB).
"""
import json
import logging
from datetime import datetime, timedelta

from db.connection import ClientDBConnection
from services import whatsapp_client

logger = logging.getLogger(__name__)

FOLLOWUP_DELAY_HOURS = 1


async def send_post_meeting_followups():
    """Check for completed meetings and initiate followup conversations."""
    try:
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=FOLLOWUP_DELAY_HOURS)

        async with ClientDBConnection() as conn:
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
                        phone_number=phone, text=message
                    )

                    await conn.execute(
                        """
                        UPDATE bni_conversation_manager
                        SET context_status = 'post_meeting_followup',
                            metadata = jsonb_set(
                                COALESCE(metadata, '{}')::jsonb,
                                '{meeting_json}',
                                $1::jsonb
                            ),
                            updated_at = NOW()
                        WHERE member_phone = $2
                        """,
                        json.dumps({
                            "meeting_id": meeting_id,
                            "other_member_name": other_name,
                            "meeting_date": time_str,
                        }),
                        phone,
                    )

                logger.info(f"Sent followup for meeting {meeting_id}")

    except Exception as e:
        logger.error(f"Error in followup task: {e}", exc_info=True)
