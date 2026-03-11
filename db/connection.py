"""
Dual-pool database connections for the BNI Conversation Service.

Two pools:
  - client_pool → salesmaya_bni (read/write conversations, messages, meetings)
  - core_pool   → salesmaya_agent (read community_roi_members, relationship_scores)

Connection classes:
  - ClientDBConnection  → salesmaya_bni (client feature tables)
  - CoreDBConnection    → salesmaya_agent (shared/core tables)

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

logger = logging.getLogger(__name__)

_client_pool: Optional[asyncpg.Pool] = None
_core_pool: Optional[asyncpg.Pool] = None


async def init_pools():
    """Initialize both connection pools."""
    global _client_pool, _core_pool

    client_url = os.getenv("BNI_DB_URL")
    core_url = os.getenv("AGENT_DB_URL")

    if not client_url:
        raise RuntimeError("BNI_DB_URL is not set")
    if not core_url:
        raise RuntimeError("AGENT_DB_URL is not set")

    _client_pool = await asyncpg.create_pool(
        dsn=client_url,
        min_size=2,
        max_size=10,
        command_timeout=30,
        server_settings={"application_name": "bni_conversation_service"},
    )
    logger.info("Client database pool created (salesmaya_bni)")

    _core_pool = await asyncpg.create_pool(
        dsn=core_url,
        min_size=1,
        max_size=5,
        command_timeout=30,
        server_settings={"application_name": "bni_conversation_service_reader"},
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
_CONFIG_DB_URL = os.getenv(
    "CONFIG_DB_URL",
    "postgresql://dbadmin:TechieMaya@165.22.221.77:5432/salesmaya_agent",
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
    """Load tenant-to-database mappings from lad_dev.tenant_database_config."""
    global _tenant_db_urls
    try:
        conn = await asyncpg.connect(_CONFIG_DB_URL)
        rows = await conn.fetch(
            "SELECT tenant_id::text, database_url FROM lad_dev.tenant_database_config"
        )
        await conn.close()
        _tenant_db_urls = {row["tenant_id"]: row["database_url"] for row in rows}
        logger.info(f"Loaded {len(_tenant_db_urls)} tenant database configs")
    except Exception as e:
        logger.error(f"Failed to load tenant config: {e}")


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
            server_settings={"application_name": "bni_conversation_service_tenant"},
        )
        _tenant_pools[db_url] = pool
        return pool


def _resolve_tenant_db_url(tenant_id: Optional[str]) -> str:
    """Resolve tenant_id to a database URL."""
    if tenant_id and tenant_id in _tenant_db_urls:
        return _tenant_db_urls[tenant_id]

    if tenant_id:
        raise TenantNotConfiguredError(
            f"No database configured for tenant {tenant_id}"
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
