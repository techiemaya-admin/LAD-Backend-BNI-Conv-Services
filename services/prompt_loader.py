"""
Load prompts from the bni_prompts table.
Falls back to a generic default if DB is unavailable.
"""
import logging
from db.connection import ClientDBConnection

logger = logging.getLogger(__name__)

# In-memory cache
_prompt_cache: dict[str, str] = {}

# Map context_status to prompt name
STATUS_TO_PROMPT = {
    "onboarding_greeting": "ONBOARDING_GREETING",
    "onboarding_profile": "ONBOARDING_PROFILE",
    "icp_discovery": "ICP_DISCOVERY",
    "onboarding_complete": "ONBOARDING_COMPLETE",
    "match_suggested": "MATCH_SUGGESTION",
    "coordination_a_availability": "COORDINATION_AVAILABILITY",
    "coordination_b_availability": "COORDINATION_AVAILABILITY",
    "coordination_overlap_proposed": "COORDINATION_AVAILABILITY",
    "post_meeting_followup": "POST_MEETING_FOLLOWUP",
    "kpi_query": "KPI_QUERY",
    "general_qa": "GENERAL_QA",
    "idle": "IDLE",
}

FALLBACK_PROMPT = (
    "You are the BNI Rising Phoenix AI Networking Assistant. "
    "Answer the member's question helpfully.\n\n"
    "Conversation history:\n{conversation_json}\n\n"
    "Return JSON:\n"
    '{{\"agent_reply\": \"your answer\", \"info_gathering_fields\": {{\"context_status\": \"idle\"}}}}'
)


async def get_prompt(name: str) -> str:
    """Load a prompt by name. Always reads from DB so edits take effect immediately."""
    try:
        async with ClientDBConnection() as conn:
            row = await conn.fetchrow(
                "SELECT prompt_text FROM bni_prompts WHERE name = $1 AND is_active = true",
                name,
            )
            if row:
                return row["prompt_text"]
    except Exception as e:
        logger.warning(f"Could not load prompt '{name}' from DB: {e}")

    logger.warning(f"Prompt '{name}' not found in DB — using generic fallback")
    return FALLBACK_PROMPT


def get_prompt_name_for_status(context_status: str) -> str:
    """Map a context_status to the prompt name."""
    return STATUS_TO_PROMPT.get(context_status, "GENERAL_QA")


def clear_prompt_cache():
    """Clear the in-memory prompt cache so prompts are re-read from DB."""
    _prompt_cache.clear()
    logger.info("Prompt cache cleared")
