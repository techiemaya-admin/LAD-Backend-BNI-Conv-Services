"""
Labels API — manage conversation labels (tags for organizing conversations).

Endpoints:
  GET    /api/labels                              — list all labels
  POST   /api/labels                              — create label
  DELETE /api/labels/{label_id}                    — delete label
  POST   /api/conversations/{id}/labels            — attach label
  DELETE /api/conversations/{id}/labels/{label_id} — detach label
  GET    /api/conversations/{id}/labels            — list labels for conversation
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from db.connection import AsyncDBConnection
from middleware.tenant import get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["labels"])


class LabelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    color: str = Field("#6366f1", pattern=r"^#[0-9a-fA-F]{6}$")


class LabelAttach(BaseModel):
    label_id: str


# ── Label CRUD ───────────────────────────────────────────────────

@router.get("/api/labels")
async def list_labels(tenant_id: Optional[str] = Depends(get_tenant_id)):
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            rows = await conn.fetch(
                "SELECT id, name, color, created_at FROM labels ORDER BY name"
            )
            return {
                "success": True,
                "data": [
                    {
                        "id": str(r["id"]),
                        "name": r["name"],
                        "color": r["color"],
                        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    }
                    for r in rows
                ],
            }
    except Exception as e:
        logger.error(f"Error listing labels: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/api/labels")
async def create_label(body: LabelCreate, tenant_id: Optional[str] = Depends(get_tenant_id)):
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO labels (name, color) VALUES ($1, $2)
                RETURNING id, name, color, created_at
                """,
                body.name,
                body.color,
            )
            return {
                "success": True,
                "data": {
                    "id": str(row["id"]),
                    "name": row["name"],
                    "color": row["color"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                },
            }
    except Exception as e:
        logger.error(f"Error creating label: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.delete("/api/labels/{label_id}")
async def delete_label(label_id: str, tenant_id: Optional[str] = Depends(get_tenant_id)):
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            await conn.execute("DELETE FROM labels WHERE id = $1::uuid", label_id)
            return {"success": True}
    except Exception as e:
        logger.error(f"Error deleting label: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ── Conversation–Label association ────────────────────────────────

@router.get("/api/conversations/{conversation_id}/labels")
async def get_conversation_labels(conversation_id: str, tenant_id: Optional[str] = Depends(get_tenant_id)):
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT l.id, l.name, l.color
                FROM labels l
                JOIN conversation_labels cl ON cl.label_id = l.id
                WHERE cl.conversation_id = $1::uuid
                ORDER BY l.name
                """,
                conversation_id,
            )
            return {
                "success": True,
                "data": [
                    {"id": str(r["id"]), "name": r["name"], "color": r["color"]}
                    for r in rows
                ],
            }
    except Exception as e:
        logger.error(f"Error getting conversation labels: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/api/conversations/{conversation_id}/labels")
async def attach_label(conversation_id: str, body: LabelAttach, tenant_id: Optional[str] = Depends(get_tenant_id)):
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            await conn.execute(
                """
                INSERT INTO conversation_labels (conversation_id, label_id)
                VALUES ($1::uuid, $2::uuid)
                ON CONFLICT DO NOTHING
                """,
                conversation_id,
                body.label_id,
            )
            return {"success": True}
    except Exception as e:
        logger.error(f"Error attaching label: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.delete("/api/conversations/{conversation_id}/labels/{label_id}")
async def detach_label(conversation_id: str, label_id: str, tenant_id: Optional[str] = Depends(get_tenant_id)):
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            await conn.execute(
                """
                DELETE FROM conversation_labels
                WHERE conversation_id = $1::uuid AND label_id = $2::uuid
                """,
                conversation_id,
                label_id,
            )
            return {"success": True}
    except Exception as e:
        logger.error(f"Error detaching label: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
