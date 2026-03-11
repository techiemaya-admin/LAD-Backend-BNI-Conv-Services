-- Migration: Create chapters table for multi-tenant chapter management
-- This table stores per-chapter configuration including WhatsApp credentials,
-- AI model preferences, and metadata.

CREATE TABLE IF NOT EXISTS lad_dev.chapters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL UNIQUE,
    slug VARCHAR(50) NOT NULL UNIQUE,
    name VARCHAR(200) NOT NULL,
    whatsapp_phone_number_id VARCHAR(50),
    whatsapp_access_token TEXT,
    whatsapp_business_account_id VARCHAR(50),
    whatsapp_verify_token VARCHAR(100),
    ai_model VARCHAR(50) DEFAULT 'gemini-2.5-flash',
    ai_api_key TEXT,
    timezone VARCHAR(50) DEFAULT 'Asia/Dubai',
    status VARCHAR(20) DEFAULT 'active',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chapters_slug ON lad_dev.chapters(slug);
CREATE INDEX IF NOT EXISTS idx_chapters_tenant_id ON lad_dev.chapters(tenant_id);
CREATE INDEX IF NOT EXISTS idx_chapters_status ON lad_dev.chapters(status);

-- Seed Rising Phoenix chapter (existing data)
INSERT INTO lad_dev.chapters (
    tenant_id, slug, name,
    whatsapp_phone_number_id, whatsapp_access_token,
    whatsapp_business_account_id, whatsapp_verify_token,
    ai_model, timezone, status
) VALUES (
    '9ca4012a-2e02-5593-8cc1-fd5bd81483f9',
    'rising-phoenix',
    'BNI Rising Phoenix',
    '569691699566732',
    'EAATwYo9R44cBPFZBijKZBGrwjh1khdVkJV5tuNjTqU7MRdN1YdKyq6yeuGFDHoMyM0kmQW7L8mBWmJMoKdiYEogw4LZBkrOnS5UTMtaAbuZABJmn8qeJgvu4m9ycZA1V1p2dwfYzH34JOZB7XGyUHGVIwP2YTP3t1iX4gkgtxB5W0xaBGXzb7ABGXAdGYcitxh0wZDZD',
    '1285734192513402',
    'BNI_Rising_Phoenix_2026',
    'gemini-2.5-flash',
    'Asia/Dubai',
    'active'
) ON CONFLICT (tenant_id) DO NOTHING;
