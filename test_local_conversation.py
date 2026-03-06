"""
Local test script for BNI Conversation Service.

Tests the full pipeline: DB lookups → prompt loading → Gemini LLM → response.
Skips WhatsApp send (not needed for local testing).

Usage:
    python3 test_local_conversation.py                   # single default message
    python3 test_local_conversation.py "Hello there"     # single custom message
    python3 test_local_conversation.py --phone 919999999999 "Hello"
    python3 test_local_conversation.py -i                # interactive chat mode
"""
import argparse
import asyncio
import time
import uuid
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def _setup_lead_and_conversation(phone: str, contact_name: str = "Test User"):
    """One-time setup: get/create lead + conversation. Returns (lead_id, conv_id)."""
    from services.message_handler import _get_or_create_lead, _get_or_create_conversation

    t0 = time.time()
    lead = await _get_or_create_lead(phone, contact_name)
    lead_id = lead["id"]
    print(f"  [SETUP] get_or_create_lead:         {time.time()-t0:.3f}s  (lead_id={lead_id[:8]}...)")

    t0 = time.time()
    conversation = await _get_or_create_conversation(lead_id)
    conv_id = conversation["id"]
    print(f"  [SETUP] get_or_create_conversation: {time.time()-t0:.3f}s  (conv_id={conv_id[:8]}...)")

    return lead_id, conv_id


async def _send_message(phone: str, lead_id: str, conv_id: str, message: str, contact_name: str = "Test User"):
    """Send a single message through the LLM pipeline and print the reply."""
    from services.message_handler import _save_incoming_message
    from services.conversation_engine import process_conversation, MODEL_NAME

    external_msg_id = f"wamid.test_{uuid.uuid4().hex[:12]}"

    # Save incoming message to DB
    t0 = time.time()
    await _save_incoming_message(conv_id, lead_id, message, external_msg_id)
    print(f"  [DB]    save_incoming_message:       {time.time()-t0:.3f}s")

    # Process through LLM pipeline
    print(f"  [LLM]   Calling Gemini (model: {MODEL_NAME})...")
    t0 = time.time()
    reply = await process_conversation(
        phone_number=phone,
        lead_id=lead_id,
        conversation_id=conv_id,
        message_text=message,
        contact_name=contact_name,
    )
    llm_time = time.time() - t0
    print(f"  [LLM]   process_conversation:       {llm_time:.3f}s")

    print(f"\n{'─'*60}")
    if reply:
        print(f"  Agent ({llm_time:.1f}s): {reply}")
    else:
        print(f"  NO REPLY (LLM returned None - check API key / model)")
    print(f"{'─'*60}")

    return reply


async def interactive_mode(phone: str):
    """Interactive terminal chat — keeps DB pools and lead/conversation alive."""
    from db.connection import init_pools, close_pools
    from services.conversation_engine import MODEL_NAME

    print(f"\n{'='*60}")
    print(f"  BNI Conversation Service — Interactive Chat")
    print(f"  Phone:  {phone}")
    print(f"  Model:  {MODEL_NAME}")
    print(f"  Type messages and press Enter. Type 'quit' to exit.")
    print(f"{'='*60}")

    await init_pools()

    # One-time setup
    print(f"\n  Setting up lead & conversation...")
    lead_id, conv_id = await _setup_lead_and_conversation(phone)
    print(f"  Ready!\n")

    while True:
        try:
            message = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not message or message.lower() in ("quit", "exit", "q"):
            print("Bye!")
            break

        await _send_message(phone, lead_id, conv_id, message)
        print()  # blank line between turns

    await close_pools()


async def single_message(phone: str, message: str):
    """Send a single test message and exit."""
    from db.connection import init_pools, close_pools

    print(f"\n{'='*60}")
    print(f"  Phone:   {phone}")
    print(f"  Message: {message}")
    print(f"{'='*60}")

    await init_pools()

    t_total = time.time()
    lead_id, conv_id = await _setup_lead_and_conversation(phone)
    await _send_message(phone, lead_id, conv_id, message)

    total = time.time() - t_total
    print(f"\n  TOTAL end-to-end: {total:.3f}s")

    await close_pools()


def main():
    parser = argparse.ArgumentParser(description="Test BNI Conversation Service locally")
    parser.add_argument("message", nargs="?", default=None, help="Message to send")
    parser.add_argument("--phone", default="919876543210", help="Phone number (default: 919876543210)")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive chat mode")

    args = parser.parse_args()

    if args.interactive:
        asyncio.run(interactive_mode(args.phone))
    elif args.message:
        asyncio.run(single_message(args.phone, args.message))
    else:
        asyncio.run(single_message(args.phone, "Hi, I'm a new member. My name is Test User."))


if __name__ == "__main__":
    main()
