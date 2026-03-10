"""
Chat Groups API — create groups, tag conversations, and broadcast templates.

Endpoints:
  GET    /api/chat-groups                                    — list all groups (with counts)
  POST   /api/chat-groups                                    — create group
  PUT    /api/chat-groups/{group_id}                         — update group
  DELETE /api/chat-groups/{group_id}                         — delete group
  GET    /api/chat-groups/{group_id}/conversations           — list conversation IDs in group
  POST   /api/chat-groups/{group_id}/conversations           — add conversations to group
  DELETE /api/chat-groups/{group_id}/conversations/{conv_id} — remove conversation from group
  POST   /api/chat-groups/{group_id}/send-template           — send template to entire group
"""
import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from db.connection import ClientDBConnection
from services.whatsapp_client import send_template_message

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat-groups"])


# ── Models ────────────────────────────────────────────────────────

class ChatGroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    color: str = Field("#6366f1", pattern=r"^#[0-9a-fA-F]{6}$")
    description: str | None = None


class ChatGroupUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    description: str | None = None


class ChatGroupAddConversations(BaseModel):
    conversation_ids: list[str]


class ChatGroupTemplateSend(BaseModel):
    template_name: str
    language_code: str = "en_GB"
    parameters: list[str] | None = None


# ── Group CRUD ────────────────────────────────────────────────────

@router.get("/api/chat-groups")
async def list_chat_groups():
    try:
        async with ClientDBConnection() as conn:
            rows = await conn.fetch("""
                SELECT g.id, g.name, g.color, g.description, g.created_at,
                       COUNT(cgc.conversation_id) AS conversation_count
                FROM chat_groups g
                LEFT JOIN chat_group_conversations cgc ON cgc.group_id = g.id
                GROUP BY g.id
                ORDER BY g.name
            """)
            return {
                "success": True,
                "data": [
                    {
                        "id": str(r["id"]),
                        "name": r["name"],
                        "color": r["color"],
                        "description": r["description"],
                        "conversation_count": r["conversation_count"],
                        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    }
                    for r in rows
                ],
            }
    except Exception as e:
        logger.error(f"Error listing chat groups: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/api/chat-groups")
async def create_chat_group(body: ChatGroupCreate):
    try:
        async with ClientDBConnection() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO chat_groups (name, color, description)
                VALUES ($1, $2, $3)
                RETURNING id, name, color, description, created_at
                """,
                body.name,
                body.color,
                body.description,
            )
            return {
                "success": True,
                "data": {
                    "id": str(row["id"]),
                    "name": row["name"],
                    "color": row["color"],
                    "description": row["description"],
                    "conversation_count": 0,
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                },
            }
    except Exception as e:
        logger.error(f"Error creating chat group: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.put("/api/chat-groups/{group_id}")
async def update_chat_group(group_id: str, body: ChatGroupUpdate):
    try:
        updates = body.model_dump(exclude_none=True)
        if not updates:
            return {"success": False, "error": "No fields to update"}

        async with ClientDBConnection() as conn:
            set_parts = []
            params = []
            idx = 1
            for key, value in updates.items():
                set_parts.append(f"{key} = ${idx}")
                params.append(value)
                idx += 1
            params.append(group_id)

            row = await conn.fetchrow(
                f"""
                UPDATE chat_groups
                SET {', '.join(set_parts)}
                WHERE id = ${idx}::uuid
                RETURNING id, name, color, description, created_at
                """,
                *params,
            )
            if not row:
                return {"success": False, "error": "Group not found"}
            return {
                "success": True,
                "data": {
                    "id": str(row["id"]),
                    "name": row["name"],
                    "color": row["color"],
                    "description": row["description"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                },
            }
    except Exception as e:
        logger.error(f"Error updating chat group: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.delete("/api/chat-groups/{group_id}")
async def delete_chat_group(group_id: str):
    try:
        async with ClientDBConnection() as conn:
            await conn.execute("DELETE FROM chat_groups WHERE id = $1::uuid", group_id)
            return {"success": True}
    except Exception as e:
        logger.error(f"Error deleting chat group: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ── Group–Conversation association ────────────────────────────────

@router.get("/api/chat-groups/{group_id}/conversations")
async def list_group_conversations(group_id: str):
    try:
        async with ClientDBConnection() as conn:
            rows = await conn.fetch(
                "SELECT conversation_id FROM chat_group_conversations WHERE group_id = $1::uuid",
                group_id,
            )
            return {
                "success": True,
                "data": [str(r["conversation_id"]) for r in rows],
            }
    except Exception as e:
        logger.error(f"Error listing group conversations: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/api/chat-groups/{group_id}/conversations")
async def add_conversations_to_group(group_id: str, body: ChatGroupAddConversations):
    try:
        async with ClientDBConnection() as conn:
            for conv_id in body.conversation_ids:
                await conn.execute(
                    """
                    INSERT INTO chat_group_conversations (group_id, conversation_id)
                    VALUES ($1::uuid, $2::uuid)
                    ON CONFLICT DO NOTHING
                    """,
                    group_id,
                    conv_id,
                )
            return {"success": True, "data": {"added": len(body.conversation_ids)}}
    except Exception as e:
        logger.error(f"Error adding conversations to group: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.delete("/api/chat-groups/{group_id}/conversations/{conversation_id}")
async def remove_conversation_from_group(group_id: str, conversation_id: str):
    try:
        async with ClientDBConnection() as conn:
            await conn.execute(
                """
                DELETE FROM chat_group_conversations
                WHERE group_id = $1::uuid AND conversation_id = $2::uuid
                """,
                group_id,
                conversation_id,
            )
            return {"success": True}
    except Exception as e:
        logger.error(f"Error removing conversation from group: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ── Group template broadcast ──────────────────────────────────────

@router.post("/api/chat-groups/{group_id}/send-template")
async def send_template_to_group(group_id: str, body: ChatGroupTemplateSend):
    """Send a WhatsApp template message to all conversations in a group."""
    try:
        async with ClientDBConnection() as conn:
            # Get all conversation IDs for this group
            group_rows = await conn.fetch(
                "SELECT conversation_id FROM chat_group_conversations WHERE group_id = $1::uuid",
                group_id,
            )
            conversation_ids = [str(r["conversation_id"]) for r in group_rows]

            if not conversation_ids:
                return {"success": True, "data": {"sent_count": 0, "failed_count": 0, "sent": [], "failed": []}}

            # Resolve phone numbers and member names
            rows = await conn.fetch(
                """
                SELECT c.id AS conversation_id, c.lead_id, l.phone,
                       COALESCE(bcm.member_name, l.name) AS name
                FROM conversations c
                LEFT JOIN leads l ON l.id = c.lead_id
                LEFT JOIN bni_conversation_manager bcm ON bcm.lead_id = c.lead_id
                WHERE c.id = ANY($1::uuid[])
                """,
                conversation_ids,
            )

        sent = []
        failed = []
        for r in rows:
            phone = r["phone"]
            if not phone:
                failed.append({"conversation_id": str(r["conversation_id"]), "reason": "No phone number"})
                continue

            params = None
            if body.parameters:
                full_name = r["name"] or "there"
                first_name = full_name.split()[0] if full_name != "there" else "there"
                params = [
                    p.replace("{member_name}", first_name)
                     .replace("{member-name}", first_name)
                     .replace("{first_name}", first_name)
                     .replace("{first-name}", first_name)
                    for p in body.parameters
                ]

            wa_id = await send_template_message(
                phone_number=phone,
                template_name=body.template_name,
                language_code=body.language_code,
                parameters=params,
                conversation_id=str(r["conversation_id"]),
                lead_id=str(r["lead_id"]) if r["lead_id"] else None,
            )

            if wa_id:
                sent.append({"conversation_id": str(r["conversation_id"]), "wa_message_id": wa_id})
            else:
                failed.append({"conversation_id": str(r["conversation_id"]), "reason": "Send failed"})

        return {
            "success": True,
            "data": {
                "sent_count": len(sent),
                "failed_count": len(failed),
                "sent": sent,
                "failed": failed,
            },
        }
    except Exception as e:
        logger.error(f"Error sending template to group: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
