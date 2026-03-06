-- Migration 001: Add tenant_id to all BNI feature tables
-- Database: salesmaya_bni
-- Run: psql $BNI_DB_URL -f db/migrations/001_add_tenant_id.sql

BEGIN;

-- BNI Tenant ID (Rising Phoenix chapter)
DO $$ BEGIN
    RAISE NOTICE 'Adding tenant_id to BNI feature tables...';
END $$;

-- 1. leads
ALTER TABLE leads ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE leads SET tenant_id = '9ca4012a-2e02-5593-8cc1-fd5bd81483f9' WHERE tenant_id IS NULL;
ALTER TABLE leads ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE leads ALTER COLUMN tenant_id SET DEFAULT '9ca4012a-2e02-5593-8cc1-fd5bd81483f9';
CREATE INDEX IF NOT EXISTS idx_leads_tenant_id ON leads (tenant_id);
CREATE INDEX IF NOT EXISTS idx_leads_tenant_phone ON leads (tenant_id, phone);

-- 2. conversations
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE conversations SET tenant_id = '9ca4012a-2e02-5593-8cc1-fd5bd81483f9' WHERE tenant_id IS NULL;
ALTER TABLE conversations ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE conversations ALTER COLUMN tenant_id SET DEFAULT '9ca4012a-2e02-5593-8cc1-fd5bd81483f9';
CREATE INDEX IF NOT EXISTS idx_conversations_tenant_id ON conversations (tenant_id);
CREATE INDEX IF NOT EXISTS idx_conversations_tenant_lead ON conversations (tenant_id, lead_id);

-- 3. messages
ALTER TABLE messages ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE messages SET tenant_id = '9ca4012a-2e02-5593-8cc1-fd5bd81483f9' WHERE tenant_id IS NULL;
ALTER TABLE messages ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE messages ALTER COLUMN tenant_id SET DEFAULT '9ca4012a-2e02-5593-8cc1-fd5bd81483f9';
CREATE INDEX IF NOT EXISTS idx_messages_tenant_id ON messages (tenant_id);
CREATE INDEX IF NOT EXISTS idx_messages_tenant_conversation ON messages (tenant_id, conversation_id);

-- 4. bni_conversation_manager
ALTER TABLE bni_conversation_manager ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE bni_conversation_manager SET tenant_id = '9ca4012a-2e02-5593-8cc1-fd5bd81483f9' WHERE tenant_id IS NULL;
ALTER TABLE bni_conversation_manager ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE bni_conversation_manager ALTER COLUMN tenant_id SET DEFAULT '9ca4012a-2e02-5593-8cc1-fd5bd81483f9';
CREATE INDEX IF NOT EXISTS idx_bni_cm_tenant_id ON bni_conversation_manager (tenant_id);
CREATE INDEX IF NOT EXISTS idx_bni_cm_tenant_phone ON bni_conversation_manager (tenant_id, member_phone);

-- 5. scheduled_meetings
ALTER TABLE scheduled_meetings ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE scheduled_meetings SET tenant_id = '9ca4012a-2e02-5593-8cc1-fd5bd81483f9' WHERE tenant_id IS NULL;
ALTER TABLE scheduled_meetings ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE scheduled_meetings ALTER COLUMN tenant_id SET DEFAULT '9ca4012a-2e02-5593-8cc1-fd5bd81483f9';
CREATE INDEX IF NOT EXISTS idx_scheduled_meetings_tenant_id ON scheduled_meetings (tenant_id);

-- 6. meeting_reminders
ALTER TABLE meeting_reminders ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE meeting_reminders SET tenant_id = '9ca4012a-2e02-5593-8cc1-fd5bd81483f9' WHERE tenant_id IS NULL;
ALTER TABLE meeting_reminders ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE meeting_reminders ALTER COLUMN tenant_id SET DEFAULT '9ca4012a-2e02-5593-8cc1-fd5bd81483f9';
CREATE INDEX IF NOT EXISTS idx_meeting_reminders_tenant_id ON meeting_reminders (tenant_id);

-- 7. processed_messages
ALTER TABLE processed_messages ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE processed_messages SET tenant_id = '9ca4012a-2e02-5593-8cc1-fd5bd81483f9' WHERE tenant_id IS NULL;
ALTER TABLE processed_messages ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE processed_messages ALTER COLUMN tenant_id SET DEFAULT '9ca4012a-2e02-5593-8cc1-fd5bd81483f9';
CREATE INDEX IF NOT EXISTS idx_processed_messages_tenant_id ON processed_messages (tenant_id);

-- 8. bni_prompts
ALTER TABLE bni_prompts ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE bni_prompts SET tenant_id = '9ca4012a-2e02-5593-8cc1-fd5bd81483f9' WHERE tenant_id IS NULL;
ALTER TABLE bni_prompts ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE bni_prompts ALTER COLUMN tenant_id SET DEFAULT '9ca4012a-2e02-5593-8cc1-fd5bd81483f9';
CREATE INDEX IF NOT EXISTS idx_bni_prompts_tenant_id ON bni_prompts (tenant_id);

-- Add metadata JSONB column where missing
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'leads' AND column_name = 'metadata') THEN
        ALTER TABLE leads ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}';
    END IF;
END $$;
ALTER TABLE conversations ALTER COLUMN metadata SET DEFAULT '{}';

DO $$ BEGIN
    RAISE NOTICE 'Migration 001 complete: tenant_id added to all BNI feature tables';
END $$;

COMMIT;
