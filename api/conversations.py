"""
REST API for listing conversations and messages.

Used by the LAD-Frontend to display conversations in the Unified Comms UI.
"""
import json
import logging
import uuid
from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from db.connection import ClientDBConnection, CoreDBConnection
from pydantic import Field
from services.whatsapp_client import (
    send_message as send_whatsapp_message,
    get_message_templates,
    send_template_message,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


# ── Pydantic models ──────────────────────────────────────────────

class BulkStatusRequest(BaseModel):
    ids: list[str]
    status: str


class BulkLabelsRequest(BaseModel):
    ids: list[str]
    label_id: str


class BulkDeleteRequest(BaseModel):
    ids: list[str]


class BulkTemplateSendRequest(BaseModel):
    conversation_ids: list[str]
    template_name: str
    language_code: str = "en_US"
    parameters: list[str] | None = None


# ── Helpers ──────────────────────────────────────────────────────

async def _get_labels_for_conversations(conn, conversation_ids: list[str]) -> dict[str, list[dict]]:
    """Fetch labels for a batch of conversations."""
    if not conversation_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT cl.conversation_id, l.id, l.name, l.color
        FROM conversation_labels cl
        JOIN labels l ON l.id = cl.label_id
        WHERE cl.conversation_id = ANY($1::uuid[])
        ORDER BY l.name
        """,
        conversation_ids,
    )
    result: dict[str, list[dict]] = {}
    for r in rows:
        cid = str(r["conversation_id"])
        if cid not in result:
            result[cid] = []
        result[cid].append({
            "id": str(r["id"]),
            "name": r["name"],
            "color": r["color"],
        })
    return result


# ── List conversations ───────────────────────────────────────────

@router.get("")
async def list_conversations(
    status: str = Query(None),
    channel: str = Query(None),
    search: str = Query(None),
    status_filter: str = Query(None, description="pending|unread|not_replied|resolved|favorites"),
    context_status: str = Query(None, description="Filter by context_status from bni_conversation_manager"),
    label_id: str = Query(None),
    sort_by: str = Query("updated_at", description="updated_at|longest_waiting"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List all conversations with latest message info."""
    try:
        async with ClientDBConnection() as conn:
            where_clauses = ["1=1"]
            params = []
            idx = 1

            # Exclude soft-deleted
            where_clauses.append("(c.is_deleted IS NULL OR c.is_deleted = false)")

            if status:
                where_clauses.append(f"c.status = ${idx}")
                params.append(status)
                idx += 1

            if search:
                where_clauses.append(f"(l.name ILIKE ${idx} OR l.phone ILIKE ${idx})")
                params.append(f"%{search}%")
                idx += 1

            # Advanced filters
            if status_filter == "resolved":
                where_clauses.append("c.status = 'resolved'")
            elif status_filter == "pending":
                where_clauses.append("c.status = 'active' AND c.owner = 'AI'")
            elif status_filter == "not_replied":
                where_clauses.append("""
                    (SELECT role FROM messages WHERE conversation_id = c.id
                     ORDER BY created_at DESC LIMIT 1) = 'lead'
                """)
            elif status_filter == "favorites":
                where_clauses.append("c.is_favorite = true")

            if context_status:
                where_clauses.append(f"cm.context_status = ${idx}")
                params.append(context_status)
                idx += 1

            if label_id:
                where_clauses.append(f"""
                    c.id IN (SELECT conversation_id FROM conversation_labels WHERE label_id = ${idx}::uuid)
                """)
                params.append(label_id)
                idx += 1

            # Sorting
            order_clause = "c.updated_at DESC"
            if sort_by == "longest_waiting":
                order_clause = """
                    COALESCE(
                        (SELECT created_at FROM messages
                         WHERE conversation_id = c.id AND role = 'lead'
                         ORDER BY created_at DESC LIMIT 1),
                        c.started_at
                    ) ASC
                """

            params.extend([limit, offset])

            rows = await conn.fetch(
                f"""
                SELECT
                    c.id,
                    c.lead_id,
                    l.name AS lead_name,
                    l.phone AS lead_phone,
                    'whatsapp' AS lead_channel,
                    c.status,
                    c.owner,
                    c.started_at,
                    c.updated_at,
                    c.is_favorite,
                    c.is_pinned,
                    c.is_locked,
                    cm.context_status,
                    (SELECT content FROM messages
                     WHERE conversation_id = c.id
                     ORDER BY created_at DESC LIMIT 1) AS last_message_content,
                    (SELECT role FROM messages
                     WHERE conversation_id = c.id
                     ORDER BY created_at DESC LIMIT 1) AS last_message_role,
                    (SELECT created_at FROM messages
                     WHERE conversation_id = c.id
                     ORDER BY created_at DESC LIMIT 1) AS last_message_at,
                    (SELECT COUNT(*) FROM messages
                     WHERE conversation_id = c.id) AS message_count
                FROM conversations c
                LEFT JOIN leads l ON l.id = c.lead_id
                LEFT JOIN bni_conversation_manager cm ON cm.lead_id = c.lead_id
                WHERE {" AND ".join(where_clauses)}
                ORDER BY c.is_pinned DESC NULLS LAST, {order_clause}
                LIMIT ${idx} OFFSET ${idx + 1}
                """,
                *params,
            )

            count_row = await conn.fetchrow(
                f"""
                SELECT COUNT(*) AS total
                FROM conversations c
                LEFT JOIN leads l ON l.id = c.lead_id
                LEFT JOIN bni_conversation_manager cm ON cm.lead_id = c.lead_id
                WHERE {" AND ".join(where_clauses)}
                """,
                *params[:-2],
            )

            # Fetch labels in batch
            conv_ids = [str(r["id"]) for r in rows]
            labels_map = await _get_labels_for_conversations(conn, conv_ids)

            data = []
            for r in rows:
                cid = str(r["id"])
                data.append({
                    "id": cid,
                    "lead_id": str(r["lead_id"]),
                    "lead_name": r["lead_name"] or "Unknown",
                    "lead_phone": r["lead_phone"],
                    "lead_channel": r["lead_channel"],
                    "status": r["status"],
                    "owner": r["owner"] or "AI",
                    "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                    "context_status": r["context_status"],
                    "last_message_content": r["last_message_content"],
                    "last_message_role": r["last_message_role"],
                    "last_message_at": r["last_message_at"].isoformat() if r["last_message_at"] else None,
                    "message_count": r["message_count"] or 0,
                    "unread_count": 0,
                    "is_favorite": r["is_favorite"] or False,
                    "is_pinned": r["is_pinned"] or False,
                    "is_locked": r["is_locked"] or False,
                    "labels": labels_map.get(cid, []),
                })

            return {
                "success": True,
                "data": data,
                "total": count_row["total"] if count_row else 0,
            }

    except Exception as e:
        logger.error(f"Error listing conversations: {e}", exc_info=True)
        return {"success": False, "data": [], "total": 0, "error": str(e)}


# ── Context statuses (tenant-specific) ────────────────────────────
# NOTE: Static routes MUST be defined BEFORE parameterized /{conversation_id} routes
# otherwise FastAPI will match "context-statuses" as a conversation_id.

@router.get("/context-statuses")
async def list_context_statuses():
    """Return distinct context statuses that exist for this tenant's conversations."""
    try:
        async with ClientDBConnection() as conn:
            rows = await conn.fetch(
                """
                SELECT cm.context_status, COUNT(*) AS count
                FROM bni_conversation_manager cm
                WHERE cm.context_status IS NOT NULL AND cm.context_status != ''
                GROUP BY cm.context_status
                ORDER BY count DESC
                """
            )
            data = [
                {"value": r["context_status"], "count": r["count"]}
                for r in rows
            ]
            return {"success": True, "data": data}
    except Exception as e:
        logger.error(f"Error listing context statuses: {e}", exc_info=True)
        return {"success": True, "data": []}


# ── Template messages ─────────────────────────────────────────────

@router.get("/templates/debug")
async def debug_templates():
    """Diagnostic endpoint to check template config (non-sensitive)."""
    from services.whatsapp_client import PHONE_NUMBER_ID, ACCESS_TOKEN, _get_waba_id
    waba_id = await _get_waba_id()
    return {
        "phone_number_id_set": bool(PHONE_NUMBER_ID),
        "phone_number_id_preview": PHONE_NUMBER_ID[:6] + "..." if PHONE_NUMBER_ID else "MISSING",
        "access_token_set": bool(ACCESS_TOKEN),
        "waba_id": waba_id or "NOT_RESOLVED",
    }


@router.get("/templates")
async def list_templates():
    """Return approved WhatsApp message templates from Meta API."""
    try:
        templates = await get_message_templates()
        logger.info(f"Templates fetched: {len(templates)} templates found")
        return {"success": True, "data": templates}
    except Exception as e:
        logger.error(f"Error fetching templates: {e}", exc_info=True)
        return {"success": False, "data": [], "error": str(e)}


# ── Bulk operations ──────────────────────────────────────────────

@router.post("/bulk/send-template")
async def bulk_send_template(body: BulkTemplateSendRequest):
    """Send a WhatsApp template message to multiple conversations."""
    try:
        async with ClientDBConnection() as conn:
            # Resolve phone numbers for all conversation IDs
            rows = await conn.fetch(
                """
                SELECT c.id AS conversation_id, c.lead_id, l.phone, l.name
                FROM conversations c
                LEFT JOIN leads l ON l.id = c.lead_id
                WHERE c.id = ANY($1::uuid[])
                """,
                body.conversation_ids,
            )

        sent = []
        failed = []
        for r in rows:
            phone = r["phone"]
            if not phone:
                failed.append({"conversation_id": str(r["conversation_id"]), "reason": "No phone number"})
                continue

            # Substitute {member_name} / {member-name} in parameters if present
            params = None
            if body.parameters:
                member_name = r["name"] or "there"
                params = [
                    p.replace("{member_name}", member_name)
                     .replace("{member-name}", member_name)
                     .replace("{first_name}", member_name.split()[0] if member_name != "there" else "there")
                     .replace("{first-name}", member_name.split()[0] if member_name != "there" else "there")
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
        logger.error(f"Error bulk sending template: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/bulk/status")
async def bulk_update_status(body: BulkStatusRequest):
    """Update status for multiple conversations."""
    if body.status not in ("active", "resolved", "muted"):
        return {"success": False, "error": "Invalid status"}
    try:
        async with ClientDBConnection() as conn:
            result = await conn.execute(
                "UPDATE conversations SET status = $1, updated_at = NOW() WHERE id = ANY($2::uuid[])",
                body.status,
                body.ids,
            )
            return {"success": True, "data": {"updated": len(body.ids)}}
    except Exception as e:
        logger.error(f"Error bulk updating status: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/bulk/labels")
async def bulk_add_label(body: BulkLabelsRequest):
    """Add a label to multiple conversations."""
    try:
        async with ClientDBConnection() as conn:
            for cid in body.ids:
                await conn.execute(
                    """
                    INSERT INTO conversation_labels (conversation_id, label_id)
                    VALUES ($1::uuid, $2::uuid)
                    ON CONFLICT DO NOTHING
                    """,
                    cid,
                    body.label_id,
                )
            return {"success": True, "data": {"updated": len(body.ids)}}
    except Exception as e:
        logger.error(f"Error bulk adding labels: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.post("/bulk/delete")
async def bulk_delete(body: BulkDeleteRequest):
    """Soft-delete multiple conversations."""
    try:
        async with ClientDBConnection() as conn:
            await conn.execute(
                "UPDATE conversations SET is_deleted = true, updated_at = NOW() WHERE id = ANY($1::uuid[])",
                body.ids,
            )
            return {"success": True, "data": {"deleted": len(body.ids)}}
    except Exception as e:
        logger.error(f"Error bulk deleting: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ── Parameterized routes (MUST come AFTER static routes) ─────────

@router.get("/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List messages for a conversation."""
    try:
        async with ClientDBConnection() as conn:
            rows = await conn.fetch(
                """
                SELECT id, conversation_id, lead_id, role, content,
                       message_status, created_at
                FROM messages
                WHERE conversation_id = $1::uuid
                ORDER BY created_at ASC
                LIMIT $2 OFFSET $3
                """,
                conversation_id,
                limit,
                offset,
            )

            count_row = await conn.fetchrow(
                "SELECT COUNT(*) AS total FROM messages WHERE conversation_id = $1::uuid",
                conversation_id,
            )

            total = count_row["total"] if count_row else 0
            data = []
            for r in rows:
                data.append({
                    "id": str(r["id"]),
                    "conversation_id": str(r["conversation_id"]),
                    "lead_id": str(r["lead_id"]),
                    "role": "assistant" if r["role"] == "agent" else ("user" if r["role"] == "lead" else r["role"]),
                    "content": r["content"],
                    "message_status": r["message_status"] or "sent",
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                })

            return {
                "success": True,
                "data": data,
                "total": total,
                "has_more": (offset + limit) < total,
            }

    except Exception as e:
        logger.error(f"Error listing messages: {e}", exc_info=True)
        return {"success": False, "data": [], "total": 0, "has_more": False, "error": str(e)}


@router.post("/{conversation_id}/messages")
async def post_message(conversation_id: str, request: Request):
    """Send a human-agent message via WhatsApp and save to DB."""
    try:
        body = await request.json()
        content = body.get("content", "").strip()
        lead_id = body.get("lead_id")
        phone_number = body.get("phone_number")

        if not content:
            return {"success": False, "error": "Message content is required"}

        # Look up phone number from lead if not provided
        if not phone_number and lead_id:
            async with ClientDBConnection() as conn:
                lead = await conn.fetchrow(
                    "SELECT phone FROM leads WHERE id = $1::uuid", lead_id
                )
                if lead:
                    phone_number = lead["phone"]

        if not phone_number:
            return {"success": False, "error": "Could not determine recipient phone number"}

        # Send via WhatsApp
        wa_msg_id = await send_whatsapp_message(
            phone_number=phone_number,
            text=content,
            conversation_id=conversation_id,
            lead_id=lead_id,
        )

        if not wa_msg_id:
            return {"success": False, "error": "Failed to send WhatsApp message"}

        # Update conversation owner to human agent
        async with ClientDBConnection() as conn:
            await conn.execute(
                "UPDATE conversations SET owner = 'human_agent', updated_at = NOW() WHERE id = $1::uuid",
                conversation_id,
            )

        msg_id = str(uuid.uuid4())
        return {
            "success": True,
            "data": {
                "id": msg_id,
                "conversation_id": conversation_id,
                "lead_id": lead_id,
                "role": "human_agent",
                "content": content,
                "message_status": "sent",
                "created_at": None,
            },
        }

    except Exception as e:
        logger.error(f"Error sending message: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str):
    """Get a single conversation by ID."""
    try:
        async with ClientDBConnection() as conn:
            row = await conn.fetchrow(
                """
                SELECT c.id, c.lead_id, c.status, c.owner, c.started_at, c.updated_at,
                       c.is_favorite, c.is_pinned, c.is_locked,
                       l.name AS lead_name, l.phone AS lead_phone,
                       cm.context_status
                FROM conversations c
                LEFT JOIN leads l ON l.id = c.lead_id
                LEFT JOIN bni_conversation_manager cm ON cm.lead_id = c.lead_id
                WHERE c.id = $1::uuid
                """,
                conversation_id,
            )
            if not row:
                return {"success": False, "error": "Conversation not found"}

            # Get labels
            cid = str(row["id"])
            labels_map = await _get_labels_for_conversations(conn, [cid])

            return {
                "success": True,
                "data": {
                    "id": cid,
                    "lead_id": str(row["lead_id"]),
                    "lead_name": row["lead_name"] or "Unknown",
                    "lead_phone": row["lead_phone"],
                    "lead_channel": "whatsapp",
                    "status": row["status"],
                    "owner": row["owner"] or "AI",
                    "started_at": row["started_at"].isoformat() if row["started_at"] else None,
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                    "context_status": row["context_status"],
                    "is_favorite": row["is_favorite"] or False,
                    "is_pinned": row["is_pinned"] or False,
                    "is_locked": row["is_locked"] or False,
                    "labels": labels_map.get(cid, []),
                },
            }

    except Exception as e:
        logger.error(f"Error getting conversation: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.patch("/{conversation_id}/status")
async def update_status(conversation_id: str, request: Request):
    """Update conversation status (active, resolved, muted)."""
    try:
        body = await request.json()
        new_status = body.get("status")
        if new_status not in ("active", "resolved", "muted"):
            return {"success": False, "error": "Invalid status"}

        async with ClientDBConnection() as conn:
            await conn.execute(
                "UPDATE conversations SET status = $1, updated_at = NOW() WHERE id = $2::uuid",
                new_status,
                conversation_id,
            )

        return {"success": True}

    except Exception as e:
        logger.error(f"Error updating status: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.patch("/{conversation_id}/ownership")
async def update_ownership(conversation_id: str, request: Request):
    """Update conversation ownership (AI, human_agent)."""
    try:
        body = await request.json()
        new_owner = body.get("owner")
        if new_owner not in ("AI", "human_agent"):
            return {"success": False, "error": "Invalid owner"}

        async with ClientDBConnection() as conn:
            await conn.execute(
                "UPDATE conversations SET owner = $1, updated_at = NOW() WHERE id = $2::uuid",
                new_owner,
                conversation_id,
            )

        return {"success": True}

    except Exception as e:
        logger.error(f"Error updating ownership: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ── CRM actions ──────────────────────────────────────────────────

@router.patch("/{conversation_id}/favorite")
async def toggle_favorite(conversation_id: str):
    """Toggle favorite status."""
    try:
        async with ClientDBConnection() as conn:
            row = await conn.fetchrow(
                "UPDATE conversations SET is_favorite = NOT COALESCE(is_favorite, false), updated_at = NOW() WHERE id = $1::uuid RETURNING is_favorite",
                conversation_id,
            )
            return {"success": True, "data": {"is_favorite": row["is_favorite"] if row else False}}
    except Exception as e:
        logger.error(f"Error toggling favorite: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.patch("/{conversation_id}/pin")
async def toggle_pin(conversation_id: str):
    """Toggle pin status."""
    try:
        async with ClientDBConnection() as conn:
            row = await conn.fetchrow(
                "UPDATE conversations SET is_pinned = NOT COALESCE(is_pinned, false), updated_at = NOW() WHERE id = $1::uuid RETURNING is_pinned",
                conversation_id,
            )
            return {"success": True, "data": {"is_pinned": row["is_pinned"] if row else False}}
    except Exception as e:
        logger.error(f"Error toggling pin: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.patch("/{conversation_id}/lock")
async def toggle_lock(conversation_id: str):
    """Toggle lock status (prevents AI from responding)."""
    try:
        async with ClientDBConnection() as conn:
            row = await conn.fetchrow(
                "UPDATE conversations SET is_locked = NOT COALESCE(is_locked, false), updated_at = NOW() WHERE id = $1::uuid RETURNING is_locked",
                conversation_id,
            )
            return {"success": True, "data": {"is_locked": row["is_locked"] if row else False}}
    except Exception as e:
        logger.error(f"Error toggling lock: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@router.delete("/{conversation_id}")
async def soft_delete_conversation(conversation_id: str):
    """Soft-delete a conversation."""
    try:
        async with ClientDBConnection() as conn:
            await conn.execute(
                "UPDATE conversations SET is_deleted = true, updated_at = NOW() WHERE id = $1::uuid",
                conversation_id,
            )
            return {"success": True}
    except Exception as e:
        logger.error(f"Error deleting conversation: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# ── Business profile ─────────────────────────────────────────────

@router.get("/{conversation_id}/business-profile")
async def get_business_profile(conversation_id: str):
    """Get the member's full business profile for a conversation."""
    try:
        async with ClientDBConnection() as client_conn:
            # Get lead phone from conversation
            conv = await client_conn.fetchrow(
                """
                SELECT c.lead_id, l.phone, l.name
                FROM conversations c
                LEFT JOIN leads l ON l.id = c.lead_id
                WHERE c.id = $1::uuid
                """,
                conversation_id,
            )
            if not conv:
                return {"success": False, "error": "Conversation not found"}

            phone = conv["phone"]

            # Get profile from bni_conversation_manager (client DB)
            bcm = await client_conn.fetchrow(
                """
                SELECT company_name, industry, designation, services_offered,
                       ideal_customer_profile, metadata, context_status
                FROM bni_conversation_manager
                WHERE member_phone = $1
                LIMIT 1
                """,
                phone,
            )

        # Get enrichment from community_roi_members (core DB)
        core_profile = None
        try:
            async with CoreDBConnection() as core_conn:
                core_profile = await core_conn.fetchrow(
                    """
                    SELECT name, email, company_name, industry, designation,
                           metadata,
                           total_one_to_ones, total_referrals_given,
                           total_referrals_received, total_business_inside_aed,
                           total_business_outside_aed, current_streak, max_streak
                    FROM community_roi_members
                    WHERE phone = $1 AND (is_deleted IS NULL OR is_deleted = false)
                    LIMIT 1
                    """,
                    phone,
                )
        except Exception as e:
            logger.warning(f"Could not fetch core profile: {e}")

        # Parse metadata JSONB
        bcm_metadata = {}
        if bcm and bcm["metadata"]:
            m = bcm["metadata"]
            bcm_metadata = json.loads(m) if isinstance(m, str) else m

        core_metadata = {}
        if core_profile and core_profile["metadata"]:
            m = core_profile["metadata"]
            core_metadata = json.loads(m) if isinstance(m, str) else m

        website_data = bcm_metadata.get("website_data", {})
        icp_answers = bcm_metadata.get("icp_answers", {})

        profile = {
            "member_name": conv["name"] or (bcm["designation"] if bcm else None),
            "phone": phone,
            "email": core_profile["email"] if core_profile else None,
            "company_name": (bcm["company_name"] if bcm else None)
                or (core_profile["company_name"] if core_profile else None),
            "industry": (bcm["industry"] if bcm else None)
                or (core_profile["industry"] if core_profile else None),
            "designation": (bcm["designation"] if bcm else None)
                or (core_profile["designation"] if core_profile else None),
            "services_offered": bcm["services_offered"] if bcm else None,
            "ideal_customer_profile": bcm["ideal_customer_profile"] if bcm else None,
            "context_status": bcm["context_status"] if bcm else None,
            # Website / social data
            "website": icp_answers.get("q1", website_data.get("raw_url", "")),
            "website_about": website_data.get("about"),
            "website_clients": website_data.get("clients", []),
            "website_services": website_data.get("services", []),
            # ICP discovery answers
            "icp_top_clients": icp_answers.get("q2"),
            "icp_decision_maker": icp_answers.get("q3"),
            "icp_ideal_referrals": icp_answers.get("q4"),
            # KPIs from core
            "total_one_to_ones": core_profile["total_one_to_ones"] if core_profile else 0,
            "total_referrals_given": core_profile["total_referrals_given"] if core_profile else 0,
            "total_referrals_received": core_profile["total_referrals_received"] if core_profile else 0,
            "total_business_inside_aed": core_profile["total_business_inside_aed"] if core_profile else 0,
            "total_business_outside_aed": core_profile["total_business_outside_aed"] if core_profile else 0,
            "current_streak": core_profile["current_streak"] if core_profile else 0,
            "max_streak": core_profile["max_streak"] if core_profile else 0,
            # Onboarding
            "onboarding_completed_at": bcm_metadata.get("onboarding_completed_at")
                or core_metadata.get("onboarding_completed_at"),
        }

        return {"success": True, "data": profile}

    except Exception as e:
        logger.error(f"Error fetching business profile: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
