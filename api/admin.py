"""
Admin API

Endpoints for managing WhatsApp accounts and tenant onboarding.
Supports both generic clients and BNI-specific chapters.
Protected endpoints (should be behind auth in production).

Backward-compatible: /admin/chapters still works as alias.
"""
from __future__ import annotations

import asyncpg
import logging
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from db.connection import CoreDBConnection, reload_tenant_config
from middleware.tenant import get_tenant_id
from services.account_registry import (
    reload_accounts as reload_chapters,
    get_all_active_accounts as get_all_active_chapters,
    get_accounts_for_tenant,
    get_account_by_slug as get_chapter_by_slug,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

_CONFIG_DB_URL = os.getenv(
    "CONFIG_DB_URL",
    os.getenv("AGENT_DB_URL", "postgresql://dbadmin:TechieMaya@165.22.221.77:5432/salesmaya_agent"),
)


# ====================
# Request Models
# ====================

class WhatsAppAccountCreateRequest(BaseModel):
    """Request to register a new WhatsApp account for a tenant."""
    display_name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=50, pattern=r"^[a-z0-9-]+$")
    database_url: str = Field(..., min_length=10)
    tenant_id: Optional[str] = None  # If None, creates a new tenant
    phone_number_id: Optional[str] = None
    access_token: Optional[str] = None
    business_account_id: Optional[str] = None
    verify_token: Optional[str] = None
    ai_model: str = "gemini-2.5-flash"
    ai_api_key: Optional[str] = None
    timezone: str = "UTC"
    conversation_flow_template: str = "generic"


class WhatsAppAccountUpdateRequest(BaseModel):
    """Request to update a WhatsApp account."""
    display_name: Optional[str] = Field(None, max_length=255)
    phone_number_id: Optional[str] = None
    access_token: Optional[str] = None
    business_account_id: Optional[str] = None
    verify_token: Optional[str] = None
    ai_model: Optional[str] = None
    ai_api_key: Optional[str] = None
    timezone: Optional[str] = None
    conversation_flow_template: Optional[str] = None
    status: Optional[str] = Field(None, pattern=r"^(active|inactive)$")


# Backward compat aliases
ChapterCreateRequest = WhatsAppAccountCreateRequest
ChapterUpdateRequest = WhatsAppAccountUpdateRequest


# ====================
# WhatsApp Account CRUD
# ====================

@router.get("/whatsapp-accounts")
@router.get("/chapters")  # backward compat
async def list_accounts(tenant_id: Optional[str] = Depends(get_tenant_id)):
    """List WhatsApp accounts, filtered to the current tenant when X-Tenant-ID is present."""
    if tenant_id:
        accounts = get_accounts_for_tenant(tenant_id)
    else:
        accounts = get_all_active_chapters()

    return {
        "success": True,
        "data": [
            {
                "id": a.id,
                "tenant_id": a.tenant_id,
                "slug": a.slug,
                "display_name": getattr(a, "display_name", a.name),
                "ai_model": a.ai_model,
                "timezone": a.timezone,
                "status": a.status,
                "conversation_flow_template": getattr(a, "conversation_flow_template", "bni"),
                "phone_number_id": a.whatsapp_phone_number_id,
                "business_account_id": a.whatsapp_business_account_id,
            }
            for a in accounts
        ],
        "total": len(accounts),
    }


@router.post("/whatsapp-accounts")
@router.post("/chapters")  # backward compat
async def create_account(body: WhatsAppAccountCreateRequest):
    """Register a new WhatsApp account.

    Creates:
    1. Tenant in lad_dev.tenants (if tenant_id not provided)
    2. Row in lad_dev.social_whatsapp_accounts
    3. Row in lad_dev.tenant_database_config (DB routing)
    4. Required tables in the tenant's database
    5. Default prompts for the selected flow template
    """
    existing = get_chapter_by_slug(body.slug)
    if existing:
        raise HTTPException(400, f"Account with slug '{body.slug}' already exists")

    tenant_id = body.tenant_id or str(uuid.uuid4())

    try:
        async with CoreDBConnection() as conn:
            # Create tenant if needed
            if not body.tenant_id:
                await conn.execute(
                    """
                    INSERT INTO lad_dev.tenants (id, name, is_active)
                    VALUES ($1::uuid, $2, true)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    tenant_id, body.display_name,
                )

            # Insert WhatsApp account config
            row = await conn.fetchrow(
                """
                INSERT INTO lad_dev.social_whatsapp_accounts (
                    tenant_id, slug, display_name,
                    phone_number_id, access_token,
                    business_account_id, verify_token,
                    ai_model, ai_api_key, timezone,
                    conversation_flow_template, status
                ) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'active')
                RETURNING id, tenant_id, slug, display_name, created_at
                """,
                tenant_id, body.slug, body.display_name,
                body.phone_number_id, body.access_token,
                body.business_account_id, body.verify_token,
                body.ai_model, body.ai_api_key, body.timezone,
                body.conversation_flow_template,
            )

            # Insert tenant database routing
            await conn.execute(
                """
                INSERT INTO lad_dev.tenant_database_config (tenant_id, database_url)
                VALUES ($1::uuid, $2)
                ON CONFLICT (tenant_id) DO UPDATE SET database_url = $2
                """,
                tenant_id, body.database_url,
            )

        # Create required tables in the tenant's database
        await _ensure_tenant_tables(body.database_url, tenant_id, body.conversation_flow_template)

        # Seed default prompts
        await _seed_prompts_for_flow(
            body.database_url, tenant_id,
            body.conversation_flow_template, body.display_name,
        )

        # Reload caches
        await reload_tenant_config()
        await reload_chapters()

        logger.info(f"WhatsApp account created: {body.slug} (tenant: {tenant_id}, flow: {body.conversation_flow_template})")

        return {
            "success": True,
            "data": {
                "id": str(row["id"]),
                "tenant_id": str(row["tenant_id"]),
                "slug": row["slug"],
                "display_name": row["display_name"],
                "conversation_flow_template": body.conversation_flow_template,
                "webhook_url": f"/webhook/{body.slug}",
                "created_at": row["created_at"].isoformat(),
            },
            "message": f"Account '{body.display_name}' created. Configure Meta webhook to: /webhook/{body.slug}",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating account: {e}")
        raise HTTPException(500, f"Failed to create account: {str(e)}")


@router.patch("/whatsapp-accounts/{slug}")
@router.patch("/chapters/{slug}")  # backward compat
async def update_account(slug: str, body: WhatsAppAccountUpdateRequest):
    """Update WhatsApp account configuration."""
    account = get_chapter_by_slug(slug)
    if not account:
        raise HTTPException(404, f"Account '{slug}' not found")

    updates = body.dict(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")

    try:
        set_clauses = []
        params = []
        idx = 1

        for field_name, value in updates.items():
            set_clauses.append(f"{field_name} = ${idx}")
            params.append(value)
            idx += 1

        set_clauses.append("updated_at = NOW()")
        params.append(account.tenant_id)

        async with CoreDBConnection() as conn:
            await conn.execute(
                f"""
                UPDATE lad_dev.social_whatsapp_accounts
                SET {', '.join(set_clauses)}
                WHERE tenant_id = ${idx}::uuid
                """,
                *params,
            )

        await reload_chapters()
        logger.info(f"Account updated: {slug}")

        return {"success": True, "message": f"Account '{slug}' updated"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating account: {e}")
        raise HTTPException(500, "Failed to update account")


@router.delete("/whatsapp-accounts/{slug}")
@router.delete("/chapters/{slug}")  # backward compat
async def deactivate_account(slug: str):
    """Deactivate a WhatsApp account (soft delete)."""
    account = get_chapter_by_slug(slug)
    if not account:
        raise HTTPException(404, f"Account '{slug}' not found")

    try:
        async with CoreDBConnection() as conn:
            await conn.execute(
                """
                UPDATE lad_dev.social_whatsapp_accounts
                SET status = 'inactive', updated_at = NOW()
                WHERE tenant_id = $1::uuid
                """,
                account.tenant_id,
            )

        await reload_chapters()
        logger.info(f"Account deactivated: {slug}")

        return {"success": True, "message": f"Account '{slug}' deactivated"}

    except Exception as e:
        logger.error(f"Error deactivating account: {e}")
        raise HTTPException(500, "Failed to deactivate account")


# ====================
# Seed Prompts
# ====================

@router.post("/whatsapp-accounts/{slug}/seed-prompts")
@router.post("/chapters/{slug}/seed-prompts")  # backward compat
async def seed_account_prompts(slug: str):
    """Seed default prompts into an account's database."""
    account = get_chapter_by_slug(slug)
    if not account:
        raise HTTPException(404, f"Account '{slug}' not found")

    try:
        from db.connection import AsyncDBConnection

        flow_template = getattr(account, "conversation_flow_template", "bni")

        async with AsyncDBConnection(account.tenant_id) as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM prompts WHERE tenant_id = $1::uuid",
                account.tenant_id,
            )
            if count and count > 0:
                return {
                    "success": True,
                    "message": f"Account already has {count} prompts",
                    "prompt_count": count,
                }

        display_name = getattr(account, "display_name", account.name)
        from db.connection import _resolve_tenant_db_url
        db_url = _resolve_tenant_db_url(account.tenant_id)
        inserted = await _seed_prompts_for_flow(
            db_url, account.tenant_id, flow_template, display_name,
        )

        return {
            "success": True,
            "message": f"Seeded {inserted} prompts for '{display_name}' (flow: {flow_template})",
            "prompt_count": inserted,
        }

    except Exception as e:
        logger.error(f"Error seeding prompts for {slug}: {e}")
        raise HTTPException(500, "Failed to seed prompts")


# ====================
# Reload Cache
# ====================

@router.post("/whatsapp-accounts/reload")
@router.post("/chapters/reload")  # backward compat
async def reload_cache():
    """Force reload account configs and tenant DB routing."""
    await reload_tenant_config()
    await reload_chapters()
    accounts = get_all_active_chapters()
    return {
        "success": True,
        "message": f"Reloaded {len(accounts)} accounts",
        "accounts": [a.slug for a in accounts],
    }


# ====================
# Helper Functions
# ====================

async def _ensure_tenant_tables(database_url: str, tenant_id: str, flow_template: str = "generic"):
    """Create required tables in a new tenant's database.

    Creates generic tables for all tenants, plus flow-specific tables
    (e.g., scheduled_meetings for BNI flow).
    """
    conn = await asyncpg.connect(database_url)
    try:
        # ---- Generic tables (all tenants) ----
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                organization_id UUID,
                name VARCHAR(200),
                phone VARCHAR(50),
                email VARCHAR(200),
                company VARCHAR(255),
                channel VARCHAR(50) DEFAULT 'whatsapp',
                stage VARCHAR(100),
                status VARCHAR(50) DEFAULT 'active',
                source VARCHAR(100),
                metadata JSONB DEFAULT '{}',
                tenant_id UUID NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);
            CREATE INDEX IF NOT EXISTS idx_leads_tenant ON leads(tenant_id);

            CREATE TABLE IF NOT EXISTS conversations (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                lead_id UUID REFERENCES leads(id),
                channel VARCHAR(50) DEFAULT 'whatsapp',
                status VARCHAR(50) DEFAULT 'active',
                owner VARCHAR(50) DEFAULT 'AI',
                human_agent_id VARCHAR(100),
                started_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                metadata JSONB DEFAULT '{}',
                is_favorite BOOLEAN DEFAULT false,
                is_pinned BOOLEAN DEFAULT false,
                is_locked BOOLEAN DEFAULT false,
                is_deleted BOOLEAN DEFAULT false,
                tenant_id UUID NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conversations_lead ON conversations(lead_id);
            CREATE INDEX IF NOT EXISTS idx_conversations_tenant ON conversations(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_conversations_channel ON conversations(channel);

            CREATE TABLE IF NOT EXISTS messages (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                conversation_id UUID REFERENCES conversations(id),
                lead_id UUID REFERENCES leads(id),
                role VARCHAR(50) NOT NULL,
                content TEXT,
                intent VARCHAR(100),
                message_status VARCHAR(50) DEFAULT 'sent',
                external_message_id VARCHAR(200),
                tenant_id UUID NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_messages_tenant ON messages(tenant_id);
        """)

        # ---- Schema migrations for existing databases ----
        # Safe ALTER TABLE ADD COLUMN IF NOT EXISTS for new columns
        migration_sqls = [
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS company VARCHAR(255)",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS stage VARCHAR(100)",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS source VARCHAR(100)",
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS channel VARCHAR(50) DEFAULT 'whatsapp'",
        ]
        for sql in migration_sqls:
            try:
                await conn.execute(sql)
            except Exception:
                pass  # Column may already exist

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_channel ON conversations(channel);

            CREATE TABLE IF NOT EXISTS conversation_states (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                lead_id UUID,
                phone VARCHAR(50) NOT NULL,
                contact_name VARCHAR(200),
                context_status VARCHAR(100) DEFAULT 'greeting',
                profile_data JSONB DEFAULT '{}',
                metadata JSONB DEFAULT '{}',
                tenant_id UUID NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_cs_phone ON conversation_states(phone);
            CREATE INDEX IF NOT EXISTS idx_cs_tenant ON conversation_states(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_cs_status ON conversation_states(context_status);

            CREATE TABLE IF NOT EXISTS prompts (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name VARCHAR(100) NOT NULL,
                prompt_text TEXT NOT NULL,
                flow_template VARCHAR(100) DEFAULT 'generic',
                version INTEGER DEFAULT 1,
                is_active BOOLEAN DEFAULT true,
                channel VARCHAR(50) DEFAULT 'whatsapp',
                tenant_id UUID NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(name, tenant_id)
            );
            CREATE INDEX IF NOT EXISTS idx_prompts_tenant ON prompts(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_prompts_flow ON prompts(flow_template);

            CREATE TABLE IF NOT EXISTS processed_messages (
                lead_id UUID,
                message_hash VARCHAR(64),
                processed_at TIMESTAMPTZ DEFAULT NOW(),
                tenant_id UUID NOT NULL,
                PRIMARY KEY (lead_id, message_hash, processed_at)
            );

            -- CRM tables
            CREATE TABLE IF NOT EXISTS labels (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name VARCHAR(100) NOT NULL,
                color VARCHAR(7) DEFAULT '#6366f1',
                tenant_id UUID,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS conversation_labels (
                conversation_id UUID NOT NULL,
                label_id UUID NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
                tenant_id UUID,
                PRIMARY KEY (conversation_id, label_id)
            );

            CREATE TABLE IF NOT EXISTS quick_replies (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                title VARCHAR(200) NOT NULL,
                shortcut VARCHAR(50),
                content TEXT NOT NULL,
                category VARCHAR(100),
                tenant_id UUID,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS conversation_notes (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                conversation_id UUID NOT NULL,
                lead_id UUID,
                content TEXT NOT NULL,
                author_name VARCHAR(200),
                tenant_id UUID,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS chat_groups (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name VARCHAR(100) NOT NULL,
                color VARCHAR(7) DEFAULT '#6366f1',
                description TEXT,
                tenant_id UUID,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS chat_group_conversations (
                group_id UUID NOT NULL REFERENCES chat_groups(id) ON DELETE CASCADE,
                conversation_id UUID NOT NULL,
                tenant_id UUID,
                added_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (group_id, conversation_id)
            );

            CREATE TABLE IF NOT EXISTS followup_config (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                config_key TEXT NOT NULL,
                config JSONB NOT NULL DEFAULT '{}',
                tenant_id UUID,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (config_key, tenant_id)
            );
        """)

        # ---- BNI-specific tables (only for BNI flow) ----
        if flow_template == "bni":
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS member_conversation_manager (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    lead_id UUID,
                    member_phone VARCHAR(50),
                    member_name VARCHAR(200),
                    first_name VARCHAR(100),
                    last_name VARCHAR(100),
                    context_status VARCHAR(100) DEFAULT 'onboarding_greeting',
                    company_name VARCHAR(300),
                    industry VARCHAR(200),
                    designation VARCHAR(200),
                    services_offered TEXT,
                    ideal_customer_profile TEXT,
                    chat_summary TEXT,
                    metadata JSONB DEFAULT '{}',
                    tenant_id UUID NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_mcm_phone ON member_conversation_manager(member_phone);
                CREATE INDEX IF NOT EXISTS idx_mcm_tenant ON member_conversation_manager(tenant_id);

                CREATE TABLE IF NOT EXISTS scheduled_meetings (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    member_a_phone VARCHAR(50),
                    member_a_name VARCHAR(200),
                    member_b_phone VARCHAR(50),
                    member_b_name VARCHAR(200),
                    status VARCHAR(50) DEFAULT 'pending',
                    member_a_slots JSONB,
                    member_b_slots JSONB,
                    proposed_time TIMESTAMPTZ,
                    confirmed_time TIMESTAMPTZ,
                    member_a_confirmed BOOLEAN DEFAULT false,
                    member_b_confirmed BOOLEAN DEFAULT false,
                    tenant_id UUID NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_meetings_tenant ON scheduled_meetings(tenant_id);

                CREATE TABLE IF NOT EXISTS meeting_reminders (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    meeting_id UUID REFERENCES scheduled_meetings(id),
                    member_phone VARCHAR(50),
                    reminder_type VARCHAR(50),
                    scheduled_time TIMESTAMPTZ,
                    sent BOOLEAN DEFAULT false,
                    tenant_id UUID NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_reminders_tenant ON meeting_reminders(tenant_id);

                -- Prompts table (shared schema across all tenants)
                CREATE TABLE IF NOT EXISTS prompts (
                    name VARCHAR(100) PRIMARY KEY,
                    prompt_text TEXT NOT NULL,
                    version INTEGER DEFAULT 1,
                    is_active BOOLEAN DEFAULT true,
                    channel VARCHAR(50),
                    tenant_id UUID NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)

        logger.info(f"Tenant tables created for {tenant_id} (flow: {flow_template})")
    finally:
        await conn.close()


async def _seed_prompts_for_flow(
    database_url: str,
    tenant_id: str,
    flow_template: str,
    display_name: str,
) -> int:
    """Seed default prompts into a tenant's database based on flow template."""
    if flow_template == "bni":
        prompts = _get_bni_prompts(display_name)
    else:
        prompts = _get_generic_prompts(display_name)

    conn = await asyncpg.connect(database_url)
    try:
        inserted = 0
        for name, text in prompts.items():
            await conn.execute(
                """
                INSERT INTO prompts (name, prompt_text, flow_template, version, is_active, tenant_id)
                VALUES ($1, $2, $3, 1, true, $4::uuid)
                ON CONFLICT (name, tenant_id) DO NOTHING
                """,
                name, text, flow_template, tenant_id,
            )
            inserted += 1
        logger.info(f"Seeded {inserted} prompts for tenant {tenant_id} (flow: {flow_template})")
        return inserted
    finally:
        await conn.close()


def _get_generic_prompts(display_name: str) -> dict[str, str]:
    """Return generic prompt templates for any industry client."""
    return {
        "GREETING": f"""You are the {display_name} AI Assistant on WhatsApp.
Your tone is professional, warm, and helpful.

This person just messaged for the first time.

Conversation history:
{{conversation_json}}

Contact info:
{{context_json}}

INSTRUCTIONS:
- Greet them by name if available
- Introduce yourself briefly: you are the AI assistant for {display_name}
- Ask how you can help them today
- Keep it to 2-3 sentences max
- Do NOT use emojis

Respond ONLY with valid JSON: {{"agent_reply": "your message", "info_gathering_fields": {{"context_status": "active"}}}}""",

        "ACTIVE": f"""You are the {display_name} AI Assistant on WhatsApp.
Help the person with their query professionally.

Conversation history:
{{conversation_json}}

Contact info:
{{context_json}}

INSTRUCTIONS:
- Answer the person's question directly and helpfully
- Keep responses concise (2-3 sentences)
- If you don't know, say so honestly
- If the conversation seems complete, set context_status to "idle"
- Do NOT use emojis

Respond ONLY with valid JSON: {{"agent_reply": "your message", "info_gathering_fields": {{"context_status": "active"}}}}""",

        "IDLE": f"""You are the {display_name} AI Assistant on WhatsApp.
The person is returning after a previous conversation.

Conversation history:
{{conversation_json}}

Contact info:
{{context_json}}

INSTRUCTIONS:
- Welcome them back briefly
- Ask how you can help
- Set context_status to "active"
- Do NOT use emojis

Respond ONLY with valid JSON: {{"agent_reply": "your message", "info_gathering_fields": {{"context_status": "active"}}}}""",
    }


def _get_bni_prompts(chapter_name: str) -> dict[str, str]:
    """Return BNI-specific prompt templates."""
    return {
        "ONBOARDING_GREETING": f"""You are the {chapter_name} AI Networking Assistant on WhatsApp.
Your tone is professional, warm, and respectful — like a trusted chapter colleague reaching out.

This member just messaged for the first time.

Conversation history:
{{conversation_json}}

Member info:
{{context_json}}

INSTRUCTIONS:
- Greet the member by first name if available
- Introduce yourself in one clear line: you help {chapter_name} members identify the right referrals and coordinate 1-to-1 introductions
- Let them know you would like to set up their profile so you can find the right matches
- End with a simple, professional question to begin
- Keep it to 3 sentences max — concise and respectful
- Do NOT use emojis

TONE: Professional and warm. Think business WhatsApp message, not casual chat.

Respond ONLY with valid JSON: {{"agent_reply": "your message", "info_gathering_fields": {{}}}}""",

        "GENERAL_QA": f"""You are the {chapter_name} AI Networking Assistant.
Help the member with their query professionally.

Conversation history:
{{conversation_json}}

Member info:
{{context_json}}

INSTRUCTIONS:
- Answer the member's question directly and helpfully
- Keep responses concise (2-3 sentences)
- If you don't know, say so honestly
- Do NOT use emojis

Respond ONLY with valid JSON: {{"agent_reply": "your message", "info_gathering_fields": {{}}}}""",
    }
