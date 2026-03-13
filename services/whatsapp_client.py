from __future__ import annotations
"""
WhatsApp Business API client for sending messages.

Multi-tenant: All public functions accept an optional ChapterConfig.
When provided, uses chapter-specific WhatsApp credentials.
Falls back to environment variables for backward compatibility.
"""
import logging
import os
import re
import time
import uuid
import json
from typing import Optional

import httpx
from dotenv import load_dotenv

from db.connection import AsyncDBConnection, ClientDBConnection
from services.account_registry import WhatsAppAccount as ChapterConfig  # backward compat alias

load_dotenv()

logger = logging.getLogger(__name__)

# Fallback env var credentials (backward compat)
_FALLBACK_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
_FALLBACK_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
_FALLBACK_WABA_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "")

# Shared HTTP client
_http_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=15.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client


def _resolve_creds(chapter: Optional[ChapterConfig] = None) -> tuple[str, str, str]:
    """Resolve WhatsApp credentials from chapter or env vars.
    Returns (phone_number_id, access_token, api_url).
    """
    if chapter and chapter.whatsapp_phone_number_id and chapter.whatsapp_access_token:
        phone_id = chapter.whatsapp_phone_number_id
        token = chapter.whatsapp_access_token
    else:
        phone_id = _FALLBACK_PHONE_NUMBER_ID
        token = _FALLBACK_ACCESS_TOKEN

    api_url = f"https://graph.facebook.com/v22.0/{phone_id}/messages"
    return phone_id, token, api_url


def _get_headers(chapter: Optional[ChapterConfig] = None) -> dict:
    if chapter and chapter.whatsapp_access_token:
        return chapter.whatsapp_headers
    return {
        "Authorization": f"Bearer {_FALLBACK_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def _get_waba_id_sync(chapter: Optional[ChapterConfig] = None) -> str:
    if chapter and chapter.whatsapp_business_account_id:
        return chapter.whatsapp_business_account_id
    return _FALLBACK_WABA_ID


def _normalize_phone_number(phone_number: str) -> str:
    """Normalize phone number to WhatsApp Cloud API format (digits only)."""
    return re.sub(r"\D", "", phone_number or "")


async def mark_as_read(message_id: str, chapter: Optional[ChapterConfig] = None) -> None:
    """Mark an incoming message as read (shows blue ticks)."""
    phone_id, token, api_url = _resolve_creds(chapter)
    if not phone_id or not token:
        return
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    try:
        client = _get_client()
        await client.post(api_url, headers=_get_headers(chapter), json=payload)
    except Exception as e:
        logger.debug(f"mark_as_read failed: {e}")


# Cache for template body texts: {waba_id:template_name: body_text}
_template_body_cache: dict[str, str] = {}
_template_cache_loaded_for: set[str] = set()


async def get_message_templates(chapter: Optional[ChapterConfig] = None) -> list[dict]:
    """Fetch approved message templates from Meta Business API."""
    waba_id = _get_waba_id_sync(chapter)
    _, token, _ = _resolve_creds(chapter)

    if not waba_id or not token:
        logger.warning("Cannot fetch templates: WABA ID or token not available")
        return []

    templates_url = f"https://graph.facebook.com/v22.0/{waba_id}/message_templates"
    try:
        client = _get_client()
        response = await client.get(
            templates_url,
            headers=_get_headers(chapter),
            params={"status": "APPROVED", "limit": 100},
        )
        if response.status_code == 200:
            data = response.json()
            templates = []
            for t in data.get("data", []):
                body_text = ""
                params = []
                for comp in t.get("components", []):
                    if comp.get("type") == "BODY":
                        body_text = comp.get("text", "")
                        params = re.findall(r"\{\{(\d+)\}\}", body_text)

                cache_key = f"{waba_id}:{t['name']}"
                _template_body_cache[cache_key] = body_text

                templates.append({
                    "name": t["name"],
                    "language": t.get("language", "en_US"),
                    "status": t.get("status", ""),
                    "category": t.get("category", ""),
                    "body": body_text,
                    "parameter_count": len(params),
                })
            _template_cache_loaded_for.add(waba_id)
            return templates
        else:
            logger.error(f"Failed to fetch templates ({response.status_code}): {response.text}")
            return []
    except Exception as e:
        logger.error(f"Error fetching message templates: {e}")
        return []


async def _get_template_body(template_name: str, chapter: Optional[ChapterConfig] = None) -> str | None:
    waba_id = _get_waba_id_sync(chapter)
    cache_key = f"{waba_id}:{template_name}"
    if waba_id not in _template_cache_loaded_for:
        await get_message_templates(chapter)
    return _template_body_cache.get(cache_key)


async def send_template_message(
    phone_number: str,
    template_name: str,
    language_code: str = "en_GB",
    parameters: list[str] | None = None,
    conversation_id: str | None = None,
    lead_id: str | None = None,
    chapter: Optional[ChapterConfig] = None,
) -> str | None:
    """Send a WhatsApp message using a pre-approved template."""
    _, _, api_url = _resolve_creds(chapter)

    components = []
    if parameters:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": p} for p in parameters],
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": phone_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
        },
    }
    if components:
        payload["template"]["components"] = components

    try:
        client = _get_client()
        response = await client.post(api_url, headers=_get_headers(chapter), json=payload)

        if response.status_code in (200, 201):
            resp_data = response.json()
            wa_message_id = resp_data.get("messages", [{}])[0].get("id", "")
            internal_id = str(uuid.uuid4())

            template_body = await _get_template_body(template_name, chapter)
            if template_body and parameters:
                content = template_body
                for i, param_val in enumerate(parameters, start=1):
                    content = content.replace(f"{{{{{i}}}}}", param_val)
            elif template_body:
                content = template_body
            else:
                content = f"[Template: {template_name}]"
                if parameters:
                    content += f"\n{', '.join(str(p) for p in parameters)}"

            if conversation_id and lead_id:
                await _save_outgoing_message(
                    internal_id, wa_message_id, conversation_id, lead_id, content, chapter
                )

            slug = chapter.slug if chapter else "default"
            logger.info(f"[{slug}] Template '{template_name}' sent to {phone_number}: {wa_message_id}")
            return wa_message_id
        else:
            logger.error(f"Template send failed ({response.status_code}): {response.text}")
            return None

    except Exception as e:
        logger.error(f"Error sending template to {phone_number}: {e}")
        return None


async def send_message(
    phone_number: str,
    text: str,
    conversation_id: str | None = None,
    lead_id: str | None = None,
    chapter: Optional[ChapterConfig] = None,
) -> str | None:
    """Send a WhatsApp text message and save to DB."""
    _, _, api_url = _resolve_creds(chapter)
    t0 = time.time()
    normalized_phone = _normalize_phone_number(phone_number)

    if not normalized_phone:
        logger.error("WhatsApp send aborted: invalid phone number after normalization", extra={"phone": phone_number})
        return None

    payload = {
        "messaging_product": "whatsapp",
        "to": normalized_phone,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }

    try:
        client = _get_client()
        response = await client.post(api_url, headers=_get_headers(chapter), json=payload)
        slug = chapter.slug if chapter else "default"
        logger.info(f"[{slug}][TIMING] whatsapp_api_post: {time.time()-t0:.3f}s")

        if response.status_code in (200, 201):
            resp_data = response.json()
            wa_message_id = resp_data.get("messages", [{}])[0].get("id", "")
            internal_id = str(uuid.uuid4())

            if conversation_id and lead_id:
                t1 = time.time()
                await _save_outgoing_message(
                    internal_id, wa_message_id, conversation_id, lead_id, text, chapter
                )
                logger.info(f"[{slug}][TIMING] save_outgoing_message_db: {time.time()-t1:.3f}s")

            logger.info(f"[{slug}] Message sent to {normalized_phone}: {wa_message_id}")
            return wa_message_id
        else:
            # Retry with a minimal payload if Meta rejects a parameter.
            # This helps isolate fields like preview_url that can be account-version sensitive.
            retry_response = None
            error_code = None
            error_details = ""
            try:
                err_json = response.json()
                error_code = err_json.get("error", {}).get("code")
                error_details = err_json.get("error", {}).get("error_data", {}).get("details", "")
            except Exception:
                err_json = None

            if response.status_code == 400 and error_code == 100:
                minimal_payload = {
                    "messaging_product": "whatsapp",
                    "to": normalized_phone,
                    "type": "text",
                    "text": {"body": text},
                }

                retry_t0 = time.time()
                retry_response = await client.post(
                    api_url,
                    headers=_get_headers(chapter),
                    json=minimal_payload,
                )
                logger.info(
                    f"[{slug}][TIMING] whatsapp_api_post_retry_minimal: {time.time()-retry_t0:.3f}s"
                )

                if retry_response.status_code in (200, 201):
                    resp_data = retry_response.json()
                    wa_message_id = resp_data.get("messages", [{}])[0].get("id", "")
                    internal_id = str(uuid.uuid4())

                    if conversation_id and lead_id:
                        t1 = time.time()
                        await _save_outgoing_message(
                            internal_id, wa_message_id, conversation_id, lead_id, text, chapter
                        )
                        logger.info(f"[{slug}][TIMING] save_outgoing_message_db: {time.time()-t1:.3f}s")

                    logger.info(
                        f"[{slug}] Message sent on minimal retry to {normalized_phone}: {wa_message_id}"
                    )
                    return wa_message_id

            log_extra = {
                "to": normalized_phone,
                "api_url": api_url,
                "text_len": len(text),
                "error_code": error_code,
                "error_details": error_details,
            }
            if err_json is not None:
                log_extra["error_json"] = json.dumps(err_json)
            if retry_response is not None:
                log_extra["retry_status"] = retry_response.status_code
                log_extra["retry_body"] = retry_response.text

            logger.error(
                f"WhatsApp send failed ({response.status_code}): {response.text}",
                extra=log_extra,
            )
            return None

    except Exception as e:
        logger.error(f"Error sending WhatsApp message to {phone_number}: {e}")
        return None


async def _save_outgoing_message(
    internal_id: str,
    external_message_id: str,
    conversation_id: str,
    lead_id: str,
    content: str,
    chapter: Optional[ChapterConfig] = None,
):
    """Save an outgoing AI message to the messages table."""
    try:
        if chapter:
            async with AsyncDBConnection(chapter.tenant_id) as conn:
                await conn.execute(
                    """
                    INSERT INTO messages (id, conversation_id, lead_id, role, content,
                        message_status, external_message_id, tenant_id, created_at)
                    VALUES ($1::uuid, $2::uuid, $3::uuid, 'agent', $4, 'sent', $5, $6::uuid, NOW())
                    """,
                    internal_id, conversation_id, lead_id, content,
                    external_message_id, chapter.tenant_id,
                )
        else:
            async with ClientDBConnection() as conn:
                await conn.execute(
                    """
                    INSERT INTO messages (id, conversation_id, lead_id, role, content,
                        message_status, external_message_id, created_at)
                    VALUES ($1::uuid, $2::uuid, $3::uuid, 'agent', $4, 'sent', $5, NOW())
                    """,
                    internal_id, conversation_id, lead_id, content, external_message_id,
                )
    except Exception as e:
        logger.error(f"Error saving outgoing message: {e}")
