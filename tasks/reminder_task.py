"""
Meeting reminder task — runs every 5 minutes via APScheduler.

Checks for upcoming confirmed meetings and sends WhatsApp reminders:
  - 24 hours before: "You have a 1-to-1 meeting tomorrow with {name} at {time}"
  - 1 hour before: "Reminder: your 1-to-1 with {name} starts in 1 hour at {time}"
"""
import logging
from datetime import datetime, timedelta

from db.connection import ClientDBConnection
from services import whatsapp_client

logger = logging.getLogger(__name__)


async def send_meeting_reminders():
    """Check and send pending meeting reminders."""
    try:
        now = datetime.utcnow()

        async with ClientDBConnection() as conn:
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

                await whatsapp_client.send_message(phone_number=phone, text=message)

                await conn.execute(
                    "UPDATE meeting_reminders SET sent = true WHERE id = $1::uuid",
                    str(r["id"]),
                )
                logger.info(f"Sent {reminder_type} reminder to {phone}")

    except Exception as e:
        logger.error(f"Error in reminder task: {e}", exc_info=True)
