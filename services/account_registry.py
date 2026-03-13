"""
WhatsApp Account Registry

Loads and caches WhatsApp account configurations from
lad_dev.social_whatsapp_accounts table. Each account represents
a client's WhatsApp integration with its own credentials,
AI model preferences, and conversation flow template.

A single tenant can have BOTH a business WhatsApp account (Meta Cloud API)
and a personal WhatsApp account (Baileys bridge). The registry indexes
accounts by (tenant_id, channel) so each channel resolves independently.
"""
from __future__ import annotations

import asyncpg
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_CONFIG_DB_URL = os.getenv(
    "CONFIG_DB_URL",
    os.getenv("AGENT_DB_URL", "postgresql://dbadmin:TechieMaya@165.22.221.77:5432/salesmaya_agent"),
)

# Channel type constants
CHANNEL_BUSINESS = "business_whatsapp"
CHANNEL_PERSONAL = "personal_whatsapp"

# In-memory cache
_accounts_by_slug: dict[str, WhatsAppAccount] = {}
_accounts_by_tenant: dict[str, list[WhatsAppAccount]] = {}  # tenant -> [accounts]
_accounts_by_tenant_channel: dict[tuple[str, str], WhatsAppAccount] = {}  # (tenant, channel) -> account
_accounts_by_phone_id: dict[str, WhatsAppAccount] = {}


@dataclass
class WhatsAppAccount:
    """Configuration for a single WhatsApp account (any industry)."""
    id: str
    tenant_id: str
    slug: str
    display_name: str
    phone_number_id: str = ""
    access_token: str = ""
    business_account_id: str = ""
    verify_token: str = ""
    ai_model: str = "gemini-2.5-flash"
    ai_api_key: Optional[str] = None
    timezone: str = "UTC"
    conversation_flow_template: str = "generic"
    status: str = "active"
    metadata: dict = field(default_factory=dict)

    @property
    def channel_type(self) -> str:
        """Derive channel type from metadata."""
        return self.metadata.get("channel", CHANNEL_BUSINESS)

    # Backward compat: alias for code that still uses chapter naming
    @property
    def name(self) -> str:
        return self.display_name

    @property
    def whatsapp_phone_number_id(self) -> str:
        return self.phone_number_id

    @property
    def whatsapp_access_token(self) -> str:
        return self.access_token

    @property
    def whatsapp_business_account_id(self) -> str:
        return self.business_account_id

    @property
    def whatsapp_verify_token(self) -> str:
        return self.verify_token

    @property
    def whatsapp_api_url(self) -> str:
        return f"https://graph.facebook.com/v22.0/{self.phone_number_id}/messages"

    @property
    def whatsapp_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }


def _row_to_account(row) -> WhatsAppAccount:
    return WhatsAppAccount(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        slug=row["slug"],
        display_name=row["display_name"],
        phone_number_id=row["phone_number_id"] or "",
        access_token=row["access_token"] or "",
        business_account_id=row["business_account_id"] or "",
        verify_token=row["verify_token"] or "",
        ai_model=row["ai_model"] or "gemini-2.5-flash",
        ai_api_key=row["ai_api_key"],
        timezone=row["timezone"] or "UTC",
        conversation_flow_template=row["conversation_flow_template"] or "generic",
        status=row["status"] or "active",
        metadata=json.loads(row["metadata"]) if isinstance(row["metadata"], str) else (dict(row["metadata"]) if row["metadata"] else {}),
    )


def _index_account(account: WhatsAppAccount, by_slug, by_tenant, by_tenant_channel, by_phone):
    """Add one account to all index dicts."""
    by_slug[account.slug] = account

    by_tenant.setdefault(account.tenant_id, []).append(account)

    channel = account.channel_type
    by_tenant_channel[(account.tenant_id, channel)] = account

    if account.phone_number_id:
        by_phone[account.phone_number_id] = account


async def load_accounts():
    """Load all active WhatsApp accounts from DB into memory cache."""
    global _accounts_by_slug, _accounts_by_tenant, _accounts_by_tenant_channel, _accounts_by_phone_id

    try:
        conn = await asyncpg.connect(_CONFIG_DB_URL)
        rows = await conn.fetch(
            "SELECT * FROM lad_dev.social_whatsapp_accounts WHERE status = 'active'"
        )
        await conn.close()

        by_slug: dict[str, WhatsAppAccount] = {}
        by_tenant: dict[str, list[WhatsAppAccount]] = {}
        by_tenant_channel: dict[tuple[str, str], WhatsAppAccount] = {}
        by_phone: dict[str, WhatsAppAccount] = {}

        for row in rows:
            account = _row_to_account(row)
            _index_account(account, by_slug, by_tenant, by_tenant_channel, by_phone)

        _accounts_by_slug = by_slug
        _accounts_by_tenant = by_tenant
        _accounts_by_tenant_channel = by_tenant_channel
        _accounts_by_phone_id = by_phone

        logger.info(
            f"Loaded {len(by_slug)} active WhatsApp accounts: {list(by_slug.keys())}. "
            f"Tenant-channel pairs: {list(by_tenant_channel.keys())}"
        )
    except Exception as e:
        logger.error(f"Failed to load WhatsApp accounts: {e}")
        # Try loading from legacy chapters table as fallback
        await _load_from_chapters_fallback()


async def _load_from_chapters_fallback():
    """Fallback: load from lad_dev.chapters if social_whatsapp_accounts doesn't exist yet."""
    global _accounts_by_slug, _accounts_by_tenant, _accounts_by_tenant_channel, _accounts_by_phone_id

    try:
        conn = await asyncpg.connect(_CONFIG_DB_URL)
        rows = await conn.fetch(
            "SELECT * FROM lad_dev.chapters WHERE status = 'active'"
        )
        await conn.close()

        by_slug: dict[str, WhatsAppAccount] = {}
        by_tenant: dict[str, list[WhatsAppAccount]] = {}
        by_tenant_channel: dict[tuple[str, str], WhatsAppAccount] = {}
        by_phone: dict[str, WhatsAppAccount] = {}

        for row in rows:
            account = WhatsAppAccount(
                id=str(row["id"]),
                tenant_id=str(row["tenant_id"]),
                slug=row["slug"],
                display_name=row["name"],
                phone_number_id=row["whatsapp_phone_number_id"] or "",
                access_token=row["whatsapp_access_token"] or "",
                business_account_id=row["whatsapp_business_account_id"] or "",
                verify_token=row["whatsapp_verify_token"] or "",
                ai_model=row["ai_model"] or "gemini-2.5-flash",
                ai_api_key=row["ai_api_key"],
                timezone=row["timezone"] or "Asia/Dubai",
                conversation_flow_template="bni",  # chapters are always BNI
                status=row["status"] or "active",
                metadata=json.loads(row["metadata"]) if isinstance(row["metadata"], str) else (dict(row["metadata"]) if row["metadata"] else {}),
            )
            _index_account(account, by_slug, by_tenant, by_tenant_channel, by_phone)

        _accounts_by_slug = by_slug
        _accounts_by_tenant = by_tenant
        _accounts_by_tenant_channel = by_tenant_channel
        _accounts_by_phone_id = by_phone

        logger.warning(f"Loaded {len(by_slug)} accounts from legacy chapters table (fallback)")
    except Exception as e:
        logger.error(f"Chapters fallback also failed: {e}")
        _create_fallback_from_env()


def _create_fallback_from_env():
    """Raise error if database load fails (multi-tenant requires explicit config)."""
    global _accounts_by_slug, _accounts_by_tenant, _accounts_by_tenant_channel, _accounts_by_phone_id

    # DO NOT create hardcoded fallback - multi-tenant requires explicit DB config
    _accounts_by_slug = {}
    _accounts_by_tenant = {}
    _accounts_by_tenant_channel = {}
    _accounts_by_phone_id = {}

    logger.error(
        "CRITICAL: No WhatsApp accounts loaded from database. "
        "Multi-tenant service requires explicit configuration in lad_dev.social_whatsapp_accounts. "
        "Each tenant must have a registered account with slug, credentials, and flow template."
    )


async def reload_accounts():
    """Reload accounts (call after adding/updating accounts)."""
    await load_accounts()


def get_account_by_slug(slug: str) -> Optional[WhatsAppAccount]:
    """Look up account by URL slug."""
    return _accounts_by_slug.get(slug)


def get_account_by_tenant_id(tenant_id: str) -> Optional[WhatsAppAccount]:
    """Look up account by tenant ID.

    If a tenant has multiple accounts (business + personal), returns the
    business account by default. Use get_account_by_tenant_and_channel()
    for explicit channel resolution.
    """
    accounts = _accounts_by_tenant.get(tenant_id, [])
    if not accounts:
        return None
    # Prefer business account as default
    for acc in accounts:
        if acc.channel_type == CHANNEL_BUSINESS:
            return acc
    return accounts[0]


def get_account_by_tenant_and_channel(
    tenant_id: str, channel: str,
) -> Optional[WhatsAppAccount]:
    """Look up account by tenant ID and channel type.

    This is the preferred lookup when the channel is known (e.g., personal
    webhook always knows it's personal_whatsapp).

    Falls back to get_account_by_tenant_id if no channel-specific match.
    """
    account = _accounts_by_tenant_channel.get((tenant_id, channel))
    if account:
        return account
    # Fallback: tenant may have a single account that handles both channels
    return get_account_by_tenant_id(tenant_id)


def get_accounts_for_tenant(tenant_id: str) -> list[WhatsAppAccount]:
    """Get all accounts for a tenant (business + personal)."""
    return list(_accounts_by_tenant.get(tenant_id, []))


def get_account_by_phone_number_id(phone_id: str) -> Optional[WhatsAppAccount]:
    """Look up account by WhatsApp phone number ID."""
    return _accounts_by_phone_id.get(phone_id)


def get_all_active_accounts() -> list[WhatsAppAccount]:
    """Get all active WhatsApp accounts."""
    return list(_accounts_by_slug.values())


def get_default_account() -> Optional[WhatsAppAccount]:
    """Get the default/first account (backward compat)."""
    if _accounts_by_slug:
        return next(iter(_accounts_by_slug.values()))
    return None


def get_accounts_by_flow(flow_template: str) -> list[WhatsAppAccount]:
    """Get all active accounts using a specific flow template."""
    return [a for a in _accounts_by_slug.values() if a.conversation_flow_template == flow_template]
