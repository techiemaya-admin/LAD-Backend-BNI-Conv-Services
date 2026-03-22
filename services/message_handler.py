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
from services import linkedin_client
from services import instagram_client
from services import gmail_client
from services.account_registry import WhatsAppAccount, CHANNEL_BUSINESS, CHANNEL_PERSONAL

# Multi-channel constants
CHANNEL_LINKEDIN = "linkedin"
CHANNEL_INSTAGRAM = "instagram"
CHANNEL_GMAIL = "gmail"

logger = logging.getLogger(__name__)

# In-memory dedup: {external_message_id: timestamp}
_processed_messages: Dict[str, float] = {}
DEDUP_TTL_SECONDS = 300  # 5 minutes

# Per-member debounce buffers: {phone: {"messages": [], "task": Task, "account": WhatsAppAccount}}
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


async def _db_dedup_check(lead_id: str, message_text: str, account: WhatsAppAccount) -> bool:
    """Check DB-level dedup (30-second window)."""
    msg_hash = hashlib.sha256(f"{lead_id}:{message_text}".encode()).hexdigest()
    try:
        async with AsyncDBConnection(account.tenant_id) as conn:
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
                account.tenant_id,
            )
            return False
    except Exception as e:
        logger.error(f"[{account.slug}] DB dedup check error: {e}")
        return False


async def _get_or_create_lead(phone_number: str, contact_name: str, account: WhatsAppAccount) -> dict:
    """Get or create a lead by phone number. Returns {id, name, phone}."""
    async with AsyncDBConnection(account.tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT id, name, phone FROM wa_contacts WHERE phone = $1", phone_number
        )
        if row:
            return {"id": str(row["id"]), "name": row["name"], "phone": row["phone"]}

        lead_id = str(uuid.uuid4())
        name = contact_name or phone_number
        await conn.execute(
            """
            INSERT INTO wa_contacts (id, name, phone, channel, status, tenant_id, created_at, updated_at)
            VALUES ($1::uuid, $2, $3, 'whatsapp', 'active', $4::uuid, NOW(), NOW())
            """,
            lead_id,
            name,
            phone_number,
            account.tenant_id,
        )
        return {"id": lead_id, "name": name, "phone": phone_number}


async def _get_or_create_conversation(lead_id: str, account: WhatsAppAccount) -> dict:
    """Get active conversation or create one. Returns {id, lead_id, owner}."""
    async with AsyncDBConnection(account.tenant_id) as conn:
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
            account.tenant_id,
        )
        return {"id": conv_id, "lead_id": lead_id, "owner": "AI"}


async def _save_incoming_message(
    conversation_id: str, lead_id: str, content: str, external_message_id: str,
    account: WhatsAppAccount,
):
    """Save incoming user message to DB."""
    msg_id = str(uuid.uuid4())
    async with AsyncDBConnection(account.tenant_id) as conn:
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
            account.tenant_id,
        )


async def _save_outgoing_message(
    conversation_id: str, lead_id: str, content: str,
    account: WhatsAppAccount,
):
    """Save outgoing agent message to DB."""
    msg_id = str(uuid.uuid4())
    async with AsyncDBConnection(account.tenant_id) as conn:
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
            account.tenant_id,
        )


async def _update_conversation_timestamp(conv_id: str, account: WhatsAppAccount):
    """Update conversation's last activity timestamp."""
    async with AsyncDBConnection(account.tenant_id) as conn:
        await conn.execute(
            "UPDATE conversations SET updated_at = NOW() WHERE id = $1::uuid", conv_id
        )


async def _prepare_message_context(
    phone_number: str,
    contact_name: str,
    message_text: str,
    external_message_id: str,
    account: WhatsAppAccount,
    is_saved_contact: bool = False,
) -> tuple[str, str, str, bool]:
    """Prepare lead/conversation context in one connection.

    Returns: (lead_id, conversation_id, conversation_owner, is_db_duplicate)
    """
    msg_hash = hashlib.sha256(f"{phone_number}:{message_text}".encode()).hexdigest()

    async with AsyncDBConnection(account.tenant_id) as conn:
        # 1) Lead
        lead_row = await conn.fetchrow(
            "SELECT id, name, phone FROM wa_contacts WHERE phone = $1",
            phone_number,
        )

        if lead_row:
            lead_id = str(lead_row["id"])
        else:
            lead_id = str(uuid.uuid4())
            name = contact_name or phone_number
            # Derive channel for wa_contacts: use account's channel, defaulting to 'whatsapp'
            contact_channel = account.metadata.get("channel", "whatsapp")
            if contact_channel in (CHANNEL_BUSINESS, CHANNEL_PERSONAL):
                contact_channel = "whatsapp"
            await conn.execute(
                """
                INSERT INTO wa_contacts (id, name, phone, channel, status, tenant_id, created_at, updated_at)
                VALUES ($1::uuid, $2, $3, $4, 'active', $5::uuid, NOW(), NOW())
                """,
                lead_id,
                name,
                phone_number,
                contact_channel,
                account.tenant_id,
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
            account.tenant_id,
        )

        # 3) Conversation — scoped by channel so the same lead gets separate
        #    conversations for business vs personal WhatsApp, LinkedIn, Instagram, Gmail.
        channel = account.metadata.get("channel", CHANNEL_BUSINESS)
        # Legacy rows have channel='whatsapp' or NULL — treat as business_whatsapp
        if channel == CHANNEL_PERSONAL:
            channel_filter = "AND channel = 'personal_whatsapp'"
        elif channel in (CHANNEL_LINKEDIN, CHANNEL_INSTAGRAM, CHANNEL_GMAIL):
            channel_filter = f"AND channel = '{channel}'"
        else:
            channel_filter = "AND COALESCE(channel, 'whatsapp') IN ('whatsapp', 'business_whatsapp')"
        conv_row = await conn.fetchrow(
            f"""
            SELECT id, owner FROM conversations
            WHERE lead_id = $1::uuid AND status = 'active'
              {channel_filter}
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            lead_id,
        )

        # Auto-assign: for personal WhatsApp, check if saved contacts
        # should be routed to human agent instead of AI.
        # This applies to BOTH new and existing conversations.
        auto_assign_owner = None
        if channel == CHANNEL_PERSONAL:
            # Determine saved-contact status: use the flag from Baileys,
            # but also check the whatsapp_contacts DB table as fallback
            # (the in-memory flag may be stale after a restart).
            saved = is_saved_contact
            if not saved:
                try:
                    saved_row = await conn.fetchval(
                        """
                        SELECT 1 FROM whatsapp_contacts
                        WHERE tenant_id = $1::uuid AND phone = $2 AND name IS NOT NULL
                        """,
                        account.tenant_id,
                        phone_number,
                    )
                    if saved_row:
                        saved = True
                except Exception:
                    pass  # table may not exist yet

            if saved:
                auto_assign_row = await conn.fetchrow(
                    """
                    SELECT config FROM followup_config
                    WHERE config_key = 'auto_assign_contacts' AND tenant_id = $1::uuid
                    """,
                    account.tenant_id,
                )
                if auto_assign_row:
                    auto_cfg = auto_assign_row["config"] or {}
                    if isinstance(auto_cfg, str):
                        import json as _json
                        auto_cfg = _json.loads(auto_cfg)
                    if auto_cfg.get("enabled"):
                        auto_assign_owner = auto_cfg.get("saved_contacts_to", "human_agent")
                        logger.info(
                            f"[{account.slug}] Auto-assign: saved contact → {auto_assign_owner}",
                            extra={"phone": phone_number, "is_saved_contact": True},
                        )

        if conv_row:
            conv_id = str(conv_row["id"])
            owner = _normalize_owner(conv_row["owner"])

            # If auto-assign says human_agent but existing conversation is AI-owned,
            # update the conversation owner so the AI stops responding.
            if auto_assign_owner and auto_assign_owner != owner:
                owner = auto_assign_owner
                await conn.execute(
                    """
                    UPDATE conversations SET owner = $1, updated_at = NOW()
                    WHERE id = $2::uuid
                    """,
                    owner,
                    conv_id,
                )
                logger.info(
                    f"[{account.slug}] Updated existing conversation {conv_id} owner to {owner}",
                    extra={"phone": phone_number},
                )
        else:
            conv_id = str(uuid.uuid4())
            owner = auto_assign_owner or "AI"

            # Build conversation metadata — store channel-specific routing info
            import json as _json_mod
            conv_metadata: dict = {}
            if channel in (CHANNEL_LINKEDIN, CHANNEL_INSTAGRAM):
                unipile_conv_id = account.metadata.get("unipile_conversation_id", "")
                unipile_acct_id = account.metadata.get("unipile_account_id", "")
                if unipile_conv_id:
                    conv_metadata["unipile_conversation_id"] = unipile_conv_id
                if unipile_acct_id:
                    conv_metadata["unipile_account_id"] = unipile_acct_id
            elif channel == CHANNEL_GMAIL:
                gmail_thread = account.metadata.get("gmail_thread_id", "")
                gmail_email = account.metadata.get("gmail_account_email", "")
                subject = account.metadata.get("email_subject", "")
                if gmail_thread:
                    conv_metadata["gmail_thread_id"] = gmail_thread
                if gmail_email:
                    conv_metadata["gmail_account_email"] = gmail_email
                if subject:
                    conv_metadata["email_subject"] = subject

            await conn.execute(
                """
                INSERT INTO conversations (id, lead_id, channel, status, owner, metadata, tenant_id, started_at, updated_at)
                VALUES ($1::uuid, $2::uuid, $3, 'active', $4, $6::jsonb, $5::uuid, NOW(), NOW())
                """,
                conv_id,
                lead_id,
                channel,
                owner,
                account.tenant_id,
                _json_mod.dumps(conv_metadata),
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
            account.tenant_id,
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
    account: WhatsAppAccount,
    is_saved_contact: bool = False,
):
    """Main entry point for incoming WhatsApp messages."""
    t_start = time.time()

    # Layer 1: In-memory dedup
    if _is_duplicate(external_message_id):
        logger.debug(f"[{account.slug}] Duplicate message {external_message_id}, skipping")
        return

    # Mark message as read immediately (blue ticks) — only for business WhatsApp
    channel_val = account.metadata.get("channel", CHANNEL_BUSINESS)
    is_business_wa = channel_val not in (
        CHANNEL_PERSONAL, CHANNEL_LINKEDIN, CHANNEL_INSTAGRAM, CHANNEL_GMAIL
    )
    if is_business_wa:
        asyncio.create_task(whatsapp_client.mark_as_read(external_message_id, account=account))

    # Step 1-4: Prepare lead/conversation + dedup + save in one DB connection
    t0 = time.time()
    lead_id, conv_id, owner, dedup_result = await _prepare_message_context(
        phone_number=phone_number,
        contact_name=contact_name,
        message_text=message_text,
        external_message_id=external_message_id,
        account=account,
        is_saved_contact=is_saved_contact,
    )
    logger.info(f"[{account.slug}][TIMING] db_prepare_context_total: {time.time()-t0:.3f}s")

    if dedup_result:
        logger.debug(f"[{account.slug}] DB duplicate for lead {lead_id}, skipping")
        return

    logger.info(f"[{account.slug}][TIMING] pre-debounce total: {time.time()-t_start:.3f}s")

    # Check ownership — skip LLM if human agent owns the conversation
    if owner == "human_agent":
        logger.info(f"[{account.slug}] Human agent owns conversation {conv_id}, skipping AI")
        return

    # Debounce: buffer messages per member+channel, flush after 1s of silence.
    # Include channel in key so the same phone on personal vs business WA
    # doesn't collide in the buffer.
    channel = account.metadata.get("channel", CHANNEL_BUSINESS)
    buffer_key = f"{phone_number}:{channel}"

    if buffer_key not in _member_buffers:
        _member_buffers[buffer_key] = {"messages": [], "task": None, "account": account}

    buf = _member_buffers[buffer_key]
    buf["messages"].append(message_text)
    buf["account"] = account  # Update account ref

    # Cancel existing flush task if any
    if buf["task"] and not buf["task"].done():
        buf["task"].cancel()

    # Schedule new flush
    buf["task"] = asyncio.create_task(
        _flush_buffer(buffer_key, phone_number, lead_id, conv_id, contact_name, account)
    )


async def _flush_buffer(
    buffer_key: str, phone_number: str, lead_id: str, conv_id: str,
    contact_name: str, account: WhatsAppAccount,
):
    """Wait for debounce period, then process combined messages."""
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return

    buf = _member_buffers.get(buffer_key)
    if not buf or not buf["messages"]:
        return

    combined = " ".join(buf["messages"])
    buf["messages"].clear()

    lock = _get_member_lock(buffer_key)
    async with lock:
        try:
            logger.info(
                f"[{account.slug}] Processing {len(combined)} chars from {phone_number}",
                extra={"lead_id": lead_id, "conv_id": conv_id}
            )

            t_llm = time.time()
            reply = await process_conversation(
                phone_number=phone_number,
                lead_id=lead_id,
                conversation_id=conv_id,
                message_text=combined,
                contact_name=contact_name,
                account=account,
            )
            logger.info(f"[{account.slug}][TIMING] process_conversation (LLM pipeline): {time.time()-t_llm:.3f}s")

            if not reply:
                logger.warning(
                    f"[{account.slug}] LLM returned empty response for {phone_number}",
                    extra={"lead_id": lead_id}
                )
                # Don't send error message — just log and continue
                return

            logger.info(
                f"[{account.slug}] AI Reply ready ({len(reply)} chars): {reply[:100]}...",
                extra={"phone_number": phone_number}
            )

            t_wa = time.time()
            sent = await _send_reply(
                phone_number=phone_number,
                text=reply,
                conversation_id=conv_id,
                lead_id=lead_id,
                account=account,
            )
            logger.info(f"[{account.slug}][TIMING] whatsapp_send: {time.time()-t_wa:.3f}s, sent: {sent}")

            if not sent:
                logger.error(
                    f"[{account.slug}] Failed to send reply to {phone_number}",
                    extra={"reply_text": reply[:50]}
                )
        except Exception as e:
            logger.error(
                f"[{account.slug}] Error processing message for {phone_number}: {e}",
                exc_info=True,
                extra={"lead_id": lead_id}
            )


async def _send_reply(
    phone_number: str,
    text: str,
    conversation_id: str,
    lead_id: str,
    account: WhatsAppAccount,
) -> bool:
    """Route reply to the correct channel client.

    Channels:
      - personal_whatsapp: via LAD_backend Baileys bridge
      - business_whatsapp: via Meta Cloud API
      - linkedin: via LAD_backend → Unipile chat endpoint
      - instagram: via LAD_backend → Unipile chat endpoint
      - gmail: via LAD_backend → Gmail API thread reply

    Returns:
        True if message was sent successfully, False otherwise.
    """
    channel = account.metadata.get("channel", "business_whatsapp")
    slug = account.slug
    lad_backend_url = account.metadata.get("lad_backend_url") or None

    try:
        # ── LinkedIn ───────────────────────────────────────────────────────────
        if channel == CHANNEL_LINKEDIN:
            unipile_conv_id = account.metadata.get("unipile_conversation_id", "")
            unipile_acct_id = account.metadata.get("unipile_account_id", "")

            if not unipile_conv_id:
                # Fall back: load from conversation metadata in DB
                unipile_conv_id = await _get_conv_metadata_field(
                    conversation_id, "unipile_conversation_id", account,
                )

            if not unipile_conv_id:
                logger.error(
                    f"[{slug}] Missing unipile_conversation_id for LinkedIn reply",
                    extra={"phone_number": phone_number}
                )
                return False

            logger.info(f"[{slug}] Sending via LinkedIn channel")
            return await linkedin_client.send_message(
                unipile_conversation_id=unipile_conv_id,
                message_text=text,
                unipile_account_id=unipile_acct_id,
                lad_backend_url=lad_backend_url,
            )

        # ── Instagram ──────────────────────────────────────────────────────────
        elif channel == CHANNEL_INSTAGRAM:
            unipile_conv_id = account.metadata.get("unipile_conversation_id", "")
            unipile_acct_id = account.metadata.get("unipile_account_id", "")

            if not unipile_conv_id:
                unipile_conv_id = await _get_conv_metadata_field(
                    conversation_id, "unipile_conversation_id", account,
                )

            if not unipile_conv_id:
                logger.error(
                    f"[{slug}] Missing unipile_conversation_id for Instagram reply",
                    extra={"phone_number": phone_number}
                )
                return False

            logger.info(f"[{slug}] Sending via Instagram channel")
            return await instagram_client.send_message(
                unipile_conversation_id=unipile_conv_id,
                message_text=text,
                unipile_account_id=unipile_acct_id,
                lad_backend_url=lad_backend_url,
            )

        # ── Gmail ──────────────────────────────────────────────────────────────
        elif channel == CHANNEL_GMAIL:
            gmail_thread_id = account.metadata.get("gmail_thread_id", "")
            gmail_account_email = account.metadata.get("gmail_account_email", "")
            email_subject = account.metadata.get("email_subject", "")
            gmail_tenant_id = account.metadata.get("gmail_tenant_id", account.tenant_id)

            if not gmail_thread_id:
                gmail_thread_id = await _get_conv_metadata_field(
                    conversation_id, "gmail_thread_id", account,
                )

            if not gmail_thread_id or not gmail_account_email:
                logger.error(
                    f"[{slug}] Missing gmail_thread_id or gmail_account_email for Gmail reply",
                    extra={"phone_number": phone_number}
                )
                return False

            logger.info(f"[{slug}] Sending via Gmail channel")
            return await gmail_client.send_reply(
                gmail_thread_id=gmail_thread_id,
                message_text=text,
                gmail_account_email=gmail_account_email,
                tenant_id=gmail_tenant_id,
                subject=email_subject,
                to_email=phone_number,   # phone_number = from_email for Gmail channel
                lad_backend_url=lad_backend_url,
            )

        # ── Personal WhatsApp ──────────────────────────────────────────────────
        elif channel == CHANNEL_PERSONAL:
            personal_account_id = account.metadata.get("personal_account_id", "")

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
                account=account,
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

        # ── Business WhatsApp (default) ────────────────────────────────────────
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
                account=account,
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


async def _get_conv_metadata_field(
    conversation_id: str, field: str, account: WhatsAppAccount,
) -> str:
    """Fetch a single field from conversation.metadata JSONB."""
    import json as _json
    try:
        async with AsyncDBConnection(account.tenant_id) as conn:
            row = await conn.fetchrow(
                "SELECT metadata FROM conversations WHERE id = $1::uuid",
                conversation_id,
            )
            if row and row["metadata"]:
                meta = row["metadata"]
                if isinstance(meta, str):
                    meta = _json.loads(meta)
                return meta.get(field, "")
    except Exception as e:
        logger.error(f"[message_handler] Error fetching conv metadata: {e}")
    return ""
