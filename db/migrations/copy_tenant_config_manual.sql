-- ================================================================
-- MANUAL STEP-BY-STEP: Copy tenant_database_config 
-- FROM: salesmaya_agent.lad_dev
-- TO:   salesmaya_bni.public
-- ================================================================

-- ============================================================
-- STEP 1: Check source data in salesmaya_agent
-- ============================================================
\c salesmaya_agent

SELECT 
    tenant_id,
    database_url,
    schema_name,
    is_active,
    created_at
FROM lad_dev.tenant_database_config
ORDER BY created_at DESC;

-- Count records
SELECT COUNT(*) as total_records FROM lad_dev.tenant_database_config;

-- ============================================================
-- STEP 2: Create table in salesmaya_bni.public
-- ============================================================
\c salesmaya_bni

-- Create the table with same structure
CREATE TABLE IF NOT EXISTS public.tenant_database_config (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL UNIQUE,
    database_url TEXT NOT NULL,
    schema_name TEXT DEFAULT 'public',
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'::jsonb
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_tenant_database_config_tenant_id 
    ON public.tenant_database_config(tenant_id);
    
CREATE INDEX IF NOT EXISTS idx_tenant_database_config_is_active 
    ON public.tenant_database_config(is_active);

-- ============================================================
-- STEP 3: Manually copy the data
-- ============================================================
-- Option A: If you have only a few records, copy them manually
-- Replace the values below with actual data from STEP 1

-- Example INSERT (replace with your actual data):
-- INSERT INTO public.tenant_database_config (
--     id,
--     tenant_id,
--     database_url,
--     schema_name,
--     is_active,
--     created_at,
--     updated_at,
--     metadata
-- ) VALUES (
--     'your-id-uuid',
--     'your-tenant-uuid',
--     'postgresql://user:pass@host:5432/dbname',
--     'public',
--     true,
--     NOW(),
--     NOW(),
--     '{}'::jsonb
-- );

-- ============================================================
-- STEP 4: Verify the copy
-- ============================================================
\c salesmaya_bni

SELECT 
    tenant_id,
    database_url,
    schema_name,
    is_active,
    created_at
FROM public.tenant_database_config
ORDER BY created_at DESC;

-- Verify count matches
SELECT COUNT(*) as total_records FROM public.tenant_database_config;

-- ============================================================
-- NOTES
-- ============================================================
-- If you have many records, use one of these methods instead:
--
-- METHOD 1: Use the bash script
--   ./copy_tenant_config.sh
--
-- METHOD 2: Use pg_dump/psql commands
--   pg_dump -h 165.22.221.77 -U dbadmin -d salesmaya_agent \
--     -t lad_dev.tenant_database_config --clean --if-exists \
--     | sed 's/lad_dev\./public./g' \
--     | psql -h 165.22.221.77 -U dbadmin -d salesmaya_bni
--
-- METHOD 3: Use the SQL script with dblink
--   psql -h 165.22.221.77 -U dbadmin -d salesmaya_bni \
--     -f copy_tenant_config.sql
-- ============================================================
