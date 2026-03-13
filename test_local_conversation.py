"""
Local test script for Conversation Service.

Tests the full pipeline end-to-end for BOTH channels:
  - Business WhatsApp (Meta Cloud API) — mocks the HTTP send
  - Personal WhatsApp (Baileys bridge) — mocks the HTTP send

Pipeline exercised:  account registry → DB lead/conversation → dedup →
  debounce → LLM (Gemini/OpenAI) → channel router → mock send → DB save

Usage:
    python3 test_local_conversation.py -i                          # interactive (business WA, default account)
    python3 test_local_conversation.py -i --channel personal       # interactive (personal WA)
    python3 test_local_conversation.py -i --slug rising-phoenix    # specify account by slug
    python3 test_local_conversation.py "Hello there"               # single message
    python3 test_local_conversation.py --phone 919999999999 "Hi"   # custom phone
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
import uuid
import sys
import os
from unittest.mock import AsyncMock, patch
from dataclasses import replace

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test")


# ---------------------------------------------------------------------------
# Mock WhatsApp sends — prints instead of calling external APIs
# ---------------------------------------------------------------------------

_mock_business_send = AsyncMock()
_mock_personal_send = AsyncMock()


def _setup_mocks():
    """Configure mocks so they return fake message IDs and print the reply."""

    async def mock_wa_send(phone_number, text, conversation_id=None, lead_id=None, chapter=None):
        fake_id = f"wamid.mock_{uuid.uuid4().hex[:12]}"
        slug = chapter.slug if chapter else "default"
        print(f"\n  [MOCK BUSINESS WA] -> {phone_number}")
        print(f"  [MOCK BUSINESS WA] msg_id: {fake_id}")

        # Still save to DB like the real client does
        if conversation_id and lead_id and chapter:
            from services.whatsapp_client import _save_outgoing_message
            await _save_outgoing_message(
                str(uuid.uuid4()), fake_id, conversation_id, lead_id, text, chapter
            )
        return fake_id

    async def mock_personal_send(phone_number, text, personal_account_id,
                                  conversation_id=None, lead_id=None,
                                  account=None, lad_backend_url=None):
        fake_id = f"personal_mock_{uuid.uuid4().hex[:12]}"
        slug = account.slug if account else "personal"
        print(f"\n  [MOCK PERSONAL WA] -> {phone_number}")
        print(f"  [MOCK PERSONAL WA] account_id: {personal_account_id}")
        print(f"  [MOCK PERSONAL WA] msg_id: {fake_id}")

        # Still save to DB like the real client does
        if conversation_id and lead_id and account:
            from services.personal_whatsapp_client import _save_outgoing_message
            await _save_outgoing_message(
                str(uuid.uuid4()), fake_id, conversation_id, lead_id, text, account
            )
        return fake_id

    _mock_business_send.side_effect = mock_wa_send
    _mock_personal_send.side_effect = mock_personal_send


# ---------------------------------------------------------------------------
# Core test helpers
# ---------------------------------------------------------------------------

async def _resolve_account(slug: str | None, channel: str):
    """Load the WhatsApp account from the registry."""
    from services.account_registry import (
        load_accounts,
        get_account_by_slug,
        get_default_account,
    )
    from modules.bni import register_bni_flow

    register_bni_flow()
    await load_accounts()

    if slug:
        account = get_account_by_slug(slug)
        if not account:
            print(f"  ERROR: No account found for slug '{slug}'")
            sys.exit(1)
    else:
        account = get_default_account()
        if not account:
            print("  ERROR: No default account configured")
            sys.exit(1)

    # Override channel metadata based on --channel flag
    if channel == "personal":
        personal_metadata = {
            **account.metadata,
            "channel": "personal_whatsapp",
            "personal_account_id": account.metadata.get(
                "personal_account_id", f"test_session_{uuid.uuid4().hex[:8]}"
            ),
            "lad_backend_url": "http://localhost:3001",
        }
        account = replace(account, metadata=personal_metadata)
    elif channel == "business":
        # Strip personal WA metadata so the handler routes to business WA
        biz_metadata = {
            k: v for k, v in account.metadata.items()
            if k not in ("channel", "personal_account_id", "lad_backend_url")
        }
        biz_metadata["channel"] = "business_whatsapp"
        account = replace(account, metadata=biz_metadata)

    return account


async def _send_and_wait(phone: str, message: str, contact_name: str, account):
    """Send a message through the full handle_incoming_message pipeline.

    Since handle_incoming_message uses a 1-second debounce buffer, we need
    to wait for the background task to complete.
    """
    from services.message_handler import handle_incoming_message

    external_msg_id = f"wamid.test_{uuid.uuid4().hex[:12]}"

    t0 = time.time()
    await handle_incoming_message(
        phone_number=phone,
        message_text=message,
        contact_name=contact_name,
        external_message_id=external_msg_id,
        chapter=account,
    )

    # Wait for debounce (1s) + LLM processing + send
    # The debounce creates an asyncio task — we need to let it run
    print(f"  [PIPELINE] handle_incoming_message returned in {time.time()-t0:.3f}s")
    print(f"  [PIPELINE] Waiting for debounce + LLM + send...")

    # Give the background task time to complete
    for _ in range(120):  # up to 60 seconds
        await asyncio.sleep(0.5)
        # Check if mock was called (meaning reply was sent)
        channel = account.metadata.get("channel", "business_whatsapp")
        if channel == "personal_whatsapp":
            if _mock_personal_send.call_count > 0:
                break
        else:
            if _mock_business_send.call_count > 0:
                break
    else:
        print("  [TIMEOUT] No reply generated within 60s")
        return None

    total = time.time() - t0
    channel = account.metadata.get("channel", "business_whatsapp")
    if channel == "personal_whatsapp":
        call_args = _mock_personal_send.call_args
    else:
        call_args = _mock_business_send.call_args

    # Extract the reply text from mock call args
    if call_args:
        # send_message(phone_number, text, ...)
        reply_text = call_args.kwargs.get("text") or (call_args.args[1] if len(call_args.args) > 1 else None)
    else:
        reply_text = None

    # Reset mock counters for next message
    _mock_business_send.reset_mock()
    _mock_personal_send.reset_mock()
    _setup_mocks()  # Re-attach side effects

    return reply_text, total


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

async def interactive_mode(phone: str, slug: str | None, channel: str):
    """Interactive terminal chat — full pipeline with mocked WA sends."""
    from db.connection import init_pools, close_pools

    await init_pools()
    account = await _resolve_account(slug, channel)

    channel_label = "Personal WA (Baileys)" if channel == "personal" else "Business WA (Cloud API)"
    model = account.ai_model or "gemini-2.5-flash"
    flow = account.conversation_flow_template or "generic"

    print(f"\n{'='*60}")
    print(f"  Conversation Service — Interactive Chat")
    print(f"  Account: {account.slug} ({account.display_name})")
    print(f"  Channel: {channel_label}")
    print(f"  Model:   {model}")
    print(f"  Flow:    {flow}")
    print(f"  Phone:   {phone}")
    print(f"  Type messages and press Enter. Type 'quit' to exit.")
    print(f"{'='*60}\n")

    contact_name = "Test User"

    while True:
        try:
            message = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not message or message.lower() in ("quit", "exit", "q"):
            print("Bye!")
            break

        result = await _send_and_wait(phone, message, contact_name, account)
        if result:
            reply_text, total_time = result
            print(f"\n{'─'*60}")
            if reply_text:
                print(f"  Agent ({total_time:.1f}s): {reply_text}")
            else:
                print(f"  NO REPLY extracted (check logs above)")
            print(f"{'─'*60}\n")
        else:
            print(f"\n  [ERROR] No reply received\n")

    await close_pools()


# ---------------------------------------------------------------------------
# Single message mode
# ---------------------------------------------------------------------------

async def single_message(phone: str, message: str, slug: str | None, channel: str):
    """Send a single test message and exit."""
    from db.connection import init_pools, close_pools

    await init_pools()
    account = await _resolve_account(slug, channel)

    channel_label = "personal" if channel == "personal" else "business"
    print(f"\n{'='*60}")
    print(f"  Account: {account.slug}")
    print(f"  Channel: {channel_label}")
    print(f"  Phone:   {phone}")
    print(f"  Message: {message}")
    print(f"{'='*60}")

    result = await _send_and_wait(phone, message, "Test User", account)
    if result:
        reply_text, total_time = result
        print(f"\n{'─'*60}")
        if reply_text:
            print(f"  Agent ({total_time:.1f}s): {reply_text}")
        else:
            print(f"  NO REPLY extracted")
        print(f"{'─'*60}")
        print(f"\n  TOTAL end-to-end: {total_time:.3f}s")

    await close_pools()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Test Conversation Service locally (both Business & Personal WA)"
    )
    parser.add_argument("message", nargs="?", default=None, help="Message to send")
    parser.add_argument("--phone", default="919876543210", help="Phone number (default: 919876543210)")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive chat mode")
    parser.add_argument("--slug", default=None, help="Account slug (default: first active account)")
    parser.add_argument(
        "--channel",
        choices=["business", "personal"],
        default="business",
        help="Channel to test: business (Cloud API) or personal (Baileys)",
    )

    args = parser.parse_args()

    # Apply mocks BEFORE importing the message handler
    _setup_mocks()

    with patch("services.whatsapp_client.send_message", _mock_business_send), \
         patch("services.personal_whatsapp_client.send_message", _mock_personal_send), \
         patch("services.whatsapp_client.mark_as_read", AsyncMock()):

        if args.interactive:
            asyncio.run(interactive_mode(args.phone, args.slug, args.channel))
        elif args.message:
            asyncio.run(single_message(args.phone, args.message, args.slug, args.channel))
        else:
            asyncio.run(single_message(
                args.phone,
                "Hi, I'm a new member. My name is Test User.",
                args.slug,
                args.channel,
            ))


if __name__ == "__main__":
    main()
