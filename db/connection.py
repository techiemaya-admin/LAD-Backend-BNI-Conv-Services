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
import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_client_pool: asyncpg.Pool | None = None
_core_pool: asyncpg.Pool | None = None


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


async def close_pools():
    """Close both connection pools."""
    global _client_pool, _core_pool
    if _client_pool:
        await _client_pool.close()
        _client_pool = None
    if _core_pool:
        await _core_pool.close()
        _core_pool = None
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
