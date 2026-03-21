"""
Dual-pool database connections for the WhatsApp Agent Service.

Two pools:
  - client_pool → client DB (read/write conversations, messages, wa_contacts)
  - core_pool   → salesmaya_agent (read tenants, social_whatsapp_accounts, tenant_database_config)

Connection classes:
  - ClientDBConnection  → client DB (client feature tables, legacy single-tenant)
  - CoreDBConnection    → salesmaya_agent (shared/core tables)
  - AsyncDBConnection   → per-tenant DB (multi-tenant routing via tenant_database_config)

Backward-compatible aliases:
  - BNIDBConnection     = ClientDBConnection
  - AgentDBConnection   = CoreDBConnection
"""
import asyncpg
import asyncio
import logging
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# Import schema helper AFTER load_dotenv so env vars are available
# Avoids hardcoding lad_dev.* in SQL strings (Architecture Rule I.3)
from db.schema import core_table  # noqa: E402

logger = logging.getLogger(__name__)

_client_pool: Optional[asyncpg.Pool] = None
_core_pool: Optional[asyncpg.Pool] = None


async def init_pools():
    """Initialize both connection pools."""
    global _client_pool, _core_pool

    # Support CLIENT_DB_URL (new generic name) and BNI_DB_URL (legacy backward compat)
    client_url = os.getenv("CLIENT_DB_URL") or os.getenv("BNI_DB_URL")
    core_url = os.getenv("AGENT_DB_URL")

    if not client_url:
        raise RuntimeError("CLIENT_DB_URL (or legacy BNI_DB_URL) is not set")
    if not core_url:
        raise RuntimeError("AGENT_DB_URL is not set")

    _client_pool = await asyncpg.create_pool(
        dsn=client_url,
        min_size=2,
        max_size=10,
        command_timeout=30,
        server_settings={"application_name": "wa_agent_service"},
    )
    logger.info("Client database pool created")

    _core_pool = await asyncpg.create_pool(
        dsn=core_url,
        min_size=1,
        max_size=5,
        command_timeout=30,
        server_settings={"application_name": "wa_agent_service_reader"},
    )
    logger.info("Core database pool created (salesmaya_agent)")

    # Load tenant configs for multi-tenant support
    await _load_tenant_config()
    if _DEFAULT_TENANT_DB_URL:
        await _get_or_create_tenant_pool(_DEFAULT_TENANT_DB_URL)
        logger.info("Default tenant database pool created")


async def close_pools():
    """Close all connection pools (fixed + tenant)."""
    global _client_pool, _core_pool
    if _client_pool:
        await _client_pool.close()
        _client_pool = None
    if _core_pool:
        await _core_pool.close()
        _core_pool = None

    # Close tenant pools
    for url, pool in _tenant_pools.items():
        try:
            await pool.close()
        except Exception as e:
            logger.error(f"Error closing tenant pool: {e}")
    _tenant_pools.clear()
    _tenant_db_urls.clear()

    logger.info("All database pools closed")


class ClientDBConnection:
    """Connection to salesmaya_bni (conversations, messages, meetings)."""

    def __init__(self):
        self.conn = None

    async def __aenter__(self):
        global _client_pool
        if _client_pool is None:
            raise RuntimeError("Client database pool not initialized")
        self.conn = await _client_pool.acquire()
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        global _client_pool
        if self.conn and _client_pool:
            await _client_pool.release(self.conn)


class CoreDBConnection:
    """Connection to salesmaya_agent (core tables: community_roi_*, tenants, users)."""

    def __init__(self):
        self.conn = None

    async def __aenter__(self):
        global _core_pool
        if _core_pool is None:
            raise RuntimeError("Core database pool not initialized")
        self.conn = await _core_pool.acquire()
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        global _core_pool
        if self.conn and _core_pool:
            await _core_pool.release(self.conn)


# Backward-compatible aliases
BNIDBConnection = ClientDBConnection
AgentDBConnection = CoreDBConnection


# ====================
# Multi-tenant support (merged from unified-comms)
# ====================

# Config DB URL (salesmaya_agent, where lad_dev.tenant_database_config lives)
# NOTE: Always set CONFIG_DB_URL env var in production; this fallback is for local dev only.
_CONFIG_DB_URL = os.getenv(
    "CONFIG_DB_URL",
    "postgresql://dbadmin:TechieMaya%240326@165.22.221.77:5432/salesmaya_agent",
)

# Fallback DB URL when no tenant_id is provided
_DEFAULT_TENANT_DB_URL = os.getenv("POSTGRES_DB_URL")

# Cache: tenant_id -> database_url
_tenant_db_urls: dict[str, str] = {}

# Pool registry: database_url -> pool
_tenant_pools: dict[str, asyncpg.Pool] = {}

# Lock for pool creation
_tenant_pool_lock = asyncio.Lock()


class TenantNotConfiguredError(Exception):
    """Raised when a tenant has no database configured."""
    pass


async def _load_tenant_config():
    """Load tenant-to-database mappings from lad_dev.tenant_database_config.

    Also seeds the legacy single-tenant mapping so accounts loaded from the old
    lad_dev.chapters fallback table still work even if they were never added to
    tenant_database_config.
    """
    global _tenant_db_urls
    try:
        conn = await asyncpg.connect(_CONFIG_DB_URL)
        rows = await conn.fetch(
            f"SELECT tenant_id::text, database_url FROM {core_table('tenant_database_config')}"
        )
        await conn.close()
        _tenant_db_urls = {row["tenant_id"]: row["database_url"] for row in rows}
        logger.info(f"Loaded {len(_tenant_db_urls)} tenant database configs")
    except Exception as e:
        logger.error(f"Failed to load tenant config: {e}")

    # Seed legacy single-tenant mapping.
    # Tenants originally created via lad_dev.chapters (before tenant_database_config existed)
    # won't have a row in tenant_database_config.  Map them to CLIENT_DB_URL so the
    # conversations/messages APIs continue to work without a DB migration.
    _default_tenant_id = os.getenv("DEFAULT_TENANT_ID") or os.getenv("BNI_TENANT_ID")
    _client_db_url = os.getenv("CLIENT_DB_URL") or os.getenv("BNI_DB_URL")
    if _default_tenant_id and _client_db_url and _default_tenant_id not in _tenant_db_urls:
        _tenant_db_urls[_default_tenant_id] = _client_db_url
        logger.info(
            f"Seeded legacy tenant {_default_tenant_id} → CLIENT_DB_URL "
            f"(not present in tenant_database_config — add a row there to silence this)"
        )


async def _get_or_create_tenant_pool(db_url: str) -> asyncpg.Pool:
    """Get an existing pool for a DB URL, or create a new one."""
    if db_url in _tenant_pools and not _tenant_pools[db_url]._closed:
        return _tenant_pools[db_url]

    async with _tenant_pool_lock:
        if db_url in _tenant_pools and not _tenant_pools[db_url]._closed:
            return _tenant_pools[db_url]

        logger.info(f"Creating tenant pool for: {db_url[:50]}...")
        pool = await asyncpg.create_pool(
            dsn=db_url,
            min_size=1,
            max_size=10,
            command_timeout=30,
            server_settings={"application_name": "wa_agent_service_tenant"},
        )
        _tenant_pools[db_url] = pool
        return pool


def _resolve_tenant_db_url(tenant_id: Optional[str]) -> str:
    """Resolve tenant_id to a database URL."""
    if tenant_id and tenant_id in _tenant_db_urls:
        return _tenant_db_urls[tenant_id]

    if tenant_id:
        # Safety-net fallback: if this tenant is not in tenant_database_config but a
        # default (legacy) DB URL is set, use it so old single-tenant setups keep working.
        # In a fully-migrated multi-tenant deployment this branch should never be hit.
        _client_db_url = os.getenv("CLIENT_DB_URL") or os.getenv("BNI_DB_URL")
        if _client_db_url:
            logger.warning(
                f"Tenant {tenant_id} not found in tenant_database_config. "
                f"Falling back to CLIENT_DB_URL. "
                f"Fix: add this tenant via POST /admin/whatsapp-accounts."
            )
            return _client_db_url
        raise TenantNotConfiguredError(
            f"No database configured for tenant {tenant_id}. "
            f"Register it via POST /admin/whatsapp-accounts."
        )

    if _DEFAULT_TENANT_DB_URL:
        return _DEFAULT_TENANT_DB_URL

    raise RuntimeError("No database URL available (no tenant config and no POSTGRES_DB_URL)")


async def reload_tenant_config():
    """Reload tenant configs (call after adding new tenants)."""
    await _load_tenant_config()


class AsyncDBConnection:
    """Async context manager that routes to the correct tenant database."""

    def __init__(self, tenant_id: Optional[str] = None):
        self.tenant_id = tenant_id
        self.conn = None
        self.pool = None

    async def __aenter__(self):
        db_url = _resolve_tenant_db_url(self.tenant_id)
        self.pool = await _get_or_create_tenant_pool(db_url)
        self.conn = await self.pool.acquire()
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        if self.conn and self.pool:
            await self.pool.release(self.conn)
