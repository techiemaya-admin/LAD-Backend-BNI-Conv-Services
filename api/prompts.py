from __future__ import annotations
"""
Prompts & Chat Settings API — manage system prompts and chat configuration.

Endpoints:
  GET    /api/prompts              — list all prompts
  POST   /api/prompts              — create a prompt
  GET    /api/prompts/{name}       — get single prompt
  PUT    /api/prompts/{name}       — update prompt
  DELETE /api/prompts/{name}       — delete prompt
  GET    /api/chat-settings        — get knowledge base + campaign config
  PUT    /api/chat-settings        — update knowledge base + campaign config
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from db.connection import AsyncDBConnection
from middleware.tenant import get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["prompts"])


# ── Pydantic models ──────────────────────────────────────────────

class PromptCreate(BaseModel):
    name: str
    prompt_text: str
    channel: str = "whatsapp"
    is_active: bool = True


class PromptUpdate(BaseModel):
    prompt_text: str | None = None
    is_active: bool | None = None
    channel: str | None = None


class ChatSettingsUpdate(BaseModel):
    knowledge_base: str | None = None
    campaign_frequency: dict | None = None


# ── Helpers ───────────────────────────────────────────────────────

DEFAULT_CHAT_SETTINGS = {
    "knowledge_base": "",
    "campaign_frequency": {
        "enabled": True,
        "interval_hours": 24,
        "max_daily_messages": 50,
    },
}


async def _ensure_config_table(conn):
    """Create followup_config table if it doesn't exist."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS followup_config (
            id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
            config_key TEXT UNIQUE NOT NULL,
            config JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )


async def _get_chat_settings(conn) -> dict:
    """Load chat settings from DB, creating defaults if needed."""
    await _ensure_config_table(conn)
    row = await conn.fetchrow(
        "SELECT config FROM followup_config WHERE config_key = 'chat_settings'"
    )
    if row:
        cfg = row["config"]
        return json.loads(cfg) if isinstance(cfg, str) else cfg

    await conn.execute(
        """
        INSERT INTO followup_config (config_key, config)
        VALUES ('chat_settings', $1::jsonb)
        ON CONFLICT (config_key) DO NOTHING
        """,
        json.dumps(DEFAULT_CHAT_SETTINGS),
    )
    return dict(DEFAULT_CHAT_SETTINGS)


# ── Prompts Endpoints ─────────────────────────────────────────────

@router.get("/api/prompts")
async def list_prompts(tenant_id: Optional[str] = Depends(get_tenant_id)):
    """List all prompts."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT name, prompt_text, version, is_active, channel,
                       created_at, updated_at
                FROM prompts
                ORDER BY name
                """
            )
            prompts = [
                {
                    "name": r["name"],
                    "prompt_text": r["prompt_text"],
                    "version": r["version"],
                    "is_active": r["is_active"],
                    "channel": r["channel"] or "whatsapp",
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
                for r in rows
            ]
            return {"success": True, "data": prompts}
    except Exception as e:
        logger.error(f"Error listing prompts: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/api/prompts")
async def create_prompt(
    body: PromptCreate,
    tenant_id: Optional[str] = Depends(get_tenant_id),
):
    """Create a new prompt."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            await conn.execute(
                """
                INSERT INTO prompts (name, prompt_text, version, is_active, channel,
                                     tenant_id, created_at, updated_at)
                VALUES ($1, $2, 1, $3, $4, $5::uuid, NOW(), NOW())
                """,
                body.name,
                body.prompt_text,
                body.is_active,
                body.channel,
                tenant_id,
            )
            return {"success": True, "data": {"name": body.name}}
    except Exception as e:
        logger.error(f"Error creating prompt: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/api/prompts/{name}")
async def get_prompt(
    name: str,
    tenant_id: Optional[str] = Depends(get_tenant_id),
):
    """Get a single prompt by name."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            row = await conn.fetchrow(
                """
                SELECT name, prompt_text, version, is_active, channel,
                       created_at, updated_at
                FROM prompts WHERE name = $1
                """,
                name,
            )
            if not row:
                raise HTTPException(status_code=404, detail="Prompt not found")
            return {
                "success": True,
                "data": {
                    "name": row["name"],
                    "prompt_text": row["prompt_text"],
                    "version": row["version"],
                    "is_active": row["is_active"],
                    "channel": row["channel"] or "whatsapp",
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                },
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting prompt: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.put("/api/prompts/{name}")
async def update_prompt(
    name: str,
    body: PromptUpdate,
    tenant_id: Optional[str] = Depends(get_tenant_id),
):
    """Update a prompt's text, active status, or channel."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            # Check exists
            existing = await conn.fetchrow(
                "SELECT name FROM prompts WHERE name = $1", name
            )
            if not existing:
                raise HTTPException(status_code=404, detail="Prompt not found")

            updates = body.model_dump(exclude_none=True)
            if not updates:
                return {"success": True, "data": {"name": name, "message": "No changes"}}

            set_clauses = []
            params = []
            idx = 1

            if "prompt_text" in updates:
                set_clauses.append(f"prompt_text = ${idx}")
                params.append(updates["prompt_text"])
                idx += 1
                # Bump version
                set_clauses.append("version = COALESCE(version, 0) + 1")

            if "is_active" in updates:
                set_clauses.append(f"is_active = ${idx}")
                params.append(updates["is_active"])
                idx += 1

            if "channel" in updates:
                set_clauses.append(f"channel = ${idx}")
                params.append(updates["channel"])
                idx += 1

            set_clauses.append("updated_at = NOW()")
            params.append(name)

            query = f"UPDATE prompts SET {', '.join(set_clauses)} WHERE name = ${idx}"
            await conn.execute(query, *params)

            return {"success": True, "data": {"name": name}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating prompt: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.delete("/api/prompts/{name}")
async def delete_prompt(
    name: str,
    tenant_id: Optional[str] = Depends(get_tenant_id),
):
    """Delete a prompt."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            result = await conn.execute(
                "DELETE FROM prompts WHERE name = $1", name
            )
            if result == "DELETE 0":
                raise HTTPException(status_code=404, detail="Prompt not found")
            return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting prompt: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ── Chat Settings Endpoints ──────────────────────────────────────

@router.get("/api/chat-settings")
async def get_chat_settings(tenant_id: Optional[str] = Depends(get_tenant_id)):
    """Get knowledge base and campaign frequency config."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            config = await _get_chat_settings(conn)
            return {"success": True, "data": config}
    except Exception as e:
        logger.error(f"Error fetching chat settings: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.put("/api/chat-settings")
async def update_chat_settings(
    body: ChatSettingsUpdate,
    tenant_id: Optional[str] = Depends(get_tenant_id),
):
    """Update knowledge base and/or campaign frequency config."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            current = await _get_chat_settings(conn)
            updates = body.model_dump(exclude_none=True)
            current.update(updates)

            await conn.execute(
                """
                UPDATE followup_config
                SET config = $1::jsonb, updated_at = NOW()
                WHERE config_key = 'chat_settings'
                """,
                json.dumps(current),
            )
            return {"success": True, "data": current}
    except Exception as e:
        logger.error(f"Error updating chat settings: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
