"""
Dynamic schema resolution for multi-tenant architecture.

Core tables (tenants, users, community_roi_*) live in salesmaya_agent
under a schema resolved from the tenant context.

Client tables (conversations, messages, meetings) live in salesmaya_bni
under the public schema.

Usage:
    from db.schema import core_table, get_tenant_id

    # Resolves to "lad_dev.community_roi_members" (or whatever schema is configured)
    query = f"SELECT * FROM {core_table('community_roi_members')} WHERE tenant_id = $1"

    # Get tenant ID from environment
    tenant_id = get_tenant_id()
"""
import os
import logging

logger = logging.getLogger(__name__)

# Schema for core tables in salesmaya_agent database.
# Resolved from environment — never hardcoded in queries.
_CORE_SCHEMA = os.getenv("CORE_DB_SCHEMA", "lad_dev")

# BNI tenant ID — resolved from environment.
_TENANT_ID = os.getenv("BNI_TENANT_ID", "9ca4012a-2e02-5593-8cc1-fd5bd81483f9")


def core_table(table_name: str) -> str:
    """Return fully qualified table name for a core (salesmaya_agent) table.

    Example: core_table("community_roi_members") → "lad_dev.community_roi_members"
    """
    return f"{_CORE_SCHEMA}.{table_name}"


def get_tenant_id() -> str:
    """Return the current tenant ID."""
    return _TENANT_ID


def get_core_schema() -> str:
    """Return the core schema name."""
    return _CORE_SCHEMA
