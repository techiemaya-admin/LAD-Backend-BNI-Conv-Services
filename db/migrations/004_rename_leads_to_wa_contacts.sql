-- Migration 004: Rename leads → wa_contacts + add core_lead_id + drop organization_id
-- Run in EACH tenant's per-tenant database (NOT in salesmaya_agent).
-- Safe to run multiple times (idempotent via IF NOT EXISTS / ON CONFLICT).
--
-- What this does:
--   1. Creates wa_contacts with correct architecture (no organization_id, adds core_lead_id + is_deleted)
--   2. Migrates all existing rows from leads (same UUIDs → conversations.lead_id still valid)
--   3. Drops FK constraint on conversations.lead_id (referenced leads, now archived)
--   4. Adds new FK from conversations.lead_id → wa_contacts(id)
--   5. Renames leads → leads_archived_v1 (data preserved, not dropped)
-- =============================================

-- Step 1: Create wa_contacts with correct structure
-- =============================================
CREATE TABLE IF NOT EXISTS wa_contacts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL,
    name            VARCHAR(200),
    phone           VARCHAR(50),
    email           VARCHAR(200),
    company         VARCHAR(255),
    channel         VARCHAR(50)  DEFAULT 'whatsapp',
    stage           VARCHAR(100),
    status          VARCHAR(50)  DEFAULT 'active',
    source          VARCHAR(100),
    core_lead_id    UUID NULL,                          -- nullable cross-DB ref to lad_dev.leads
    metadata        JSONB NOT NULL DEFAULT '{}',
    is_deleted      BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Step 2: Migrate existing data from leads → wa_contacts (same UUIDs)
-- =============================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'leads'
    ) THEN
        INSERT INTO wa_contacts (
            id, tenant_id, name, phone, email, company,
            channel, stage, status, source, metadata,
            is_deleted, created_at, updated_at
        )
        SELECT
            id, tenant_id, name, phone, email,
            COALESCE(company, NULL),
            COALESCE(channel, 'whatsapp'),
            stage, COALESCE(status, 'active'), source,
            COALESCE(metadata, '{}'),
            false,
            created_at, updated_at
        FROM leads
        ON CONFLICT (id) DO NOTHING;

        RAISE NOTICE 'Migrated rows from leads → wa_contacts';
    END IF;
END $$;

-- Step 3: Indexes on wa_contacts
-- =============================================
CREATE INDEX IF NOT EXISTS idx_wa_contacts_phone    ON wa_contacts(phone)    WHERE is_deleted = false;
CREATE INDEX IF NOT EXISTS idx_wa_contacts_tenant   ON wa_contacts(tenant_id);
CREATE INDEX IF NOT EXISTS idx_wa_contacts_email    ON wa_contacts(email)    WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_wa_contacts_core     ON wa_contacts(core_lead_id) WHERE core_lead_id IS NOT NULL;

-- Step 4: Update conversations FK (leads(id) → wa_contacts(id))
-- Drop all FK constraints on conversations (only lead_id ref existed)
-- then re-add pointing to wa_contacts.
-- =============================================
DO $$
DECLARE
    r RECORD;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'conversations'
    ) THEN
        FOR r IN
            SELECT tc.constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
            WHERE tc.table_name = 'conversations'
              AND tc.constraint_type = 'FOREIGN KEY'
              AND kcu.column_name = 'lead_id'
        LOOP
            EXECUTE 'ALTER TABLE conversations DROP CONSTRAINT ' || quote_ident(r.constraint_name);
            RAISE NOTICE 'Dropped FK % from conversations', r.constraint_name;
        END LOOP;

        -- Add new FK → wa_contacts
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.table_constraints
            WHERE constraint_name = 'fk_conversations_wa_contact'
        ) THEN
            ALTER TABLE conversations
                ADD CONSTRAINT fk_conversations_wa_contact
                FOREIGN KEY (lead_id) REFERENCES wa_contacts(id);
            RAISE NOTICE 'Added FK conversations.lead_id → wa_contacts(id)';
        END IF;
    END IF;
END $$;

-- Step 5: Archive old leads table (rename, do NOT drop — preserves data)
-- =============================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'leads'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'leads_archived_v1'
    ) THEN
        ALTER TABLE leads RENAME TO leads_archived_v1;
        RAISE NOTICE 'Renamed leads → leads_archived_v1 (data preserved)';
    END IF;
END $$;

-- Step 6: Add core_lead_id to wa_contacts if this migration was previously
-- run without that column (handles partial previous runs)
-- =============================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'wa_contacts' AND column_name = 'core_lead_id'
    ) THEN
        ALTER TABLE wa_contacts ADD COLUMN core_lead_id UUID NULL;
        CREATE INDEX IF NOT EXISTS idx_wa_contacts_core
            ON wa_contacts(core_lead_id) WHERE core_lead_id IS NOT NULL;
        RAISE NOTICE 'Added core_lead_id column to wa_contacts';
    END IF;
END $$;

-- Step 7: Add is_deleted to wa_contacts if missing
-- =============================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'wa_contacts' AND column_name = 'is_deleted'
    ) THEN
        ALTER TABLE wa_contacts ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT false;
        RAISE NOTICE 'Added is_deleted column to wa_contacts';
    END IF;
END $$;
