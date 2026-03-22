#!/usr/bin/env python3
"""
Interactive local tester for business WhatsApp API conversation flow.

Flow per user message:
1. Sends a webhook-shaped payload to /webhook/{slug}
2. Finds/uses conversation for the phone number via /api/conversations
3. Polls /api/conversations/{id}/messages for a new assistant reply

This tests the API conversation path (not personal WhatsApp path).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx


@dataclass
class Config:
    service_url: str
    slug: str
    tenant_id: str
    phone: str
    contact_name: str
    timeout_seconds: int
    poll_interval_seconds: float


def _parse_log_timestamp(line: str) -> Optional[datetime]:
    match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})", line)
    if not match:
        return None
    base = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    # Local runtime is UTC+4 in these logs; use offset-aware datetime for comparisons.
    offset = timezone.utc
    try:
        local_offset = datetime.now().astimezone().utcoffset()
        if local_offset is not None:
            offset = timezone(local_offset)
    except Exception:
        pass
    return base.replace(microsecond=int(match.group(2)) * 1000, tzinfo=offset)


def recent_local_send_error(
    phone: str,
    accepted_at_iso: Optional[str] = None,
    log_path: str = "/tmp/bni-service.log",
) -> Optional[str]:
    """Return a recent send/provider error line for a phone number if available."""
    if not os.path.exists(log_path):
        return None

    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-400:]
    except Exception:
        return None

    phone_digits = normalize_phone(phone)
    accepted_at = parse_iso(accepted_at_iso) if accepted_at_iso else None
    matches: list[str] = []

    for line in lines:
        ts = _parse_log_timestamp(line)
        if accepted_at and ts and ts < accepted_at:
            continue

        if phone_digits and phone_digits in re.sub(r"\D", "", line):
            if "WhatsApp send failed" in line or "Failed to send reply" in line:
                matches.append(line.strip())
            continue
        if accepted_at and ("WhatsApp send failed" in line or "Failed to send reply" in line):
            matches.append(line.strip())

    return matches[-1] if matches else None


def normalize_phone(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(dt: str) -> datetime:
    # FastAPI currently returns values like 2026-03-13T06:03:32.686387+00:00
    return datetime.fromisoformat(dt.replace("Z", "+00:00"))


async def check_health(client: httpx.AsyncClient, service_url: str) -> None:
    resp = await client.get(f"{service_url}/health", timeout=10)
    resp.raise_for_status()


async def find_conversation_id(
    client: httpx.AsyncClient,
    cfg: Config,
) -> Optional[str]:
    resp = await client.get(
        f"{cfg.service_url}/api/conversations",
        params={"search": normalize_phone(cfg.phone), "limit": 20, "offset": 0},
        headers={"X-Tenant-ID": cfg.tenant_id},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()

    if not payload.get("success"):
        return None

    rows = payload.get("data", [])
    target_digits = normalize_phone(cfg.phone)

    for row in rows:
        lead_phone = normalize_phone(str(row.get("lead_phone") or ""))
        if not lead_phone:
            continue
        if target_digits.endswith(lead_phone) or lead_phone.endswith(target_digits):
            return row.get("id")

    return rows[0].get("id") if rows else None


async def fetch_messages(
    client: httpx.AsyncClient,
    cfg: Config,
    conversation_id: str,
) -> list[dict]:
    resp = await client.get(
        f"{cfg.service_url}/api/conversations/{conversation_id}/messages",
        params={"limit": 200, "offset": 0},
        headers={"X-Tenant-ID": cfg.tenant_id},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        return []
    return payload.get("data", [])


async def fetch_conversation_owner(
    client: httpx.AsyncClient,
    cfg: Config,
    conversation_id: str,
) -> Optional[str]:
    resp = await client.get(
        f"{cfg.service_url}/api/conversations/{conversation_id}",
        headers={"X-Tenant-ID": cfg.tenant_id},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        return None
    return (payload.get("data") or {}).get("owner")


def latest_assistant(messages: list[dict]) -> Optional[dict]:
    assistant = [m for m in messages if m.get("role") == "assistant"]
    if not assistant:
        return None
    return assistant[-1]


async def send_webhook_message(
    client: httpx.AsyncClient,
    cfg: Config,
    text: str,
) -> tuple[int, dict]:
    from_phone = normalize_phone(cfg.phone)
    msg_id = f"wamid.local.{uuid.uuid4().hex[:20]}"

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "local-waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "local",
                                "phone_number_id": "local",
                            },
                            "contacts": [
                                {
                                    "profile": {"name": cfg.contact_name},
                                    "wa_id": from_phone,
                                }
                            ],
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": msg_id,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }

    resp = await client.post(
        f"{cfg.service_url}/webhook/{cfg.slug}",
        json=payload,
        timeout=20,
    )

    out = {"message_id": msg_id, "accepted_at": now_utc_iso()}
    try:
        out["response"] = resp.json()
    except Exception:
        out["response"] = resp.text

    return resp.status_code, out


async def wait_for_new_assistant_reply(
    client: httpx.AsyncClient,
    cfg: Config,
    conversation_id: str,
    baseline_assistant_id: Optional[str],
    accepted_at_iso: str,
) -> Optional[dict]:
    deadline = time.time() + cfg.timeout_seconds

    while time.time() < deadline:
        messages = await fetch_messages(client, cfg, conversation_id)
        latest = latest_assistant(messages)
        if latest:
            latest_id = latest.get("id")
            if latest_id and latest_id != baseline_assistant_id:
                created_at = latest.get("created_at")
                if created_at:
                    try:
                        if parse_iso(created_at) >= parse_iso(accepted_at_iso):
                            return latest
                    except Exception:
                        return latest
                else:
                    return latest
        await asyncio.sleep(cfg.poll_interval_seconds)

    return None


async def interactive_chat(cfg: Config) -> None:
    async with httpx.AsyncClient() as client:
        await check_health(client, cfg.service_url)

        print("API conversation interactive test")
        print(f"service: {cfg.service_url}")
        print(f"slug: {cfg.slug}")
        print(f"tenant: {cfg.tenant_id}")
        print(f"phone: {cfg.phone}")
        print("type '/quit' to exit")

        while True:
            user_text = input("you> ").strip()
            if not user_text:
                continue
            if user_text.lower() in {"/quit", "quit", "exit"}:
                print("bye")
                return

            conv_before = await find_conversation_id(client, cfg)
            baseline_assistant_id = None
            if conv_before:
                existing = await fetch_messages(client, cfg, conv_before)
                latest = latest_assistant(existing)
                baseline_assistant_id = latest.get("id") if latest else None

            status, meta = await send_webhook_message(client, cfg, user_text)
            print(f"webhook status: {status}, id: {meta['message_id']}")
            if status not in (200, 202):
                print(f"webhook error: {meta['response']}")
                continue

            conv_after = await find_conversation_id(client, cfg)
            if not conv_after:
                print("no conversation found yet for this phone")
                continue

            owner = await fetch_conversation_owner(client, cfg, conv_after)
            if owner == "human_agent":
                print(
                    "assistant> conversation owner is human_agent; "
                    "AI reply is intentionally skipped for this conversation"
                )
                continue

            reply = await wait_for_new_assistant_reply(
                client=client,
                cfg=cfg,
                conversation_id=conv_after,
                baseline_assistant_id=baseline_assistant_id,
                accepted_at_iso=meta["accepted_at"],
            )

            if reply:
                print(f"assistant> {reply.get('content', '').strip()}")
            else:
                err = recent_local_send_error(cfg.phone, accepted_at_iso=meta["accepted_at"])
                if err:
                    print(
                        "assistant> no new assistant DB message; likely outbound send failure. "
                        f"latest error: {err}"
                    )
                else:
                    print(
                        "assistant> no new assistant message found within timeout; "
                        "check /tmp/bni-service.log for send/provider errors"
                    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Interactive local API conversation flow tester")
    p.add_argument("--service-url", default="http://localhost:8000")
    p.add_argument("--slug", default="rising-phoenix")
    p.add_argument("--tenant-id", default="9ca4012a-2e02-5593-8cc1-fd5bd81483f9")
    p.add_argument("--phone", default="+971567376577")
    p.add_argument("--contact-name", default="Local Test User")
    p.add_argument("--timeout", type=int, default=45)
    p.add_argument("--poll-interval", type=float, default=1.0)
    return p


def main() -> None:
    args = build_parser().parse_args()
    cfg = Config(
        service_url=args.service_url.rstrip("/"),
        slug=args.slug,
        tenant_id=args.tenant_id,
        phone=args.phone,
        contact_name=args.contact_name,
        timeout_seconds=args.timeout,
        poll_interval_seconds=args.poll_interval,
    )
    asyncio.run(interactive_chat(cfg))


if __name__ == "__main__":
    main()
