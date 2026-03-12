"""
Generic prompt loader.

Loads prompts from the per-tenant `prompts` table.
Falls back to a generic default if DB is unavailable.

The flow template's status_to_prompt mapping determines which prompt
name to load for a given context_status (handled by flow_registry).
"""
from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from db.connection import AsyncDBConnection

if TYPE_CHECKING:
    from services.account_registry import WhatsAppAccount

logger = logging.getLogger(__name__)

FALLBACK_PROMPT = (
    "You are a helpful WhatsApp business assistant.\n"
    "\n"
    "CRITICAL INSTRUCTION: You MUST respond with valid JSON. Your response must ALWAYS contain \"agent_reply\" with a non-empty message.\n"
    "\n"
    "Conversation history:\n{conversation_json}\n\n"
    "Contact info:\n{context_json}\n\n"
    "IMPORTANT: Always respond with this exact JSON format:\n"
    '{{"agent_reply": "Your helpful message here", "info_gathering_fields": {{"context_status": "active"}}}}\n\n'
    "Do NOT include any text outside the JSON object.\n"
    "The agent_reply field MUST contain a non-empty, helpful response message."
)


async def get_prompt(name: str, account: Optional[WhatsAppAccount] = None) -> str:
    """Load a prompt by name from the tenant's prompts table.

    Always reads from DB so edits take effect immediately.
    Falls back to generic prompt if DB unavailable.
    """
    tenant_id = account.tenant_id if account else None

    if tenant_id:
        try:
            async with AsyncDBConnection(tenant_id) as conn:
                row = await conn.fetchrow(
                    "SELECT prompt_text FROM prompts WHERE name = $1 AND is_active = true AND tenant_id = $2::uuid",
                    name,
                    tenant_id,
                )
                if row:
                    return row["prompt_text"]

                # Fallback: try without tenant_id filter (for shared prompts)
                row = await conn.fetchrow(
                    "SELECT prompt_text FROM prompts WHERE name = $1 AND is_active = true",
                    name,
                )
                if row:
                    return row["prompt_text"]
        except Exception as e:
            logger.warning(f"Could not load prompt '{name}' from prompts table: {e}")

    logger.warning(f"Prompt '{name}' not found — using generic fallback")
    return FALLBACK_PROMPT


def get_prompt_name_for_status(context_status: str, flow_template: str = "generic") -> str:
    """Map a context_status to the prompt name using the flow template.

    Kept for backward compatibility — the conversation engine now uses
    flow.get_prompt_name() directly.
    """
    from services.flow_registry import get_flow
    flow = get_flow(flow_template)
    return flow.get_prompt_name(context_status)


def clear_prompt_cache():
    """Clear the in-memory prompt cache (no-op now, reads are always live)."""
    logger.info("Prompt cache cleared (no-op)")
