"""
Tenant context middleware.
Extracts tenant_id from X-Tenant-ID header injected by the LAD UI proxy.
"""
import logging
from typing import Optional
from fastapi import Header, Request

logger = logging.getLogger(__name__)


async def get_tenant_id(
    request: Request,
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
    x_debug_trace_id: Optional[str] = Header(None, alias="X-Debug-Trace-Id"),
    x_debug_client_tenant: Optional[str] = Header(None, alias="X-Debug-Client-Tenant"),
) -> Optional[str]:
    """
    Extract tenant_id from X-Tenant-ID header.
    Returns None if header is missing (backward compatible during migration).
    """
    if not x_tenant_id:
        logger.debug("No X-Tenant-ID header - tenant filtering disabled for this request")

    if x_debug_trace_id:
        logger.info(
            "[TENANT_TRACE] trace=%s path=%s client_tenant=%s resolved_tenant=%s",
            x_debug_trace_id,
            request.url.path,
            x_debug_client_tenant or "none",
            x_tenant_id or "none",
        )

    return x_tenant_id
