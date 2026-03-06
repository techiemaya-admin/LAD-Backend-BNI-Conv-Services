"""
Member repository — data access for community_roi_members (core DB).

All SQL queries for member lookup, enrichment, and stats live here.
"""
import json
import logging

from db.connection import CoreDBConnection, ClientDBConnection
from db.schema import core_table, get_tenant_id

logger = logging.getLogger(__name__)


async def find_member_by_phone(phone_number: str) -> dict | None:
    """Look up a community_roi_members record by normalized phone."""
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
        return dict(row) if row else None


async def find_member_id_by_phone(phone_number: str) -> str | None:
    """Return just the member UUID for a phone number, or None."""
    tenant_id = get_tenant_id()
    async with CoreDBConnection() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT id FROM {core_table('community_roi_members')}
            WHERE REPLACE(REPLACE(REPLACE(phone, ' ', ''), '+', ''), '-', '') = $1
              AND tenant_id = $2::uuid AND is_deleted = false
            """,
            phone_number,
            tenant_id,
        )
        return str(row["id"]) if row else None


async def find_member_with_profile(phone_number: str) -> dict | None:
    """Look up a member with full profile fields for conversation state creation."""
    tenant_id = get_tenant_id()
    async with CoreDBConnection() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT name, company_name, industry, designation, metadata
            FROM {core_table('community_roi_members')}
            WHERE REPLACE(REPLACE(REPLACE(phone, ' ', ''), '+', ''), '-', '') = $1
              AND tenant_id = $2::uuid AND is_deleted = false
            """,
            phone_number,
            tenant_id,
        )
        return dict(row) if row else None


async def update_member_profile(member_id: str, company_name: str | None,
                                 industry: str | None, designation: str | None,
                                 metadata_patch: dict):
    """Update profile fields and merge metadata on a community_roi_members record."""
    async with CoreDBConnection() as conn:
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
            company_name,
            industry,
            designation,
            json.dumps(metadata_patch),
            member_id,
        )


async def get_member_stats(phone_number: str) -> dict | None:
    """Fetch KPI stats for a member."""
    tenant_id = get_tenant_id()
    async with CoreDBConnection() as conn:
        row = await conn.fetchrow(
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
        return dict(row) if row else None


async def get_member_with_matching_fields(phone_number: str) -> dict | None:
    """Get member with fields needed for ICP matching."""
    tenant_id = get_tenant_id()
    async with CoreDBConnection() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT id, name, company_name, industry, metadata
            FROM {core_table('community_roi_members')}
            WHERE REPLACE(REPLACE(REPLACE(phone, ' ', ''), '+', ''), '-', '') = $1
              AND tenant_id = $2::uuid AND is_deleted = false
            """,
            phone_number,
            tenant_id,
        )
        return dict(row) if row else None


async def get_all_active_members_except(phone_number: str) -> list[dict]:
    """Get all active members except the one with the given phone."""
    tenant_id = get_tenant_id()
    async with CoreDBConnection() as conn:
        rows = await conn.fetch(
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
        return [dict(r) for r in rows]


async def get_relationship_scores(member_id) -> list[dict]:
    """Get bidirectional relationship scores for a member."""
    tenant_id = get_tenant_id()
    async with CoreDBConnection() as conn:
        rows = await conn.fetch(
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
            member_id,
        )
        return [dict(r) for r in rows]


async def get_random_members_except(phone_number: str, limit: int = 5) -> list[dict]:
    """Get random active members (fallback when no ICP match)."""
    tenant_id = get_tenant_id()
    async with CoreDBConnection() as conn:
        rows = await conn.fetch(
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
        return [dict(r) for r in rows]
