-- ================================================================
-- Copy tenant_database_config table from salesmaya_agent.lad_dev 
-- to salesmaya_bni.public
-- ================================================================

-- Step 1: Connect to salesmaya_agent and export the table structure and data
-- Run this first to see the current data:
\c salesmaya_agent
SELECT * FROM lad_dev.tenant_database_config;

-- Step 2: Connect to salesmaya_bni and create the table
\c salesmaya_bni

-- Drop existing table if it exists (be careful!)
DROP TABLE IF EXISTS public.tenant_database_config CASCADE;

-- Create the tenant_database_config table in public schema
CREATE TABLE public.tenant_database_config (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL UNIQUE,
    database_url TEXT NOT NULL,
    schema_name TEXT DEFAULT 'public',
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'::jsonb
);

-- Create indexes for performance
CREATE INDEX idx_tenant_database_config_tenant_id ON public.tenant_database_config(tenant_id);
CREATE INDEX idx_tenant_database_config_is_active ON public.tenant_database_config(is_active);

-- Add comments
COMMENT ON TABLE public.tenant_database_config IS 'Maps tenant IDs to their respective database connection strings for multi-tenancy';
COMMENT ON COLUMN public.tenant_database_config.tenant_id IS 'UUID of the tenant (must match tenant_id in auth system)';
COMMENT ON COLUMN public.tenant_database_config.database_url IS 'PostgreSQL connection string for this tenant';

-- Step 3: Copy data from salesmaya_agent.lad_dev to salesmaya_bni.public
-- We'll use dblink extension for cross-database queries
CREATE EXTENSION IF NOT EXISTS dblink;

-- Insert data from source database (replace connection string with actual values)
INSERT INTO public.tenant_database_config (
    id,
    tenant_id,
    database_url,
    schema_name,
    is_active,
    created_at,
    updated_at,
    metadata
)
SELECT 
    id,
    tenant_id,
    database_url,
    schema_name,
    is_active,
    created_at,
    updated_at,
    metadata
FROM dblink(
    'host=165.22.221.77 port=5432 dbname=salesmaya_agent user=dbadmin password=TechieMaya',
    'SELECT id, tenant_id, database_url, schema_name, is_active, created_at, updated_at, metadata FROM lad_dev.tenant_database_config'
) AS t(
    id UUID,
    tenant_id UUID,
    database_url TEXT,
    schema_name TEXT,
    is_active BOOLEAN,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    metadata JSONB
);

-- Verify the data was copied
SELECT 
    tenant_id,
    database_url,
    schema_name,
    is_active,
    created_at
FROM public.tenant_database_config
ORDER BY created_at DESC;

-- Show count
SELECT COUNT(*) as total_tenants FROM public.tenant_database_config;
