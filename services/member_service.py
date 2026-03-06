"""
Member service — profile management and ICP-based matching.

Reads from community_roi_members (salesmaya_agent) for member data.
Writes enriched profiles back after onboarding completion.
Scores potential matches based on ICP compatibility.
"""
import json
import logging
import uuid
from datetime import datetime

from db.connection import CoreDBConnection, ClientDBConnection
from db.schema import core_table, get_tenant_id

logger = logging.getLogger(__name__)


async def enrich_member_profile(phone_number: str, info_fields: dict):
    """Write onboarding-collected profile data to community_roi_members.

    Called when context_status transitions to 'onboarding_complete'.
    Updates the member record that matches the phone number.
    """
    try:
        tenant_id = get_tenant_id()
        async with CoreDBConnection() as conn:
            member = await conn.fetchrow(
                f"""
                SELECT id FROM {core_table('community_roi_members')}
                WHERE REPLACE(REPLACE(REPLACE(phone, ' ', ''), '+', ''), '-', '') = $1
                  AND tenant_id = $2::uuid AND is_deleted = false
                """,
                phone_number,
                tenant_id,
            )
            if not member:
                logger.warning(f"No community_roi_members record for phone {phone_number}")
                return

            await conn.execute(
                f"""
                UPDATE {core_table('community_roi_members')} SET
                    company_name = COALESCE($1, company_name),
                    industry = COALESCE($2, industry),
                    designation = COALESCE($3, designation),
                    metadata = metadata || $4::jsonb,
                    updated_at = NOW()
                WHERE id = $5::uuid
                """,
                info_fields.get("company_name"),
                info_fields.get("industry"),
                info_fields.get("designation"),
                json.dumps({
                    "services_offered": info_fields.get("services_offered"),
                    "ideal_customer_profile": info_fields.get("ideal_customer_profile"),
                    "onboarding_completed_at": datetime.utcnow().isoformat(),
                }),
                str(member["id"]),
            )
            logger.info(f"Enriched member profile for {phone_number}")

    except Exception as e:
        logger.error(f"Error enriching member profile: {e}", exc_info=True)


async def get_member_stats_json(phone_number: str) -> dict:
    """Get member KPI stats for LLM context."""
    try:
        tenant_id = get_tenant_id()
        async with CoreDBConnection() as conn:
            member = await conn.fetchrow(
                f"""
                SELECT id, name, total_one_to_ones, total_referrals_given,
                       total_referrals_received, total_business_inside_aed,
                       total_business_outside_aed, current_streak, max_streak,
                       last_unique_meeting_at
                FROM {core_table('community_roi_members')}
                WHERE REPLACE(REPLACE(REPLACE(phone, ' ', ''), '+', ''), '-', '') = $1
                  AND tenant_id = $2::uuid AND is_deleted = false
                """,
                phone_number,
                tenant_id,
            )
            if not member:
                return {"error": "Member not found"}

            return {
                "name": member["name"],
                "total_one_to_ones": member["total_one_to_ones"] or 0,
                "total_referrals_given": member["total_referrals_given"] or 0,
                "total_referrals_received": member["total_referrals_received"] or 0,
                "total_business_inside_aed": float(member["total_business_inside_aed"] or 0),
                "total_business_outside_aed": float(member["total_business_outside_aed"] or 0),
                "current_streak": member["current_streak"] or 0,
                "max_streak": member["max_streak"] or 0,
                "last_unique_meeting_at": (
                    member["last_unique_meeting_at"].isoformat()
                    if member["last_unique_meeting_at"]
                    else None
                ),
            }
    except Exception as e:
        logger.error(f"Error getting member stats: {e}")
        return {"error": str(e)}


async def find_best_match(phone_number: str) -> dict | None:
    """Find the best 1-to-1 match for a member using ICP-based scoring.

    Scoring:
      - ICP match (35 pts): candidate's industry/services match member's ICP
      - Reverse ICP (25 pts): member matches candidate's ICP
      - Never-met bonus (40 pts): only suggest members never met before
      - Activity bonus (up to 10 pts): prefer active members

    Returns the top candidate dict or None if no suitable match.
    """
    try:
        tenant_id = get_tenant_id()

        # Also load ICP answers from bni_conversation_manager for richer matching
        icp_answers_text = ""
        try:
            async with ClientDBConnection() as bni_conn:
                cm_row = await bni_conn.fetchrow(
                    "SELECT metadata FROM bni_conversation_manager WHERE member_phone = $1",
                    phone_number,
                )
                if cm_row:
                    cm_meta = cm_row["metadata"]
                    if isinstance(cm_meta, str):
                        cm_meta = json.loads(cm_meta)
                    icp_answers = cm_meta.get("icp_answers", {})
                    if icp_answers:
                        icp_answers_text = " ".join(str(v) for v in icp_answers.values()).lower()
        except Exception as e:
            logger.warning(f"Could not load ICP answers for matching: {e}")

        async with CoreDBConnection() as conn:
            # Get the requesting member
            member = await conn.fetchrow(
                f"""
                SELECT id, name, company_name, industry, metadata
                FROM {core_table('community_roi_members')}
                WHERE REPLACE(REPLACE(REPLACE(phone, ' ', ''), '+', ''), '-', '') = $1
                  AND tenant_id = $2::uuid AND is_deleted = false
                """,
                phone_number,
                tenant_id,
            )
            if not member:
                # Member not in community_roi_members — still try fallback
                fallback = await _get_unmet_members_fallback_no_member(phone_number, conn)
                if fallback:
                    return {"type": "fallback_list", "members": fallback}
                return None

            member_id = str(member["id"])
            member_metadata = member["metadata"] if isinstance(member["metadata"], dict) else {}
            member_icp = member_metadata.get("ideal_customer_profile", "")
            member_services = member_metadata.get("services_offered", "")

            # Combine ICP summary + raw ICP answers for richer keyword matching
            icp_text = f"{member_icp} {icp_answers_text}".lower().strip()

            # Get all other active members
            candidates = await conn.fetch(
                f"""
                SELECT id, name, company_name, industry, phone, metadata,
                       current_streak, total_one_to_ones
                FROM {core_table('community_roi_members')}
                WHERE tenant_id = $1::uuid AND is_deleted = false
                  AND REPLACE(REPLACE(REPLACE(phone, ' ', ''), '+', ''), '-', '') != $2
                  AND phone IS NOT NULL
                """,
                tenant_id,
                phone_number,
            )

            if not candidates:
                return None

            # Get relationship scores for this member
            relationships = await conn.fetch(
                f"""
                SELECT member_b_id, one_to_one_count
                FROM {core_table('community_roi_relationship_scores')}
                WHERE tenant_id = $1::uuid AND member_a_id = $2::uuid AND is_deleted = false
                UNION ALL
                SELECT member_a_id, one_to_one_count
                FROM {core_table('community_roi_relationship_scores')}
                WHERE tenant_id = $1::uuid AND member_b_id = $2::uuid AND is_deleted = false
                """,
                tenant_id,
                member["id"],
            )
            met_counts = {str(r["member_b_id"]): r["one_to_one_count"] for r in relationships}

        # Check which candidates already have pending meetings
        pending_phones = set()
        try:
            async with ClientDBConnection() as conn:
                rows = await conn.fetch(
                    """
                    SELECT member_b_phone FROM scheduled_meetings
                    WHERE member_a_phone = $1 AND status NOT IN ('completed', 'cancelled')
                    UNION
                    SELECT member_a_phone FROM scheduled_meetings
                    WHERE member_b_phone = $1 AND status NOT IN ('completed', 'cancelled')
                    """,
                    phone_number,
                )
                pending_phones = {r[0] for r in rows}
        except Exception:
            pass  # Table might not exist yet

        # Score each candidate
        scored = []
        for c in candidates:
            cid = str(c["id"])
            c_meta = c["metadata"] if isinstance(c["metadata"], dict) else {}
            c_icp = c_meta.get("ideal_customer_profile", "")
            c_services = c_meta.get("services_offered", "")
            c_phone = c["phone"]

            # Skip candidates with pending meetings
            if c_phone in pending_phones:
                continue

            # Only suggest members never met before
            meetings_with = met_counts.get(cid, 0)
            if meetings_with > 0:
                continue

            score = 0

            # ICP match (35 pts): does the candidate match what the member is looking for?
            if icp_text and (c["industry"] or c_services or c["company_name"]):
                # Check industry category (e.g. "Construction > Elevators")
                if c["industry"]:
                    industry_parts = [p.strip().lower() for p in c["industry"].split(">")]
                    for part in industry_parts:
                        # Match industry keywords against ICP text + raw answers
                        words = [w for w in part.split() if len(w) > 2]
                        matching_words = sum(1 for w in words if w in icp_text)
                        if matching_words > 0:
                            score += min(matching_words * 5, 15)

                # Check candidate's services against member's ICP
                if c_services:
                    for svc in c_services.split(","):
                        svc_clean = svc.strip().lower()
                        if svc_clean and svc_clean in icp_text:
                            score += 10
                            break

                # Check company name keywords (e.g. "Snap Fitness" matching "gym/fitness" in ICP)
                if c["company_name"]:
                    comp_words = [w.lower() for w in c["company_name"].split() if len(w) > 3]
                    if any(w in icp_text for w in comp_words):
                        score += 10

            # Cap ICP match at 35
            score = min(score, 35)

            # Reverse ICP (25 pts): does the member match what the candidate is looking for?
            if c_icp and (member["industry"] or member_services):
                c_icp_lower = c_icp.lower()
                if member["industry"] and member["industry"].lower() in c_icp_lower:
                    score += 15
                if member_services and any(
                    svc.strip().lower() in c_icp_lower
                    for svc in member_services.split(",")
                ):
                    score += 10

            # Never-met bonus (40 pts) — all candidates here are unmet
            score += 40

            # Activity bonus (up to 10 pts)
            streak = c["current_streak"] or 0
            score += min(streak * 2, 10)

            scored.append({
                "member_id": cid,
                "name": c["name"],
                "company_name": c["company_name"],
                "industry": c["industry"],
                "phone": c_phone,
                "services_offered": c_services,
                "score": score,
                "match_reason": _build_match_reason(c, member_icp),
            })

        if not scored:
            # Fallback: return 5 random unmet members
            fallback = await _get_unmet_members_fallback(phone_number, candidates, met_counts, pending_phones)
            if fallback:
                return {"type": "fallback_list", "members": fallback}
            return None

        # Sort by score descending, return top 4 matches
        scored.sort(key=lambda x: x["score"], reverse=True)
        top_matches = scored[:4]
        logger.info(
            f"Top {len(top_matches)} matches for {phone_number}: "
            + ", ".join(f"{m['name']}({m['score']})" for m in top_matches)
        )
        return {"type": "scored_list", "members": top_matches}

    except Exception as e:
        logger.error(f"Error finding match: {e}", exc_info=True)
        return None


async def _get_unmet_members_fallback(
    phone_number: str, candidates, met_counts: dict, pending_phones: set, limit: int = 5
) -> list[dict]:
    """Fallback: pick up to `limit` unmet members when ICP scoring yields no results."""
    unmet = []
    for c in candidates:
        cid = str(c["id"])
        c_phone = c["phone"]
        if c_phone in pending_phones:
            continue
        if met_counts.get(cid, 0) > 0:
            continue
        c_meta = c["metadata"] if isinstance(c["metadata"], dict) else {}
        unmet.append({
            "name": c["name"],
            "company_name": c["company_name"],
            "industry": c["industry"],
            "services_offered": c_meta.get("services_offered", ""),
            "phone": c_phone,
        })
        if len(unmet) >= limit:
            break
    logger.info(f"Fallback: returning {len(unmet)} unmet members for {phone_number}")
    return unmet


async def _get_unmet_members_fallback_no_member(
    phone_number: str, conn, limit: int = 5
) -> list[dict]:
    """Fallback when member doesn't exist in community_roi_members.

    Just pick 5 random active members as suggestions.
    """
    tenant_id = get_tenant_id()
    candidates = await conn.fetch(
        f"""
        SELECT name, company_name, industry, phone, metadata
        FROM {core_table('community_roi_members')}
        WHERE tenant_id = $1::uuid AND is_deleted = false
          AND phone IS NOT NULL
          AND REPLACE(REPLACE(REPLACE(phone, ' ', ''), '+', ''), '-', '') != $2
        ORDER BY RANDOM()
        LIMIT $3
        """,
        tenant_id,
        phone_number,
        limit,
    )
    result = []
    for c in candidates:
        c_meta = c["metadata"] if isinstance(c["metadata"], dict) else {}
        result.append({
            "name": c["name"],
            "company_name": c["company_name"],
            "industry": c["industry"],
            "services_offered": c_meta.get("services_offered", ""),
            "phone": c["phone"],
        })
    logger.info(f"Fallback (no member record): returning {len(result)} random members for {phone_number}")
    return result


def _build_match_reason(candidate: dict, member_icp: str) -> str:
    """Build a human-readable reason why this candidate is a good match."""
    parts = []
    if candidate["company_name"]:
        parts.append(f"They run {candidate['company_name']}")
    if candidate["industry"]:
        parts.append(f"specializing in {candidate['industry']}")
    if member_icp:
        parts.append("which aligns with your ideal customer profile")
    return ", ".join(parts) if parts else "Great networking opportunity"


async def get_member_by_phone(phone_number: str) -> dict | None:
    """Look up a community_roi_members record by phone."""
    try:
        tenant_id = get_tenant_id()
        async with CoreDBConnection() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT id, name, company_name, industry, phone, metadata
                FROM {core_table('community_roi_members')}
                WHERE REPLACE(REPLACE(REPLACE(phone, ' ', ''), '+', ''), '-', '') = $1
                  AND tenant_id = $2::uuid AND is_deleted = false
                """,
                phone_number,
                tenant_id,
            )
            if row:
                return dict(row)
            return None
    except Exception as e:
        logger.error(f"Error looking up member by phone: {e}")
        return None
