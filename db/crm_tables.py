"""
Auto-create CRM feature tables (labels, quick_replies, conversation_notes).
Also adds new columns to the conversations table.
Called once on startup.
"""
import logging

from db.connection import ClientDBConnection

logger = logging.getLogger(__name__)


async def ensure_crm_tables():
    """Create CRM tables if they don't exist."""
    try:
        async with ClientDBConnection() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS labels (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    name VARCHAR(100) NOT NULL,
                    color VARCHAR(7) NOT NULL DEFAULT '#6366f1',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS conversation_labels (
                    conversation_id UUID NOT NULL,
                    label_id UUID NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
                    PRIMARY KEY (conversation_id, label_id)
                );

                CREATE TABLE IF NOT EXISTS quick_replies (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    title VARCHAR(200) NOT NULL,
                    shortcut VARCHAR(50),
                    content TEXT NOT NULL,
                    category VARCHAR(100),
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS conversation_notes (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    conversation_id UUID NOT NULL,
                    lead_id UUID,
                    content TEXT NOT NULL,
                    author_name VARCHAR(200),
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS chat_groups (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    name VARCHAR(100) NOT NULL,
                    color VARCHAR(7) NOT NULL DEFAULT '#6366f1',
                    description TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS chat_group_conversations (
                    group_id UUID NOT NULL REFERENCES chat_groups(id) ON DELETE CASCADE,
                    conversation_id UUID NOT NULL,
                    added_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (group_id, conversation_id)
                );
            """)

            # Add columns to conversations if they don't exist
            for col, col_type, default in [
                ("is_favorite", "BOOLEAN", "false"),
                ("is_pinned", "BOOLEAN", "false"),
                ("is_locked", "BOOLEAN", "false"),
                ("is_deleted", "BOOLEAN", "false"),
            ]:
                await conn.execute(f"""
                    DO $$
                    BEGIN
                        ALTER TABLE conversations ADD COLUMN {col} {col_type} DEFAULT {default};
                    EXCEPTION
                        WHEN duplicate_column THEN NULL;
                    END $$;
                """)

            logger.info("CRM tables ensured")
    except Exception as e:
        logger.error(f"Error ensuring CRM tables: {e}", exc_info=True)
