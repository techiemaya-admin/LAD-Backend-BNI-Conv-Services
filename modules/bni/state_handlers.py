"""
BNI State Transition Handlers

Extracted from conversation_engine.py — handles side effects when
BNI conversation state changes (matching, meeting coordination, etc.).
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Optional

from db.connection import AsyncDBConnection, CoreDBConnection
from db.schema import core_table
from services.account_registry import WhatsAppAccount

logger = logging.getLogger(__name__)


async def bni_handle_state_transition(
    account: WhatsAppAccount,
    phone_number: str,
    lead_id: str,
    conversation_id: str,
    old_status: str,
    new_status: str,
    info_fields: dict,
) -> Optional[str]:
    """Handle BNI-specific side effects when conversation state changes.

    Returns the overridden status if the state was changed programmatically
    (e.g. onboarding_complete -> match_suggested), or None if no override.
    """
    overridden_status = None
    tenant_id = account.tenant_id

    if new_status == "onboarding_complete" and old_status != "onboarding_complete":
        from modules.bni.member_service import enrich_member_profile, find_best_match
        await enrich_member_profile(phone_number, info_fields, tenant_id)
        logger.info(f"Member profile enriched for {phone_number}")

        # Auto-suggest a 1-to-1 match right after onboarding
        match = await find_best_match(phone_number, tenant_id)
        if match:
            match_json_str = json.dumps(match)
            async with AsyncDBConnection(tenant_id) as conn:
                await conn.execute(
                    """
                    UPDATE conversation_states
                    SET context_status = 'match_suggested',
                        metadata = jsonb_set(
                            COALESCE(metadata, '{}')::jsonb,
                            '{match_json}',
                            $1::jsonb
                        ),
                        updated_at = NOW()
                    WHERE phone = $2
                    """,
                    match_json_str,
                    phone_number,
                )
            match_type = match.get("type", "unknown")
            members_count = len(match.get("members", []))
            logger.info(f"Match result for {phone_number}: type={match_type}, members={members_count}")
            overridden_status = "match_suggested"
        else:
            logger.info(f"No suitable match found for {phone_number} after onboarding")

    # Scrape website after ICP Q1 answer (icp_step transitions to 2)
    if new_status == "icp_discovery" or (old_status == "icp_discovery" and new_status == "icp_discovery"):
        icp_step = info_fields.get("icp_step")
        icp_answers = info_fields.get("icp_answers", {})
        q1_answer = icp_answers.get("q1", "")

        if icp_step == 2 and q1_answer:
            urls = re.findall(r'(?:https?://)?(?:www\.)?[\w.-]+\.\w{2,}(?:/\S*)?', q1_answer)
            if urls:
                from services.website_scraper import scrape_website_for_clients
                url = urls[0]
                logger.info(f"Scraping website from ICP Q1: {url}")
                scraped = await scrape_website_for_clients(url)

                if scraped.get("clients") or scraped.get("services"):
                    async with AsyncDBConnection(tenant_id) as conn:
                        await conn.execute(
                            """
                            UPDATE conversation_states
                            SET metadata = jsonb_set(
                                COALESCE(metadata, '{}')::jsonb,
                                '{website_data}',
                                $1::jsonb
                            ),
                            updated_at = NOW()
                            WHERE phone = $2
                            """,
                            json.dumps(scraped),
                            phone_number,
                        )
                    logger.info(
                        f"Website scraped for {phone_number}: "
                        f"{len(scraped.get('clients', []))} clients, "
                        f"{len(scraped.get('services', []))} services"
                    )

    if new_status == "coordination_a_availability" and old_status == "match_suggested":
        if info_fields.get("match_accepted"):
            from modules.bni.meeting_scheduler import initiate_meeting_from_match
            await initiate_meeting_from_match(phone_number, conversation_id, lead_id, account)

    if new_status == "kpi_query" and old_status != "kpi_query":
        from modules.bni.member_service import get_member_stats_json
        stats = await get_member_stats_json(phone_number, tenant_id)
        async with AsyncDBConnection(tenant_id) as conn:
            await conn.execute(
                """
                UPDATE conversation_states
                SET metadata = jsonb_set(COALESCE(metadata, '{}')::jsonb, '{stats_json}', $1::jsonb)
                WHERE phone = $2
                """,
                json.dumps(stats),
                phone_number,
            )

    return overridden_status


async def bni_create_state(
    account: WhatsAppAccount,
    phone_number: str,
    lead_id: str,
    contact_name: str,
) -> dict:
    """BNI-specific state creation: looks up existing member in community_roi_members
    to decide initial status (skip onboarding if profile exists).

    Returns the initial state dict.
    """
    tenant_id = account.tenant_id
    member_name = contact_name
    profile_data = {}
    initial_status = "onboarding_greeting"

    try:
        async with CoreDBConnection() as conn:
            existing = await conn.fetchrow(
                f"""
                SELECT name, company_name, industry, designation, metadata
                FROM {core_table('community_roi_members')}
                WHERE REPLACE(REPLACE(REPLACE(phone, ' ', ''), '+', ''), '-', '') = $1
                  AND tenant_id = $2::uuid AND is_deleted = false
                """,
                phone_number,
                tenant_id,
            )
            if existing:
                raw_name = existing["name"] or contact_name
                if "(" in raw_name:
                    raw_name = raw_name[:raw_name.index("(")].strip()
                member_name = raw_name

                profile_data["company_name"] = existing["company_name"]
                profile_data["industry"] = existing["industry"]
                profile_data["designation"] = existing["designation"]

                ex_meta = existing["metadata"]
                if isinstance(ex_meta, str):
                    try:
                        ex_meta = json.loads(ex_meta)
                    except Exception:
                        ex_meta = {}
                profile_data["services_offered"] = ex_meta.get("services_offered")

                if existing["company_name"] and existing["industry"]:
                    initial_status = "icp_discovery"
                    logger.info(
                        f"Existing member found for {phone_number}: {member_name} "
                        f"({existing['company_name']}). Skipping to icp_discovery."
                    )
                else:
                    initial_status = "onboarding_profile"
                    logger.info(
                        f"Existing member found for {phone_number}: {member_name} "
                        f"but profile incomplete. Starting onboarding_profile."
                    )
    except Exception as e:
        logger.warning(f"Could not look up existing member for {phone_number}: {e}")

    return {
        "contact_name": member_name,
        "context_status": initial_status,
        "profile_data": profile_data,
    }
