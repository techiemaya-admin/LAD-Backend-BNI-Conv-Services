"""
Ownership API

Transfer conversation ownership between AI and human agents.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from db.connection import AsyncDBConnection, TenantNotConfiguredError
from middleware.tenant import get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ownership"])


class OwnershipTransferRequest(BaseModel):
    conversation_id: str
    new_owner: str  # 'AI' or 'human_agent'
    human_agent_id: Optional[str] = None


def _normalize_owner_input(raw_owner: Optional[str]) -> Optional[str]:
    val = (raw_owner or "").strip().lower()
    if val in {"ai", "agent", "bot"}:
        return "AI"
    if val in {"human_agent", "human", "human-agent"}:
        return "human_agent"
    return None


@router.patch("/ownership")
async def transfer_ownership(
    request: OwnershipTransferRequest,
    tenant_id: Optional[str] = Depends(get_tenant_id),
):
    """Transfer conversation ownership."""
    normalized_owner = _normalize_owner_input(request.new_owner)
    if normalized_owner not in ("AI", "human_agent"):
        raise HTTPException(status_code=400, detail="new_owner must be 'AI' or 'human_agent'")

    if normalized_owner == "human_agent" and not request.human_agent_id:
        raise HTTPException(status_code=400, detail="human_agent_id required for human_agent ownership")

    try:
        async with AsyncDBConnection(tenant_id) as conn:
            conv = await conn.fetchrow(
                "SELECT id FROM conversations WHERE id = $1",
                request.conversation_id,
            )
            if not conv:
                raise HTTPException(status_code=404, detail="Conversation not found")

            await conn.execute(
                """
                UPDATE conversations
                SET owner = $1, human_agent_id = $2, updated_at = CURRENT_TIMESTAMP
                WHERE id = $3
                """,
                normalized_owner,
                request.human_agent_id if normalized_owner == "human_agent" else None,
                request.conversation_id,
            )

        return {
            "success": True,
            "data": {
                "conversation_id": request.conversation_id,
                "owner": normalized_owner,
                "human_agent_id": request.human_agent_id,
            },
        }

    except HTTPException:
        raise
    except TenantNotConfiguredError:
        raise HTTPException(status_code=403, detail="No database configured for this tenant")
    except Exception as e:
        logger.error(f"Error transferring ownership: {e}")
        raise HTTPException(status_code=500, detail="Failed to transfer ownership")
