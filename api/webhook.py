"""
WhatsApp webhook endpoint.

GET  /webhook — Facebook verification handshake
POST /webhook — Receive incoming WhatsApp messages
"""
import logging
import os

from fastapi import APIRouter, Request, BackgroundTasks, Query
from fastapi.responses import PlainTextResponse, JSONResponse

from services.message_handler import handle_incoming_message
from db.connection import ClientDBConnection

logger = logging.getLogger(__name__)

VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "BNI_Rising_Phoenix_2026")

router = APIRouter(tags=["webhook"])


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """Facebook webhook verification handshake."""
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        logger.info("Webhook verified successfully")
        return PlainTextResponse(content=hub_challenge)

    logger.warning(f"Webhook verification failed: mode={hub_mode}")
    return PlainTextResponse(content="Verification failed", status_code=403)


@router.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive incoming WhatsApp messages. Returns 200 immediately, processes in background."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "invalid payload"}, status_code=400)

    # Return 200 immediately (Facebook requires < 5s response)
    # Process message in background
    background_tasks.add_task(process_webhook_payload, data)
    return JSONResponse({"status": "accepted"})


async def process_webhook_payload(data: dict):
    """Background processing of webhook payload."""
    try:
        if data.get("object") != "whatsapp_business_account":
            return

        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                # Handle delivery status updates
                if "statuses" in value:
                    for status in value["statuses"]:
                        await _update_message_status(status)

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
                            )

    except Exception as e:
        logger.error(f"Error processing webhook payload: {e}", exc_info=True)


async def _update_message_status(status: dict):
    """Update message delivery status from WhatsApp status webhook.

    WhatsApp sends: sent → delivered → read (each as a separate callback).
    We map these to our message_status column.
    """
    try:
        wa_message_id = status.get("id")
        wa_status = status.get("status")  # sent, delivered, read, failed

        if not wa_message_id or not wa_status:
            return

        # Map WhatsApp status to our DB status
        status_map = {
            "sent": "sent",
            "delivered": "delivered",
            "read": "read",
            "failed": "failed",
        }
        db_status = status_map.get(wa_status)
        if not db_status:
            return

        async with ClientDBConnection() as conn:
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
                logger.info(f"Message {wa_message_id} status → {db_status}")

    except Exception as e:
        logger.error(f"Error updating message status: {e}")
