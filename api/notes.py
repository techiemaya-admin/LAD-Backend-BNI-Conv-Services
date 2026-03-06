"""
Notes API — manage conversation notes.

Endpoints:
  GET    /api/conversations/{id}/notes  — list notes for conversation
  POST   /api/conversations/{id}/notes  — create note
  PUT    /api/notes/{id}                — update note
  DELETE /api/notes/{id}                — delete note
"""
import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from db.connection import ClientDBConnection

logger = logging.getLogger(__name__)

router = APIRouter(tags=["notes"])


class NoteCreate(BaseModel):
    content: str = Field(..., min_length=1)
    author_name: str | None = None
    lead_id: str | None = None


class NoteUpdate(BaseModel):
    content: str = Field(..., min_length=1)


def _row_to_dict(r) -> dict:
    return {
        "id": str(r["id"]),
        "conversation_id": str(r["conversation_id"]),
        "lead_id": str(r["lead_id"]) if r["lead_id"] else None,
        "content": r["content"],
        "author_name": r["author_name"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


@router.get("/api/conversations/{conversation_id}/notes")
async def list_notes(conversation_id: str):
    try:
        async with ClientDBConnection() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM conversation_notes
                WHERE conversation_id = $1::uuid
                ORDER BY created_at DESC
                """,
                conversation_id,
            )
            return {"success": True, "data": [_row_to_dict(r) for r in rows]}
    except Exception as e:
        logger.error(f"Error listing notes: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/api/conversations/{conversation_id}/notes")
async def create_note(conversation_id: str, body: NoteCreate):
    try:
        async with ClientDBConnection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO conversation_notes (conversation_id, lead_id, content, author_name)
                VALUES ($1::uuid, $2::uuid, $3, $4)
                RETURNING *
                """,
                conversation_id,
                body.lead_id,
                body.content,
                body.author_name or "Agent",
            )
            return {"success": True, "data": _row_to_dict(row)}
    except Exception as e:
        logger.error(f"Error creating note: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.put("/api/notes/{note_id}")
async def update_note(note_id: str, body: NoteUpdate):
    try:
        async with ClientDBConnection() as conn:
            row = await conn.fetchrow(
                """
                UPDATE conversation_notes
                SET content = $1, updated_at = NOW()
                WHERE id = $2::uuid
                RETURNING *
                """,
                body.content,
                note_id,
            )
            if not row:
                return {"success": False, "error": "Note not found"}
            return {"success": True, "data": _row_to_dict(row)}
    except Exception as e:
        logger.error(f"Error updating note: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.delete("/api/notes/{note_id}")
async def delete_note(note_id: str):
    try:
        async with ClientDBConnection() as conn:
            await conn.execute(
                "DELETE FROM conversation_notes WHERE id = $1::uuid", note_id
            )
            return {"success": True}
    except Exception as e:
        logger.error(f"Error deleting note: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
