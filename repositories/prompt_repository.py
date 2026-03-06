"""
Prompt repository — data access for bni_prompts table.
"""
import logging

from db.connection import ClientDBConnection

logger = logging.getLogger(__name__)


async def get_active_prompt(name: str) -> str | None:
    """Load an active prompt by name. Returns the prompt_text or None."""
    async with ClientDBConnection() as conn:
        row = await conn.fetchrow(
            "SELECT prompt_text FROM bni_prompts WHERE name = $1 AND is_active = true",
            name,
        )
        return row["prompt_text"] if row else None
