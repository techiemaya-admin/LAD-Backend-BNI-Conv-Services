-- Migration: Create social_whatsapp_accounts table (replaces chapters for generic multi-tenant)
-- This table stores per-WhatsApp-account configuration including credentials,
-- AI model preferences, and conversation flow template selection.
-- Lives in shared DB (salesmaya_agent) alongside lad_dev.tenants.

-- =============================================
-- 1. Shared table: social_whatsapp_accounts
-- =============================================

CREATE TABLE IF NOT EXISTS lad_dev.social_whatsapp_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES lad_dev.tenants(id),
    slug VARCHAR(50) NOT NULL UNIQUE,
    display_name VARCHAR(255) NOT NULL,
    phone_number_id VARCHAR(100),
    access_token TEXT,
    business_account_id VARCHAR(100),
    verify_token VARCHAR(100),
    ai_model VARCHAR(100) DEFAULT 'gemini-2.5-flash',
    ai_api_key TEXT,
    timezone VARCHAR(50) DEFAULT 'UTC',
    conversation_flow_template VARCHAR(100) DEFAULT 'generic',
    status VARCHAR(20) DEFAULT 'active',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_swa_tenant ON lad_dev.social_whatsapp_accounts(tenant_id);
CREATE INDEX IF NOT EXISTS idx_swa_slug ON lad_dev.social_whatsapp_accounts(slug);
CREATE INDEX IF NOT EXISTS idx_swa_phone ON lad_dev.social_whatsapp_accounts(phone_number_id);
CREATE INDEX IF NOT EXISTS idx_swa_status ON lad_dev.social_whatsapp_accounts(status);

-- Migrate existing Rising Phoenix data from chapters table
INSERT INTO lad_dev.social_whatsapp_accounts (
    tenant_id, slug, display_name,
    phone_number_id, access_token,
    business_account_id, verify_token,
    ai_model, ai_api_key, timezone,
    conversation_flow_template, status, metadata
)
SELECT
    tenant_id, slug, name,
    whatsapp_phone_number_id, whatsapp_access_token,
    whatsapp_business_account_id, whatsapp_verify_token,
    ai_model, ai_api_key, timezone,
    'bni', status, metadata
FROM lad_dev.chapters
WHERE status = 'active'
ON CONFLICT (slug) DO NOTHING;


-- =============================================
-- 2. Per-tenant generic table: conversation_states
--    (replaces bni_conversation_manager)
--    Run this in EACH tenant's database.
-- =============================================

-- NOTE: This DDL is used by _ensure_tenant_tables() in api/admin.py
-- when creating tables for a new tenant. For existing tenants,
-- run this manually or via the admin seed endpoint.

-- CREATE TABLE IF NOT EXISTS conversation_states (
--     id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
--     lead_id UUID,
--     phone VARCHAR(50) NOT NULL,
--     contact_name VARCHAR(200),
--     context_status VARCHAR(100) DEFAULT 'greeting',
--     profile_data JSONB DEFAULT '{}',
--     metadata JSONB DEFAULT '{}',
--     tenant_id UUID NOT NULL,
--     created_at TIMESTAMPTZ DEFAULT NOW(),
--     updated_at TIMESTAMPTZ DEFAULT NOW()
-- );
-- CREATE UNIQUE INDEX IF NOT EXISTS idx_cs_phone ON conversation_states(phone);
-- CREATE INDEX IF NOT EXISTS idx_cs_tenant ON conversation_states(tenant_id);
-- CREATE INDEX IF NOT EXISTS idx_cs_status ON conversation_states(context_status);


-- =============================================
-- 3. Per-tenant generic table: prompts
--    (replaces bni_prompts)
-- =============================================

-- CREATE TABLE IF NOT EXISTS prompts (
--     id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
--     name VARCHAR(100) NOT NULL,
--     prompt_text TEXT NOT NULL,
--     flow_template VARCHAR(100) DEFAULT 'generic',
--     version INTEGER DEFAULT 1,
--     is_active BOOLEAN DEFAULT true,
--     channel VARCHAR(50) DEFAULT 'whatsapp',
--     tenant_id UUID NOT NULL,
--     created_at TIMESTAMPTZ DEFAULT NOW(),
--     updated_at TIMESTAMPTZ DEFAULT NOW(),
--     UNIQUE(name, tenant_id)
-- );
-- CREATE INDEX IF NOT EXISTS idx_prompts_tenant ON prompts(tenant_id);
-- CREATE INDEX IF NOT EXISTS idx_prompts_flow ON prompts(flow_template);
