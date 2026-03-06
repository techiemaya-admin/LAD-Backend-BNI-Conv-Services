"""
Conversation repository — data access for conversations, messages, leads.

All SQL queries for the conversation lifecycle live here.
Database: salesmaya_bni (client DB).
"""
import json
import logging
import uuid

from db.connection import ClientDBConnection
from db.schema import get_tenant_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

async def find_lead_by_phone(phone_number: str) -> dict | None:
    """Look up a lead by phone number."""
    async with ClientDBConnection() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, phone FROM leads WHERE phone = $1", phone_number
        )
        return dict(row) if row else None


async def create_lead(lead_id: str, name: str, phone: str) -> None:
    """Create a new lead record."""
    tenant_id = get_tenant_id()
    async with ClientDBConnection() as conn:
        await conn.execute(
            """
            INSERT INTO leads (id, organization_id, name, phone, channel, status, created_at, updated_at)
            VALUES ($1::uuid, $2::uuid, $3, $4, 'whatsapp', 'active', NOW(), NOW())
            """,
            lead_id,
            tenant_id,
            name,
            phone,
        )


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

async def find_active_conversation(lead_id: str) -> dict | None:
    """Find the most recent active conversation for a lead."""
    async with ClientDBConnection() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, lead_id, owner FROM conversations
            WHERE lead_id = $1::uuid AND status = 'active'
            ORDER BY updated_at DESC LIMIT 1
            """,
            lead_id,
        )
        return {"id": str(row["id"]), "lead_id": str(row["lead_id"]), "owner": row["owner"] or "AI"} if row else None


async def create_conversation(conv_id: str, lead_id: str) -> dict:
    """Create a new conversation."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            """
            INSERT INTO conversations (id, lead_id, status, owner, metadata, started_at, updated_at)
            VALUES ($1::uuid, $2::uuid, 'active', 'AI', '{}', NOW(), NOW())
            """,
            conv_id,
            lead_id,
        )
    return {"id": conv_id, "lead_id": lead_id, "owner": "AI"}


async def update_conversation_timestamp(conv_id: str) -> None:
    """Touch the updated_at timestamp on a conversation."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            "UPDATE conversations SET updated_at = NOW() WHERE id = $1::uuid", conv_id
        )


async def update_conversation_owner(conv_id: str, owner: str) -> None:
    """Update the owner of a conversation."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            "UPDATE conversations SET owner = $1, updated_at = NOW() WHERE id = $2::uuid",
            owner,
            conv_id,
        )


async def update_conversation_status(conv_id: str, status: str) -> None:
    """Update conversation status."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            "UPDATE conversations SET status = $1, updated_at = NOW() WHERE id = $2::uuid",
            status,
            conv_id,
        )


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

async def save_message(conversation_id: str, lead_id: str, role: str,
                       content: str, message_status: str,
                       external_message_id: str | None = None) -> str:
    """Save a message and return its ID."""
    msg_id = str(uuid.uuid4())
    async with ClientDBConnection() as conn:
        await conn.execute(
            """
            INSERT INTO messages (id, conversation_id, lead_id, role, content,
                message_status, external_message_id, created_at)
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, NOW())
            """,
            msg_id,
            conversation_id,
            lead_id,
            role,
            content,
            message_status,
            external_message_id,
        )
    return msg_id


async def get_recent_messages(conversation_id: str, limit: int = 10) -> list[dict]:
    """Get recent messages for a conversation, ordered by created_at DESC."""
    async with ClientDBConnection() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content, created_at FROM messages
            WHERE conversation_id = $1::uuid
            ORDER BY created_at DESC LIMIT $2
            """,
            conversation_id,
            limit,
        )
        return [dict(r) for r in rows]


async def update_message_status(external_message_id: str, status: str) -> bool:
    """Update message delivery status. Returns True if a row was updated."""
    async with ClientDBConnection() as conn:
        result = await conn.execute(
            """
            UPDATE messages SET message_status = $1
            WHERE external_message_id = $2
              AND message_status != 'read'
            """,
            status,
            external_message_id,
        )
        return "UPDATE 1" in result


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

async def check_message_dedup(lead_id: str, message_hash: str) -> bool:
    """Check if a message was recently processed (30s window). Returns True if duplicate."""
    async with ClientDBConnection() as conn:
        existing = await conn.fetchval(
            """
            SELECT 1 FROM processed_messages
            WHERE lead_id = $1 AND message_hash = $2
            AND processed_at > NOW() - INTERVAL '30 seconds'
            """,
            lead_id,
            message_hash,
        )
        if existing:
            return True
        await conn.execute(
            "INSERT INTO processed_messages (lead_id, message_hash) VALUES ($1, $2)",
            lead_id,
            message_hash,
        )
        return False


# ---------------------------------------------------------------------------
# Conversation Manager (BNI state machine)
# ---------------------------------------------------------------------------

async def load_conversation_state(phone_number: str) -> dict | None:
    """Load conversation state from bni_conversation_manager."""
    async with ClientDBConnection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM bni_conversation_manager WHERE member_phone = $1",
            phone_number,
        )
        if row:
            result = dict(row)
            if isinstance(result.get("metadata"), str):
                try:
                    result["metadata"] = json.loads(result["metadata"])
                except Exception:
                    result["metadata"] = {}
            return result
        return None


async def create_conversation_state(state_id: str, lead_id: str, phone: str,
                                     name: str, status: str,
                                     company_name: str | None, industry: str | None,
                                     designation: str | None,
                                     services_offered: str | None) -> None:
    """Create initial conversation state row."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            """
            INSERT INTO bni_conversation_manager
                (id, lead_id, member_phone, member_name, context_status,
                 company_name, industry, designation, services_offered,
                 metadata, created_at, updated_at)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, '{}', NOW(), NOW())
            ON CONFLICT (member_phone) DO NOTHING
            """,
            state_id,
            lead_id,
            phone,
            name,
            status,
            company_name,
            industry,
            designation,
            services_offered,
        )


async def update_conversation_state(phone_number: str, status: str,
                                     company_name: str | None, industry: str | None,
                                     designation: str | None, services_offered: str | None,
                                     ideal_customer_profile: str | None) -> None:
    """Update profile fields and context status."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            """
            UPDATE bni_conversation_manager SET
                context_status = $1,
                company_name = COALESCE($2, company_name),
                industry = COALESCE($3, industry),
                designation = COALESCE($4, designation),
                services_offered = COALESCE($5, services_offered),
                ideal_customer_profile = COALESCE($6, ideal_customer_profile),
                updated_at = NOW()
            WHERE member_phone = $7
            """,
            status,
            company_name,
            industry,
            designation,
            services_offered,
            ideal_customer_profile,
            phone_number,
        )


async def merge_conversation_metadata(phone_number: str, metadata_patch: dict) -> None:
    """Merge a JSON patch into the metadata column."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            """
            UPDATE bni_conversation_manager
            SET metadata = COALESCE(metadata, '{}')::jsonb || $1::jsonb
            WHERE member_phone = $2
            """,
            json.dumps(metadata_patch),
            phone_number,
        )


async def set_conversation_status_and_metadata(phone_number: str, status: str,
                                                 metadata_key: str,
                                                 metadata_value) -> None:
    """Set context_status and a specific metadata key."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            f"""
            UPDATE bni_conversation_manager
            SET context_status = $1,
                metadata = jsonb_set(
                    COALESCE(metadata, '{{}}')::jsonb,
                    '{{{metadata_key}}}',
                    $2::jsonb
                ),
                updated_at = NOW()
            WHERE member_phone = $3
            """,
            status,
            json.dumps(metadata_value),
            phone_number,
        )


async def set_metadata_key(phone_number: str, key: str, value) -> None:
    """Set a single key in the metadata JSONB column."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            f"""
            UPDATE bni_conversation_manager
            SET metadata = jsonb_set(
                COALESCE(metadata, '{{}}')::jsonb,
                '{{{key}}}',
                $1::jsonb
            ),
            updated_at = NOW()
            WHERE member_phone = $2
            """,
            json.dumps(value),
            phone_number,
        )


async def set_context_status(phone_number: str, status: str) -> None:
    """Update just the context_status."""
    async with ClientDBConnection() as conn:
        await conn.execute(
            """
            UPDATE bni_conversation_manager
            SET context_status = $1, updated_at = NOW()
            WHERE member_phone = $2
            """,
            status,
            phone_number,
        )


async def get_conversation_metadata(phone_number: str) -> dict | None:
    """Load just the metadata column for a member."""
    async with ClientDBConnection() as conn:
        row = await conn.fetchrow(
            "SELECT metadata FROM bni_conversation_manager WHERE member_phone = $1",
            phone_number,
        )
        if not row:
            return None
        meta = row["metadata"]
        if isinstance(meta, str):
            try:
                return json.loads(meta)
            except Exception:
                return {}
        return meta if isinstance(meta, dict) else {}
