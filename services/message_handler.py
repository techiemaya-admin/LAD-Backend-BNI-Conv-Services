"""
Message handler orchestrator.

Receives incoming WhatsApp messages and routes them through:
  1. Deduplication (in-memory + DB)
  2. Debounce buffer (1s per member)
  3. Lead/conversation management
  4. Conversation engine (LLM)
  5. WhatsApp response

Multi-tenant: All operations are scoped to a WhatsAppAccount.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from typing import Dict

from db.connection import AsyncDBConnection
from services.conversation_engine import process_conversation
from services import whatsapp_client
from services import personal_whatsapp_client
from services.account_registry import WhatsAppAccount

logger = logging.getLogger(__name__)

# In-memory dedup: {external_message_id: timestamp}
_processed_messages: Dict[str, float] = {}
DEDUP_TTL_SECONDS = 300  # 5 minutes

# Per-member debounce buffers: {phone: {"messages": [], "task": Task, "chapter": WhatsAppAccount}}
_member_buffers: Dict[str, dict] = {}
DEBOUNCE_SECONDS = 1

# Per-member processing locks
_member_locks: Dict[str, asyncio.Lock] = {}


def _normalize_owner(owner: str | None) -> str:
    """Normalize owner values while keeping AI as the safe default."""
    val = (owner or "").strip().lower()
    if val in {"human_agent", "human", "human-agent"}:
        return "human_agent"
    return "AI"


def _get_member_lock(phone: str) -> asyncio.Lock:
    if phone not in _member_locks:
        _member_locks[phone] = asyncio.Lock()
    return _member_locks[phone]


def _is_duplicate(external_message_id: str) -> bool:
    """Check in-memory dedup cache."""
    now = time.time()
    expired = [k for k, v in _processed_messages.items() if now - v > DEDUP_TTL_SECONDS]
    for k in expired:
        del _processed_messages[k]

    if external_message_id in _processed_messages:
        return True
    _processed_messages[external_message_id] = now
    return False


async def _db_dedup_check(lead_id: str, message_text: str, chapter: WhatsAppAccount) -> bool:
    """Check DB-level dedup (30-second window)."""
    msg_hash = hashlib.sha256(f"{lead_id}:{message_text}".encode()).hexdigest()
    try:
        async with AsyncDBConnection(chapter.tenant_id) as conn:
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
                "INSERT INTO processed_messages (lead_id, message_hash, tenant_id) VALUES ($1, $2, $3::uuid)",
                lead_id,
                msg_hash,
                chapter.tenant_id,
            )
            return False
    except Exception as e:
        logger.error(f"[{chapter.slug}] DB dedup check error: {e}")
        return False


async def _get_or_create_lead(phone_number: str, contact_name: str, chapter: WhatsAppAccount) -> dict:
    """Get or create a lead by phone number. Returns {id, name, phone}."""
    async with AsyncDBConnection(chapter.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT id, name, phone FROM leads WHERE phone = $1", phone_number
        )
        if row:
            return {"id": str(row["id"]), "name": row["name"], "phone": row["phone"]}

        lead_id = str(uuid.uuid4())
        name = contact_name or phone_number
        await conn.execute(
            """
            INSERT INTO leads (id, organization_id, name, phone, channel, status, tenant_id, created_at, updated_at)
            VALUES ($1::uuid, $2::uuid, $3, $4, 'whatsapp', 'active', $2::uuid, NOW(), NOW())
            """,
            lead_id,
            chapter.tenant_id,
            name,
            phone_number,
        )
        return {"id": lead_id, "name": name, "phone": phone_number}


async def _get_or_create_conversation(lead_id: str, chapter: WhatsAppAccount) -> dict:
    """Get active conversation or create one. Returns {id, lead_id, owner}."""
    async with AsyncDBConnection(chapter.tenant_id) as conn:
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
                "owner": _normalize_owner(row["owner"]),
            }

        conv_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO conversations (id, lead_id, status, owner, metadata, tenant_id, started_at, updated_at)
            VALUES ($1::uuid, $2::uuid, 'active', 'AI', '{}', $3::uuid, NOW(), NOW())
            """,
            conv_id,
            lead_id,
            chapter.tenant_id,
        )
        return {"id": conv_id, "lead_id": lead_id, "owner": "AI"}


async def _save_incoming_message(
    conversation_id: str, lead_id: str, content: str, external_message_id: str,
    chapter: WhatsAppAccount,
):
    """Save incoming user message to DB."""
    msg_id = str(uuid.uuid4())
    async with AsyncDBConnection(chapter.tenant_id) as conn:
        await conn.execute(
            """
            INSERT INTO messages (id, conversation_id, lead_id, role, content,
                message_status, external_message_id, tenant_id, created_at)
            VALUES ($1::uuid, $2::uuid, $3::uuid, 'lead', $4, 'received', $5, $6::uuid, NOW())
            """,
            msg_id,
            conversation_id,
            lead_id,
            content,
            external_message_id,
            chapter.tenant_id,
        )


async def _save_outgoing_message(
    conversation_id: str, lead_id: str, content: str,
    chapter: WhatsAppAccount,
):
    """Save outgoing agent message to DB."""
    msg_id = str(uuid.uuid4())
    async with AsyncDBConnection(chapter.tenant_id) as conn:
        await conn.execute(
            """
            INSERT INTO messages (id, conversation_id, lead_id, role, content,
                message_status, tenant_id, created_at)
            VALUES ($1::uuid, $2::uuid, $3::uuid, 'agent', $4, 'sent', $5::uuid, NOW())
            """,
            msg_id,
            conversation_id,
            lead_id,
            content,
            chapter.tenant_id,
        )


async def _update_conversation_timestamp(conv_id: str, chapter: WhatsAppAccount):
    """Update conversation's last activity timestamp."""
    async with AsyncDBConnection(chapter.tenant_id) as conn:
        await conn.execute(
            "UPDATE conversations SET updated_at = NOW() WHERE id = $1::uuid", conv_id
        )


async def _prepare_message_context(
    phone_number: str,
    contact_name: str,
    message_text: str,
    external_message_id: str,
    chapter: WhatsAppAccount,
) -> tuple[str, str, str, bool]:
    """Prepare lead/conversation context in one connection.

    Returns: (lead_id, conversation_id, conversation_owner, is_db_duplicate)
    """
    msg_hash = hashlib.sha256(f"{phone_number}:{message_text}".encode()).hexdigest()

    async with AsyncDBConnection(chapter.tenant_id) as conn:
        # 1) Lead
        lead_row = await conn.fetchrow(
            "SELECT id, name, phone FROM leads WHERE phone = $1",
            phone_number,
        )

        if lead_row:
            lead_id = str(lead_row["id"])
        else:
            lead_id = str(uuid.uuid4())
            name = contact_name or phone_number
            await conn.execute(
                """
                INSERT INTO leads (id, organization_id, name, phone, channel, status, tenant_id, created_at, updated_at)
                VALUES ($1::uuid, $2::uuid, $3, $4, 'whatsapp', 'active', $2::uuid, NOW(), NOW())
                """,
                lead_id,
                chapter.tenant_id,
                name,
                phone_number,
            )

        # 2) DB dedup
        dedup_existing = await conn.fetchval(
            """
            SELECT 1 FROM processed_messages
                        WHERE lead_id = $1 AND message_hash = $2
              AND processed_at > NOW() - INTERVAL '30 seconds'
            """,
            lead_id,
            msg_hash,
        )
        if dedup_existing:
            return lead_id, "", "AI", True

        await conn.execute(
            "INSERT INTO processed_messages (lead_id, message_hash, tenant_id) VALUES ($1, $2, $3::uuid)",
            lead_id,
            msg_hash,
            chapter.tenant_id,
        )

        # 3) Conversation
        conv_row = await conn.fetchrow(
            """
            SELECT id, owner FROM conversations
            WHERE lead_id = $1::uuid AND status = 'active'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            lead_id,
        )

        if conv_row:
            conv_id = str(conv_row["id"])
            owner = _normalize_owner(conv_row["owner"])
        else:
            conv_id = str(uuid.uuid4())
            owner = "AI"
            await conn.execute(
                """
                INSERT INTO conversations (id, lead_id, status, owner, metadata, tenant_id, started_at, updated_at)
                VALUES ($1::uuid, $2::uuid, 'active', 'AI', '{}', $3::uuid, NOW(), NOW())
                """,
                conv_id,
                lead_id,
                chapter.tenant_id,
            )

        # 4) Save incoming + bump timestamp
        msg_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO messages (id, conversation_id, lead_id, role, content,
                message_status, external_message_id, tenant_id, created_at)
            VALUES ($1::uuid, $2::uuid, $3::uuid, 'lead', $4, 'received', $5, $6::uuid, NOW())
            """,
            msg_id,
            conv_id,
            lead_id,
            message_text,
            external_message_id,
            chapter.tenant_id,
        )
        await conn.execute(
            "UPDATE conversations SET updated_at = NOW() WHERE id = $1::uuid",
            conv_id,
        )

        return lead_id, conv_id, owner, False


async def handle_incoming_message(
    phone_number: str,
    message_text: str,
    contact_name: str,
    external_message_id: str,
    chapter: WhatsAppAccount,
):
    """Main entry point for incoming WhatsApp messages."""
    t_start = time.time()

    # Layer 1: In-memory dedup
    if _is_duplicate(external_message_id):
        logger.debug(f"[{chapter.slug}] Duplicate message {external_message_id}, skipping")
        return

    # Mark message as read immediately (blue ticks) — skip for personal WhatsApp
    is_personal = chapter.metadata.get("channel") == "personal_whatsapp"
    if not is_personal:
        asyncio.create_task(whatsapp_client.mark_as_read(external_message_id, chapter=chapter))

    # Step 1-4: Prepare lead/conversation + dedup + save in one DB connection
    t0 = time.time()
    lead_id, conv_id, owner, dedup_result = await _prepare_message_context(
        phone_number=phone_number,
        contact_name=contact_name,
        message_text=message_text,
        external_message_id=external_message_id,
        chapter=chapter,
    )
    logger.info(f"[{chapter.slug}][TIMING] db_prepare_context_total: {time.time()-t0:.3f}s")

    if dedup_result:
        logger.debug(f"[{chapter.slug}] DB duplicate for lead {lead_id}, skipping")
        return

    logger.info(f"[{chapter.slug}][TIMING] pre-debounce total: {time.time()-t_start:.3f}s")

    # Check ownership — skip LLM if human agent owns the conversation
    if owner == "human_agent":
        logger.info(f"[{chapter.slug}] Human agent owns conversation {conv_id}, skipping AI")
        return

    # Debounce: buffer messages per member, flush after 1s of silence
    if phone_number not in _member_buffers:
        _member_buffers[phone_number] = {"messages": [], "task": None, "chapter": chapter}

    buf = _member_buffers[phone_number]
    buf["messages"].append(message_text)
    buf["chapter"] = chapter  # Update chapter ref

    # Cancel existing flush task if any
    if buf["task"] and not buf["task"].done():
        buf["task"].cancel()

    # Schedule new flush
    buf["task"] = asyncio.create_task(
        _flush_buffer(phone_number, lead_id, conv_id, contact_name, chapter)
    )


async def _flush_buffer(
    phone_number: str, lead_id: str, conv_id: str, contact_name: str,
    chapter: WhatsAppAccount,
):
    """Wait for debounce period, then process combined messages."""
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return

    buf = _member_buffers.get(phone_number)
    if not buf or not buf["messages"]:
        return

    combined = " ".join(buf["messages"])
    buf["messages"].clear()

    lock = _get_member_lock(phone_number)
    async with lock:
        try:
            logger.info(
                f"[{chapter.slug}] Processing {len(combined)} chars from {phone_number}",
                extra={"lead_id": lead_id, "conv_id": conv_id}
            )
            
            t_llm = time.time()
            reply = await process_conversation(
                phone_number=phone_number,
                lead_id=lead_id,
                conversation_id=conv_id,
                message_text=combined,
                contact_name=contact_name,
                account=chapter,
            )
            logger.info(f"[{chapter.slug}][TIMING] process_conversation (LLM pipeline): {time.time()-t_llm:.3f}s")

            if not reply:
                logger.warning(
                    f"[{chapter.slug}] LLM returned empty response for {phone_number}",
                    extra={"lead_id": lead_id}
                )
                # Don't send error message — just log and continue
                return

            logger.info(
                f"[{chapter.slug}] AI Reply ready ({len(reply)} chars): {reply[:100]}...",
                extra={"phone_number": phone_number}
            )
            
            # Save agent response to database
            await _save_outgoing_message(
                conversation_id=conv_id,
                lead_id=lead_id,
                content=reply,
                chapter=chapter,
            )
            
            t_wa = time.time()
            sent = await _send_reply(
                phone_number=phone_number,
                text=reply,
                conversation_id=conv_id,
                lead_id=lead_id,
                chapter=chapter,
            )
            logger.info(f"[{chapter.slug}][TIMING] whatsapp_send: {time.time()-t_wa:.3f}s, sent: {sent}")
            
            if not sent:
                logger.error(
                    f"[{chapter.slug}] Failed to send reply to {phone_number}",
                    extra={"reply_text": reply[:50]}
                )
        except Exception as e:
            logger.error(
                f"[{chapter.slug}] Error processing message for {phone_number}: {e}", 
                exc_info=True,
                extra={"lead_id": lead_id}
            )


async def _send_reply(
    phone_number: str,
    text: str,
    conversation_id: str,
    lead_id: str,
    chapter: WhatsAppAccount,
) -> bool:
    """Route reply to the correct channel client.

    For personal WhatsApp (Baileys), sends via LAD_backend.
    For business WhatsApp (Cloud API), sends via Meta Graph API.
    
    Returns:
        True if message was sent successfully, False otherwise.
    """
    channel = chapter.metadata.get("channel", "business_whatsapp")
    slug = chapter.slug

    try:
        if channel == "personal_whatsapp":
            personal_account_id = chapter.metadata.get("personal_account_id", "")
            lad_backend_url = chapter.metadata.get("lad_backend_url") or None
            
            if not personal_account_id:
                logger.error(
                    f"[{slug}] Missing personal_account_id in metadata for personal WhatsApp channel",
                    extra={"phone_number": phone_number}
                )
                return False
            
            logger.info(
                f"[{slug}] Sending via personal WhatsApp channel",
                extra={"account_id": personal_account_id, "to": phone_number}
            )
            
            gateway_msg_id = await personal_whatsapp_client.send_message(
                phone_number=phone_number,
                text=text,
                personal_account_id=personal_account_id,
                conversation_id=conversation_id,
                lead_id=lead_id,
                account=chapter,
                lad_backend_url=lad_backend_url,
            )
            
            if gateway_msg_id:
                logger.info(
                    f"[{slug}] Personal WhatsApp message sent successfully",
                    extra={"msg_id": gateway_msg_id, "to": phone_number}
                )
                return True
            else:
                logger.error(
                    f"[{slug}] Personal WhatsApp send returned no message ID",
                    extra={"to": phone_number, "text_len": len(text)}
                )
                return False
        else:
            logger.info(
                f"[{slug}] Sending via business WhatsApp channel",
                extra={"to": phone_number}
            )
            
            gateway_msg_id = await whatsapp_client.send_message(
                phone_number=phone_number,
                text=text,
                conversation_id=conversation_id,
                lead_id=lead_id,
                chapter=chapter,
            )
            
            if gateway_msg_id:
                logger.info(
                    f"[{slug}] Business WhatsApp message sent successfully",
                    extra={"msg_id": gateway_msg_id}
                )
                return True
            else:
                logger.error(
                    f"[{slug}] Business WhatsApp send returned no message ID",
                    extra={"to": phone_number}
                )
                return False
                
    except Exception as e:
        logger.error(
            f"[{slug}] Exception sending reply to {phone_number}: {e}",
            exc_info=True,
            extra={"channel": channel}
        )
        return False
