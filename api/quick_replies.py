from __future__ import annotations
"""
Quick Replies API — manage canned/template responses for agents.

Endpoints:
  GET    /api/quick-replies       — list all
  POST   /api/quick-replies       — create
  PUT    /api/quick-replies/{id}  — update
  DELETE /api/quick-replies/{id}  — delete
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from db.connection import AsyncDBConnection
from middleware.tenant import get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/quick-replies", tags=["quick-replies"])


class QuickReplyCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    shortcut: str | None = Field(None, max_length=50)
    content: str = Field(..., min_length=1)
    category: str | None = Field(None, max_length=100)


class QuickReplyUpdate(BaseModel):
    title: str | None = Field(None, max_length=200)
    shortcut: str | None = Field(None, max_length=50)
    content: str | None = None
    category: str | None = Field(None, max_length=100)


def _row_to_dict(r) -> dict:
    return {
        "id": str(r["id"]),
        "title": r["title"],
        "shortcut": r["shortcut"],
        "content": r["content"],
        "category": r["category"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


@router.get("")
async def list_quick_replies(tenant_id: Optional[str] = Depends(get_tenant_id)):
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            rows = await conn.fetch(
                "SELECT * FROM quick_replies ORDER BY category NULLS LAST, title"
            )
            return {"success": True, "data": [_row_to_dict(r) for r in rows]}
    except Exception as e:
        logger.error(f"Error listing quick replies: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("")
async def create_quick_reply(body: QuickReplyCreate, tenant_id: Optional[str] = Depends(get_tenant_id)):
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO quick_replies (title, shortcut, content, category)
                VALUES ($1, $2, $3, $4)
                RETURNING *
                """,
                body.title,
                body.shortcut,
                body.content,
                body.category,
            )
            return {"success": True, "data": _row_to_dict(row)}
    except Exception as e:
        logger.error(f"Error creating quick reply: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.put("/{reply_id}")
async def update_quick_reply(reply_id: str, body: QuickReplyUpdate, tenant_id: Optional[str] = Depends(get_tenant_id)):
    try:
        updates = body.model_dump(exclude_none=True)
        if not updates:
            return {"success": False, "error": "No fields to update"}

        async with AsyncDBConnection(tenant_id) as conn:
            # Build dynamic SET clause
            set_parts = []
            params = []
            idx = 1
            for key, value in updates.items():
                set_parts.append(f"{key} = ${idx}")
                params.append(value)
                idx += 1
            set_parts.append("updated_at = NOW()")
            params.append(reply_id)

            row = await conn.fetchrow(
                f"""
                UPDATE quick_replies
                SET {', '.join(set_parts)}
                WHERE id = ${idx}::uuid
                RETURNING *
                """,
                *params,
            )
            if not row:
                return {"success": False, "error": "Quick reply not found"}
            return {"success": True, "data": _row_to_dict(row)}
    except Exception as e:
        logger.error(f"Error updating quick reply: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.delete("/{reply_id}")
async def delete_quick_reply(reply_id: str, tenant_id: Optional[str] = Depends(get_tenant_id)):
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            await conn.execute(
                "DELETE FROM quick_replies WHERE id = $1::uuid", reply_id
            )
            return {"success": True}
    except Exception as e:
        logger.error(f"Error deleting quick reply: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
