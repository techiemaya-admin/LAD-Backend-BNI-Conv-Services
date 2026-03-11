"""
Followups API

Endpoints for managing automated follow-up messages for inactive leads.
Queries tenant database via multi-tenant routing.

Database tables used:
  conversation_manager: id, lead_id, first_name, last_name, context_status,
      chat_summary, created_at, updated_at, ...
  messages: id, conversation_id, lead_id, role, content, intent, message_status, created_at, ...
  followup_schedule: (if exists) for scheduled followups
"""
import logging
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel

from db.connection import AsyncDBConnection, TenantNotConfiguredError
from middleware.tenant import get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/followup", tags=["followups"])

ELIGIBLE_CONTEXT_STATUSES = [
    "Greeting",
    "Info Gathering",
    "Slot Finalizing",
]

BUSINESS_HOURS_START = 7
BUSINESS_HOURS_END = 23


class ScheduleFollowupRequest(BaseModel):
    phone_number: str
    delay_hours: Optional[float] = 4.0


# ====================
# Scheduler Status
# ====================

@router.get("/status")
async def get_status(tenant_id: Optional[str] = Depends(get_tenant_id)):
    """Get followup scheduler status and stats."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            # Count scheduled followups
            scheduled_count = 0
            try:
                scheduled_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM followup_schedule WHERE status = 'scheduled'"
                ) or 0
            except Exception:
                pass

            # Count recently sent followups (last 24h)
            recent_sent = await conn.fetchval(
                """
                SELECT COUNT(*) FROM messages m
                WHERE m.role = 'assistant'
                AND m.intent = 'followup'
                AND m.created_at > NOW() - INTERVAL '24 hours'
                """
            ) or 0

            # Count eligible leads
            eligible_count = await conn.fetchval(
                f"""
                SELECT COUNT(DISTINCT cm.lead_id) FROM conversation_manager cm
                JOIN conversations c ON c.lead_id = cm.lead_id
                WHERE cm.context_status IN ({','.join(f"'{s}'" for s in ELIGIBLE_CONTEXT_STATUSES)})
                AND c.status = 'active'
                AND c.owner = 'AI'
                """
            ) or 0

            # Total active conversations
            active_count = await conn.fetchval(
                "SELECT COUNT(*) FROM conversations WHERE status = 'active'"
            ) or 0

        now = datetime.utcnow()
        is_business_hours = BUSINESS_HOURS_START <= now.hour < BUSINESS_HOURS_END

        return {
            "is_running": True,
            "scheduler_running": True,
            "scheduled_followups_count": scheduled_count,
            "scheduled_count": scheduled_count,
            "recent_followups_sent_24h": recent_sent,
            "leads_eligible_for_followup": eligible_count,
            "active_conversations": active_count,
            "business_hours": f"{BUSINESS_HOURS_START}:00 AM - {BUSINESS_HOURS_END}:00 PM GST",
            "timezone": "Asia/Dubai",
            "current_time": now.isoformat(),
            "is_business_hours": is_business_hours,
            "eligible_context_statuses": ELIGIBLE_CONTEXT_STATUSES,
        }

    except TenantNotConfiguredError:
        now = datetime.utcnow()
        return {
            "is_running": False, "scheduler_running": False,
            "scheduled_followups_count": 0, "scheduled_count": 0,
            "recent_followups_sent_24h": 0, "leads_eligible_for_followup": 0,
            "active_conversations": 0,
            "business_hours": f"{BUSINESS_HOURS_START}:00 AM - {BUSINESS_HOURS_END}:00 PM GST",
            "timezone": "Asia/Dubai", "current_time": now.isoformat(),
            "is_business_hours": BUSINESS_HOURS_START <= now.hour < BUSINESS_HOURS_END,
            "eligible_context_statuses": ELIGIBLE_CONTEXT_STATUSES,
        }
    except Exception as e:
        logger.error(f"Error getting followup status: {e}")
        raise HTTPException(status_code=500, detail="Failed to get followup status")


# ====================
# Inactive Leads
# ====================

@router.get("/leads/inactive")
async def get_inactive_leads(
    tenant_id: Optional[str] = Depends(get_tenant_id),
    limit: int = Query(100, ge=1, le=500),
):
    """Get leads that are inactive and may need a follow-up."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            statuses_sql = ",".join(f"'{s}'" for s in ELIGIBLE_CONTEXT_STATUSES)
            query = f"""
                SELECT * FROM (
                    SELECT DISTINCT ON (l.id)
                        l.id AS lead_id,
                        l.phone,
                        l.name AS lead_name,
                        cm.first_name,
                        cm.last_name,
                        cm.context_status,
                        cm.chat_summary,
                        (SELECT content FROM messages m WHERE m.lead_id = l.id AND m.role IN ('lead', 'user')
                         ORDER BY m.created_at DESC LIMIT 1) AS last_user_message,
                        (SELECT content FROM messages m WHERE m.lead_id = l.id AND m.role IN ('AI', 'assistant')
                         ORDER BY m.created_at DESC LIMIT 1) AS last_bot_message,
                        EXTRACT(EPOCH FROM (NOW() - COALESCE(
                            (SELECT MAX(m.created_at) FROM messages m WHERE m.lead_id = l.id),
                            c.updated_at
                        ))) / 3600.0 AS hours_since_activity,
                        CASE
                            WHEN cm.context_status IN ({statuses_sql})
                            AND c.status = 'active' AND c.owner = 'AI'
                            THEN true ELSE false
                        END AS eligible_for_followup
                    FROM leads l
                    JOIN conversations c ON c.lead_id = l.id
                    LEFT JOIN LATERAL (
                        SELECT cm2.*
                        FROM conversation_manager cm2
                        WHERE cm2.lead_id = l.id
                        ORDER BY cm2.updated_at DESC LIMIT 1
                    ) cm ON true
                    WHERE c.status = 'active'
                    ORDER BY l.id, c.updated_at DESC
                ) sub
                ORDER BY hours_since_activity DESC NULLS LAST
                LIMIT $1
            """

            rows = await conn.fetch(query, limit)

            leads = []
            for row in rows:
                leads.append({
                    "lead_id": str(row["lead_id"]),
                    "phone": row["phone"],
                    "first_name": row["first_name"] or row["lead_name"] or "",
                    "last_name": row["last_name"] or "",
                    "context_status": row["context_status"] or "unknown",
                    "chat_summary": row["chat_summary"],
                    "last_user_message": row["last_user_message"],
                    "last_bot_message": row["last_bot_message"],
                    "hours_since_activity": round(row["hours_since_activity"] or 0, 1),
                    "is_scheduled": False,
                    "eligible_for_followup": row["eligible_for_followup"] or False,
                })

        return {
            "leads": leads,
            "total_inactive_leads": len(leads),
        }

    except TenantNotConfiguredError:
        return {"leads": [], "total_inactive_leads": 0}
    except Exception as e:
        logger.error(f"Error getting inactive leads: {e}")
        raise HTTPException(status_code=500, detail="Failed to get inactive leads")


# ====================
# Context Stats
# ====================

@router.get("/context-stats")
async def get_context_stats(tenant_id: Optional[str] = Depends(get_tenant_id)):
    """Get context status distribution for leads."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            query = """
                SELECT
                    cm.context_status,
                    COUNT(DISTINCT cm.lead_id) AS lead_count
                FROM conversation_manager cm
                JOIN leads l ON l.id = cm.lead_id
                WHERE cm.id IN (
                    SELECT DISTINCT ON (cm2.lead_id) cm2.id
                    FROM conversation_manager cm2
                    ORDER BY cm2.lead_id, cm2.updated_at DESC
                )
                GROUP BY cm.context_status
                ORDER BY lead_count DESC
            """
            rows = await conn.fetch(query)

            total = sum(row["lead_count"] for row in rows)
            eligible_count = sum(
                row["lead_count"]
                for row in rows
                if row["context_status"] in ELIGIBLE_CONTEXT_STATUSES
            )

            distribution = []
            for row in rows:
                distribution.append({
                    "context_status": row["context_status"] or "unknown",
                    "lead_count": row["lead_count"],
                    "eligible_for_followup": row["context_status"] in ELIGIBLE_CONTEXT_STATUSES,
                })

        return {
            "total_leads": total,
            "eligible_leads_count": eligible_count,
            "eligible_percentage": round((eligible_count / total * 100) if total > 0 else 0, 1),
            "eligible_context_statuses": ELIGIBLE_CONTEXT_STATUSES,
            "context_distribution": distribution,
        }

    except TenantNotConfiguredError:
        return {"total_leads": 0, "eligible_leads_count": 0, "eligible_percentage": 0,
                "eligible_context_statuses": ELIGIBLE_CONTEXT_STATUSES, "context_distribution": []}
    except Exception as e:
        logger.error(f"Error getting context stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to get context stats")


# ====================
# Schedule Followup
# ====================

@router.post("/schedule/{lead_id}")
async def schedule_followup(
    lead_id: str,
    request: ScheduleFollowupRequest,
    tenant_id: Optional[str] = Depends(get_tenant_id),
):
    """Schedule a followup message for a lead."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            await conn.execute(
                """
                UPDATE conversations
                SET metadata = COALESCE(metadata, '{}'::jsonb) || '{"followup_scheduled": true}'::jsonb,
                    updated_at = CURRENT_TIMESTAMP
                WHERE lead_id = $1::uuid
                """,
                lead_id,
            )

        return {"success": True, "message": f"Followup scheduled for lead {lead_id}"}

    except TenantNotConfiguredError:
        raise HTTPException(status_code=403, detail="No database configured for this tenant")
    except Exception as e:
        logger.error(f"Error scheduling followup for {lead_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to schedule followup")


# ====================
# Cancel Followup
# ====================

@router.delete("/cancel/{lead_id}")
async def cancel_followup(
    lead_id: str,
    tenant_id: Optional[str] = Depends(get_tenant_id),
):
    """Cancel a scheduled followup for a lead."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            await conn.execute(
                """
                UPDATE conversations
                SET metadata = COALESCE(metadata, '{}'::jsonb) || '{"followup_scheduled": false}'::jsonb,
                    updated_at = CURRENT_TIMESTAMP
                WHERE lead_id = $1::uuid
                """,
                lead_id,
            )

        return {"success": True, "message": f"Followup cancelled for lead {lead_id}"}

    except TenantNotConfiguredError:
        raise HTTPException(status_code=403, detail="No database configured for this tenant")
    except Exception as e:
        logger.error(f"Error cancelling followup for {lead_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to cancel followup")
