"""
Message handler orchestrator.

Receives incoming WhatsApp messages and routes them through:
  1. Deduplication (in-memory + DB)
  2. Debounce buffer (1s per member)
  3. Lead/conversation management
  4. Conversation engine (LLM)
  5. WhatsApp response
"""
import asyncio
import hashlib
import logging
import time
import uuid
from typing import Dict

from db.connection import ClientDBConnection
from db.schema import get_tenant_id
from services.conversation_engine import process_conversation
from services import whatsapp_client

logger = logging.getLogger(__name__)

# In-memory dedup: {external_message_id: timestamp}
_processed_messages: Dict[str, float] = {}
DEDUP_TTL_SECONDS = 300  # 5 minutes

# Per-member debounce buffers: {phone: {"messages": [], "task": Task, "lock": Lock}}
_member_buffers: Dict[str, dict] = {}
DEBOUNCE_SECONDS = 1

# Per-member processing locks
_member_locks: Dict[str, asyncio.Lock] = {}


def _get_member_lock(phone: str) -> asyncio.Lock:
    if phone not in _member_locks:
        _member_locks[phone] = asyncio.Lock()
    return _member_locks[phone]


def _is_duplicate(external_message_id: str) -> bool:
    """Check in-memory dedup cache."""
    now = time.time()
    # Clean expired entries
    expired = [k for k, v in _processed_messages.items() if now - v > DEDUP_TTL_SECONDS]
    for k in expired:
        del _processed_messages[k]

    if external_message_id in _processed_messages:
        return True
    _processed_messages[external_message_id] = now
    return False


async def _db_dedup_check(lead_id: str, message_text: str) -> bool:
    """Check DB-level dedup (30-second window)."""
    msg_hash = hashlib.sha256(f"{lead_id}:{message_text}".encode()).hexdigest()
    try:
        async with ClientDBConnection() as conn:
            existing = await conn.fetchval(
                """
                SELECT 1 FROM processed_messages
                WHERE lead_id = $1 AND message_hash = $2
                AND processed_at > NOW() - INTERVAL '30 seconds'
                """,
                lead_id,
                msg_hash,
            )
            if existing:
                return True
            await conn.execute(
                "INSERT INTO processed_messages (lead_id, message_hash) VALUES ($1, $2)",
                lead_id,
                msg_hash,
            )
            return False
    except Exception as e:
        logger.error(f"DB dedup check error: {e}")
        return False


async def _get_or_create_lead(phone_number: str, contact_name: str) -> dict:
    """Get or create a lead by phone number. Returns {id, name, phone}."""
    async with ClientDBConnection() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, phone FROM leads WHERE phone = $1", phone_number
        )
        if row:
            return {"id": str(row["id"]), "name": row["name"], "phone": row["phone"]}

        lead_id = str(uuid.uuid4())
        org_id = get_tenant_id()
        name = contact_name or phone_number
        await conn.execute(
            """
            INSERT INTO leads (id, organization_id, name, phone, channel, status, created_at, updated_at)
            VALUES ($1::uuid, $2::uuid, $3, $4, 'whatsapp', 'active', NOW(), NOW())
            """,
            lead_id,
            org_id,
            name,
            phone_number,
        )
        return {"id": lead_id, "name": name, "phone": phone_number}


async def _get_or_create_conversation(lead_id: str) -> dict:
    """Get active conversation or create one. Returns {id, lead_id, owner}."""
    async with ClientDBConnection() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, lead_id, owner FROM conversations
            WHERE lead_id = $1::uuid AND status = 'active'
            ORDER BY updated_at DESC LIMIT 1
            """,
            lead_id,
        )
        if row:
            return {
                "id": str(row["id"]),
                "lead_id": str(row["lead_id"]),
                "owner": row["owner"] or "AI",
            }

        conv_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO conversations (id, lead_id, status, owner, metadata, started_at, updated_at)
            VALUES ($1::uuid, $2::uuid, 'active', 'AI', '{}', NOW(), NOW())
            """,
            conv_id,
            lead_id,
        )
        return {"id": conv_id, "lead_id": lead_id, "owner": "AI"}


async def _save_incoming_message(
    conversation_id: str, lead_id: str, content: str, external_message_id: str
):
    """Save incoming user message to DB."""
    msg_id = str(uuid.uuid4())
    async with ClientDBConnection() as conn:
        await conn.execute(
            """
            INSERT INTO messages (id, conversation_id, lead_id, role, content,
                message_status, external_message_id, created_at)
            VALUES ($1::uuid, $2::uuid, $3::uuid, 'lead', $4, 'received', $5, NOW())
            """,
            msg_id,
            conversation_id,
            lead_id,
            content,
            external_message_id,
        )


async def handle_incoming_message(
    phone_number: str,
    message_text: str,
    contact_name: str,
    external_message_id: str,
):
    """Main entry point for incoming WhatsApp messages."""
    t_start = time.time()

    # Layer 1: In-memory dedup
    if _is_duplicate(external_message_id):
        logger.debug(f"Duplicate message {external_message_id}, skipping")
        return

    # Mark message as read immediately (blue ticks = user knows we received it)
    # Run in background, don't wait
    asyncio.create_task(whatsapp_client.mark_as_read(external_message_id))

    # Step 1: Get or create lead (must be sequential — need lead_id for next steps)
    t0 = time.time()
    lead = await _get_or_create_lead(phone_number, contact_name)
    lead_id = lead["id"]
    logger.info(f"[TIMING] get_or_create_lead: {time.time()-t0:.3f}s")

    # Step 2: DB dedup + get/create conversation IN PARALLEL
    t0 = time.time()
    dedup_result, conversation = await asyncio.gather(
        _db_dedup_check(lead_id, message_text),
        _get_or_create_conversation(lead_id),
    )
    logger.info(f"[TIMING] db_dedup+get_conversation (parallel): {time.time()-t0:.3f}s")

    if dedup_result:
        logger.debug(f"DB duplicate for lead {lead_id}, skipping")
        return

    conv_id = conversation["id"]

    # Step 3: Save message + update timestamp IN PARALLEL
    t0 = time.time()
    await asyncio.gather(
        _save_incoming_message(conv_id, lead_id, message_text, external_message_id),
        _update_conversation_timestamp(conv_id),
    )
    logger.info(f"[TIMING] save_msg+update_timestamp (parallel): {time.time()-t0:.3f}s")

    logger.info(f"[TIMING] pre-debounce total: {time.time()-t_start:.3f}s")

    # Check ownership — skip LLM if human agent owns the conversation
    if conversation["owner"] == "human_agent":
        logger.info(f"Human agent owns conversation {conv_id}, skipping AI")
        return

    # Debounce: buffer messages per member, flush after 1s of silence
    if phone_number not in _member_buffers:
        _member_buffers[phone_number] = {"messages": [], "task": None}

    buf = _member_buffers[phone_number]
    buf["messages"].append(message_text)

    # Cancel existing flush task if any
    if buf["task"] and not buf["task"].done():
        buf["task"].cancel()

    # Schedule new flush
    buf["task"] = asyncio.create_task(
        _flush_buffer(phone_number, lead_id, conv_id, contact_name)
    )


async def _update_conversation_timestamp(conv_id: str):
    """Update conversation's last activity timestamp."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            "UPDATE conversations SET updated_at = NOW() WHERE id = $1::uuid", conv_id
        )


async def _flush_buffer(
    phone_number: str, lead_id: str, conv_id: str, contact_name: str
):
    """Wait for debounce period, then process combined messages."""
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return  # New message came in, timer reset

    buf = _member_buffers.get(phone_number)
    if not buf or not buf["messages"]:
        return

    # Collect all buffered messages
    combined = " ".join(buf["messages"])
    buf["messages"].clear()

    # Process with per-member lock
    lock = _get_member_lock(phone_number)
    async with lock:
        try:
            t_llm = time.time()
            reply = await process_conversation(
                phone_number=phone_number,
                lead_id=lead_id,
                conversation_id=conv_id,
                message_text=combined,
                contact_name=contact_name,
            )
            logger.info(f"[TIMING] process_conversation (LLM pipeline): {time.time()-t_llm:.3f}s")

            if reply:
                t_wa = time.time()
                await whatsapp_client.send_message(
                    phone_number=phone_number,
                    text=reply,
                    conversation_id=conv_id,
                    lead_id=lead_id,
                )
                logger.info(f"[TIMING] whatsapp_send: {time.time()-t_wa:.3f}s")
                logger.info(f"[TIMING] TOTAL (debounce+LLM+send): {time.time()-t_llm+DEBOUNCE_SECONDS:.3f}s")
        except Exception as e:
            logger.error(
                f"Error processing message for {phone_number}: {e}", exc_info=True
            )
