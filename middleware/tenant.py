"""
Tenant context middleware.
Extracts tenant_id from X-Tenant-ID header injected by the LAD UI proxy.
"""
import logging
from typing import Optional
from fastapi import Header

logger = logging.getLogger(__name__)


async def get_tenant_id(
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
) -> Optional[str]:
    """
    Extract tenant_id from X-Tenant-ID header.
    Returns None if header is missing (backward compatible during migration).
    """
    if not x_tenant_id:
        logger.debug("No X-Tenant-ID header - tenant filtering disabled for this request")
    return x_tenant_id
