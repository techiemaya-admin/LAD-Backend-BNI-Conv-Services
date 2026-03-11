from __future__ import annotations
"""
Conversation engine — generic LLM pipeline with pluggable flow templates.

Loads conversation state -> selects prompt via flow template -> calls LLM
-> parses response -> updates state -> runs flow-specific side effects.

Supports any industry client through the flow template system.
BNI-specific logic lives in modules/bni/ and is invoked via flow hooks.
"""
import json
import logging
import os
import time as _time
import uuid
from datetime import datetime

import asyncio

import google.generativeai as genai
from openai import AsyncOpenAI

from db.connection import AsyncDBConnection, CoreDBConnection
from services.account_registry import WhatsAppAccount
from services.flow_registry import get_flow
from services.prompt_loader import get_prompt

logger = logging.getLogger(__name__)

# Fallback models (used when account doesn't specify)
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

# Expose for test script
MODEL_NAME = DEFAULT_GEMINI_MODEL

_gemini_configured = False
_openai_client: AsyncOpenAI | None = None


def _ensure_gemini_configured(api_key: str | None = None):
    """Lazily configure Gemini API key."""
    global _gemini_configured
    if not _gemini_configured:
        key = api_key or os.getenv("GOOGLE_API_KEY", "")
        if key:
            genai.configure(api_key=key)
            _gemini_configured = True
            logger.info("Gemini API configured successfully")
        else:
            logger.warning("GOOGLE_API_KEY not set — Gemini unavailable")


def _get_openai_client() -> AsyncOpenAI:
    """Get or create the shared async OpenAI client."""
    global _openai_client
    if _openai_client is None:
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            logger.error("OPENAI_API_KEY env var is empty or not set")
        _openai_client = AsyncOpenAI(api_key=api_key, max_retries=0, timeout=30.0)
        logger.info("OpenAI client initialized")
    return _openai_client


# Per-member conversation history (in-memory, last 5 pairs)
_chat_histories: dict[str, list] = {}
MAX_HISTORY_PAIRS = 5


async def process_conversation(
    phone_number: str,
    lead_id: str,
    conversation_id: str,
    message_text: str,
    contact_name: str,
    account: WhatsAppAccount | None = None,
) -> str | None:
    """Main conversation processing pipeline. Returns the AI reply text.

    Works with any flow template — BNI, generic, or custom.
    """
    t_pipeline = _time.time()

    # Resolve account and flow
    if account is None:
        from services.account_registry import get_default_account
        account = get_default_account()
        if account is None:
            logger.error("No account available for conversation processing")
            return "I'm sorry, I'm having trouble processing your message. Please try again."

    flow = get_flow(account.conversation_flow_template)
    tenant_id = account.tenant_id
    slug = account.slug

    # 1. Load or create conversation state
    t0 = _time.time()
    state = await _load_conversation_state(phone_number, tenant_id)

    if state is None:
        state = await _create_conversation_state(
            phone_number, lead_id, contact_name, account, flow
        )
    logger.info(f"[{slug}][TIMING] load_conversation_state: {_time.time()-t0:.3f}s")

    context_status = state.get("context_status", flow.initial_status)

    # 2. Build context for the prompt
    profile_data = state.get("profile_data", {})
    if isinstance(profile_data, str):
        try:
            profile_data = json.loads(profile_data)
        except Exception:
            profile_data = {}

    metadata = state.get("metadata", {})
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}

    # Build context_json — a single JSON blob with all state data
    context_info = {
        "name": state.get("contact_name", contact_name),
        "phone": phone_number,
        "context_status": context_status,
    }
    # Merge profile_data fields into context
    context_info.update(profile_data)
    # Include metadata fields that prompts might need
    for key in ["match_json", "meeting_json", "stats_json", "icp_step", "icp_answers", "website_data"]:
        if key in metadata:
            context_info[key] = metadata[key]

    context_json = json.dumps(context_info, indent=2)

    # Load recent messages + prompt IN PARALLEL
    t0 = _time.time()
    prompt_name = flow.get_prompt_name(context_status)
    logger.info(f"[{slug}] Loading prompt '{prompt_name}' for status '{context_status}'")
    conversation_json, prompt_template = await asyncio.gather(
        _get_recent_messages(conversation_id, tenant_id, limit=10),
        get_prompt(prompt_name, account),
    )
    logger.info(f"[{slug}][TIMING] get_messages+load_prompt (parallel): {_time.time()-t0:.3f}s")

    try:
        system_prompt = prompt_template.format(
            conversation_json=conversation_json,
            context_json=context_json,
            # Backward compat: BNI prompts may still use these
            member_json=context_json,
            match_json=json.dumps(metadata.get("match_json", {})),
            meeting_json=json.dumps(metadata.get("meeting_json", {})),
            stats_json=json.dumps(metadata.get("stats_json", {})),
            current_date=datetime.utcnow().strftime("%Y-%m-%d"),
        )
    except Exception as e:
        logger.error(f"Prompt formatting failed: {e}", exc_info=True)
        return "I'm sorry, I'm having trouble processing your message. Please try again."

    # 4. Call LLM — use account's model preference
    t0 = _time.time()
    gemini_model = account.ai_model if "gemini" in (account.ai_model or "").lower() else DEFAULT_GEMINI_MODEL
    openai_model = account.ai_model if "gpt" in (account.ai_model or "").lower() else DEFAULT_OPENAI_MODEL

    llm_response = await _call_gemini(
        system_prompt, message_text, phone_number,
        model=gemini_model, api_key=account.ai_api_key,
    )

    if llm_response:
        logger.info(f"[{slug}][TIMING] gemini_api_call: {_time.time()-t0:.3f}s")
    else:
        logger.warning(f"[{slug}] Gemini failed, falling back to OpenAI")
        t0 = _time.time()
        llm_response = await _call_openai(
            system_prompt, message_text, phone_number, model=openai_model
        )
        logger.info(f"[{slug}][TIMING] openai_fallback_api_call: {_time.time()-t0:.3f}s")

    if not llm_response:
        logger.error(f"[{slug}] LLM failed to generate response for {phone_number}")
        return "I'm sorry, I'm having trouble processing your message. Please try again."

    # 5. Parse response
    agent_reply, info_fields = _parse_llm_response(llm_response)

    if not agent_reply:
        logger.error(
            f"[{slug}] LLM returned empty agent_reply for {phone_number}. "
            f"Context status: {context_status}, Raw response: {llm_response[:200]}"
        )
        # Fall back to acknowledging the message
        return f"Thank you for your message. I'm processing your information."

    # 6. Update conversation state
    t0 = _time.time()
    new_status = info_fields.get("context_status", context_status)
    await _update_conversation_state(phone_number, tenant_id, info_fields, new_status, flow)
    logger.info(f"[{slug}][TIMING] update_conversation_state: {_time.time()-t0:.3f}s")

    # 7. Handle side effects via flow template hook
    t0 = _time.time()
    overridden_status = None
    if flow.state_transition_handler:
        overridden_status = await flow.state_transition_handler(
            account, phone_number, lead_id, conversation_id,
            context_status, new_status, info_fields,
        )
    logger.info(f"[{slug}][TIMING] handle_state_transition: {_time.time()-t0:.3f}s")

    # 8. If state was overridden, chain another LLM call
    if overridden_status and overridden_status != new_status:
        logger.info(f"[{slug}] State overridden: {new_status} -> {overridden_status}, chaining LLM call")
        chained_reply = await _chain_next_phase(
            phone_number, lead_id, conversation_id, overridden_status, agent_reply, account
        )
        if chained_reply:
            agent_reply = f"{agent_reply}\n\n{chained_reply}"

    logger.info(f"[{slug}][TIMING] process_conversation TOTAL: {_time.time()-t_pipeline:.3f}s")
    return agent_reply


# ---------------------------------------------------------------------------
# Database helpers (generic — work with conversation_states table)
# ---------------------------------------------------------------------------

async def _load_conversation_state(phone_number: str, tenant_id: str) -> dict | None:
    """Load conversation state from conversation_states table."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            row = await conn.fetchrow(
                "SELECT * FROM conversation_states WHERE phone = $1",
                phone_number,
            )
            if row:
                result = dict(row)
                for field in ("profile_data", "metadata"):
                    if isinstance(result.get(field), str):
                        try:
                            result[field] = json.loads(result[field])
                        except Exception:
                            result[field] = {}
                return result
            return None
    except Exception as e:
        logger.error(f"Error loading conversation state: {e}")
        return None


async def _create_conversation_state(
    phone_number: str, lead_id: str, contact_name: str,
    account: WhatsAppAccount, flow,
) -> dict:
    """Create initial conversation state for a new contact.

    If the flow has a create_state_handler (e.g., BNI looks up existing members),
    it's called first to determine initial status and profile data.
    """
    state_id = str(uuid.uuid4())
    tenant_id = account.tenant_id

    initial_status = flow.initial_status
    profile_data = {}
    resolved_name = contact_name

    # Let the flow customize state creation
    if flow.create_state_handler:
        try:
            custom_state = await flow.create_state_handler(
                account, phone_number, lead_id, contact_name
            )
            if custom_state:
                resolved_name = custom_state.get("contact_name", contact_name)
                initial_status = custom_state.get("context_status", initial_status)
                profile_data = custom_state.get("profile_data", {})
        except Exception as e:
            logger.warning(f"Flow create_state_handler failed: {e}")

    try:
        async with AsyncDBConnection(tenant_id) as conn:
            await conn.execute(
                """
                INSERT INTO conversation_states
                    (id, lead_id, phone, contact_name, context_status,
                     profile_data, metadata, tenant_id, created_at, updated_at)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb, '{}', $7::uuid, NOW(), NOW())
                ON CONFLICT (phone) DO NOTHING
                """,
                state_id,
                lead_id,
                phone_number,
                resolved_name,
                initial_status,
                json.dumps(profile_data),
                tenant_id,
            )
    except Exception as e:
        logger.error(f"Error creating conversation state: {e}")

    return {
        "id": state_id,
        "lead_id": lead_id,
        "phone": phone_number,
        "contact_name": resolved_name,
        "context_status": initial_status,
        "profile_data": profile_data,
        "metadata": {},
    }


async def _update_conversation_state(
    phone_number: str, tenant_id: str, info_fields: dict, new_status: str, flow,
):
    """Update conversation state from LLM response fields."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            # Update profile_data with any fields the flow defines
            profile_updates = {}
            for field_name in flow.profile_fields:
                val = info_fields.get(field_name)
                if val is not None:
                    profile_updates[field_name] = val

            # Update context_status + merge profile_data
            if profile_updates:
                await conn.execute(
                    """
                    UPDATE conversation_states SET
                        context_status = $1,
                        profile_data = COALESCE(profile_data, '{}')::jsonb || $2::jsonb,
                        updated_at = NOW()
                    WHERE phone = $3
                    """,
                    new_status,
                    json.dumps(profile_updates),
                    phone_number,
                )
            else:
                await conn.execute(
                    """
                    UPDATE conversation_states SET
                        context_status = $1,
                        updated_at = NOW()
                    WHERE phone = $2
                    """,
                    new_status,
                    phone_number,
                )

            # Update metadata for special fields
            metadata_updates = {}
            for key in ("icp_step", "icp_answers"):
                val = info_fields.get(key)
                if val is not None:
                    metadata_updates[key] = val

            if metadata_updates:
                await conn.execute(
                    """
                    UPDATE conversation_states
                    SET metadata = COALESCE(metadata, '{}')::jsonb || $1::jsonb
                    WHERE phone = $2
                    """,
                    json.dumps(metadata_updates),
                    phone_number,
                )

    except Exception as e:
        logger.error(f"Error updating conversation state: {e}")


async def _chain_next_phase(
    phone_number: str,
    lead_id: str,
    conversation_id: str,
    new_status: str,
    previous_reply: str,
    account: WhatsAppAccount,
) -> str | None:
    """Run a follow-up LLM call for an overridden state."""
    flow = get_flow(account.conversation_flow_template)
    tenant_id = account.tenant_id

    state = await _load_conversation_state(phone_number, tenant_id)
    if not state:
        return None

    profile_data = state.get("profile_data", {})
    if isinstance(profile_data, str):
        try:
            profile_data = json.loads(profile_data)
        except Exception:
            profile_data = {}

    metadata = state.get("metadata", {})
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}

    context_info = {
        "name": state.get("contact_name", ""),
        "phone": phone_number,
        "context_status": new_status,
    }
    context_info.update(profile_data)
    for key in ["match_json", "meeting_json", "stats_json"]:
        if key in metadata:
            context_info[key] = metadata[key]

    context_json = json.dumps(context_info, indent=2)

    prompt_name = flow.get_prompt_name(new_status)
    logger.info(f"[CHAIN] Loading prompt '{prompt_name}' for overridden status '{new_status}'")

    conversation_json, prompt_template = await asyncio.gather(
        _get_recent_messages(conversation_id, tenant_id, limit=10),
        get_prompt(prompt_name, account),
    )

    try:
        system_prompt = prompt_template.format(
            conversation_json=conversation_json,
            context_json=context_json,
            member_json=context_json,
            match_json=json.dumps(metadata.get("match_json", {})),
            meeting_json=json.dumps(metadata.get("meeting_json", {})),
            stats_json=json.dumps(metadata.get("stats_json", {})),
            current_date=datetime.utcnow().strftime("%Y-%m-%d"),
        )
    except Exception as e:
        logger.error(f"[CHAIN] Prompt formatting failed: {e}", exc_info=True)
        return None

    synthetic_msg = f"[System: The contact just completed their profile. Your previous reply was: \"{previous_reply}\". Now present the next phase naturally.]"

    gemini_model = account.ai_model if "gemini" in (account.ai_model or "").lower() else DEFAULT_GEMINI_MODEL
    llm_response = await _call_gemini(
        system_prompt, synthetic_msg, phone_number,
        model=gemini_model, api_key=account.ai_api_key,
    )
    if not llm_response:
        llm_response = await _call_openai(system_prompt, synthetic_msg, phone_number)

    if not llm_response:
        return None

    agent_reply, info_fields = _parse_llm_response(llm_response)

    chained_status = info_fields.get("context_status", new_status)
    await _update_conversation_state(phone_number, tenant_id, info_fields, chained_status, flow)

    return agent_reply


async def _get_recent_messages(conversation_id: str, tenant_id: str, limit: int = 10) -> str:
    """Load recent messages for conversation context."""
    try:
        async with AsyncDBConnection(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT role, content, created_at FROM messages
                WHERE conversation_id = $1::uuid
                ORDER BY created_at DESC LIMIT $2
                """,
                conversation_id,
                limit,
            )
            messages = []
            for row in reversed(rows):
                role = "Lead" if row["role"] in ("lead", "user") else "Assistant"
                messages.append(f"{role}: {row['content']}")
            return "\n".join(messages) if messages else "No previous messages."
    except Exception as e:
        logger.error(f"Error loading messages: {e}")
        return "No previous messages."


# ---------------------------------------------------------------------------
# LLM callers
# ---------------------------------------------------------------------------

def _call_gemini_sync(
    system_prompt: str, user_message: str, phone_number: str,
    model: str = DEFAULT_GEMINI_MODEL,
) -> str:
    """Synchronous Gemini call (runs in thread pool)."""
    _ensure_gemini_configured()

    gmodel = genai.GenerativeModel(
        model,
        generation_config={
            "temperature": 0.3,
            "response_mime_type": "application/json",
        },
    )

    history = _chat_histories.get(phone_number, [])
    gemini_history = []
    for msg in history:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user":
            gemini_history.append({"role": "user", "parts": [content]})
        elif role in ("assistant", "model"):
            gemini_history.append({"role": "model", "parts": [content]})

    chat_history = [
        {"role": "user", "parts": [f"[System instruction]\n{system_prompt}"]},
        {"role": "model", "parts": ["Understood. I'll follow these instructions."]},
    ]
    chat_history.extend(gemini_history)

    chat = gmodel.start_chat(history=chat_history)
    response = chat.send_message(user_message)
    return response.text


async def _call_gemini(
    system_prompt: str, user_message: str, phone_number: str,
    model: str = DEFAULT_GEMINI_MODEL,
    api_key: str | None = None,
) -> str | None:
    """Call Gemini LLM (primary). Returns None on failure so caller can fallback."""
    key = api_key or os.getenv("GOOGLE_API_KEY", "")
    if not key:
        logger.warning("GOOGLE_API_KEY not set — skipping Gemini")
        return None

    try:
        if api_key:
            genai.configure(api_key=api_key)

        logger.info(f"Calling Gemini for {phone_number}, model={model}")

        result = await asyncio.to_thread(
            _call_gemini_sync, system_prompt, user_message, phone_number, model
        )

        logger.info(f"Gemini response received for {phone_number} ({len(result)} chars)")
        _update_history(phone_number, user_message, result)
        return result

    except Exception as e:
        logger.error(f"Gemini API error for {phone_number}: {type(e).__name__}: {e}", exc_info=True)
        return None


async def _call_openai(
    system_prompt: str, user_message: str, phone_number: str,
    model: str = DEFAULT_OPENAI_MODEL,
) -> str | None:
    """Call OpenAI LLM (fallback). Returns None on failure."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        logger.error("OPENAI_API_KEY not set — fallback unavailable")
        return None

    try:
        logger.info(f"Calling OpenAI fallback for {phone_number}, model={model}")
        client = _get_openai_client()

        messages = [{"role": "system", "content": system_prompt}]

        history = _chat_histories.get(phone_number, [])
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.3,
            response_format={"type": "json_object"},
        )

        result = response.choices[0].message.content
        logger.info(f"OpenAI response received for {phone_number} ({len(result)} chars)")
        _update_history(phone_number, user_message, result)
        return result

    except Exception as e:
        logger.error(f"OpenAI API error for {phone_number}: {type(e).__name__}: {e}", exc_info=True)
        return None


def _update_history(phone_number: str, user_message: str, assistant_reply: str):
    """Update in-memory chat history."""
    if phone_number not in _chat_histories:
        _chat_histories[phone_number] = []
    _chat_histories[phone_number].append({"role": "user", "content": user_message})
    _chat_histories[phone_number].append({"role": "assistant", "content": assistant_reply})
    if len(_chat_histories[phone_number]) > MAX_HISTORY_PAIRS * 2:
        _chat_histories[phone_number] = _chat_histories[phone_number][-(MAX_HISTORY_PAIRS * 2):]


def _parse_llm_response(response_text: str) -> tuple[str, dict]:
    """Parse LLM JSON response into (agent_reply, info_gathering_fields)."""
    if not response_text or not response_text.strip():
        logger.warning("LLM returned empty response")
        return "", {}
    
    try:
        data = json.loads(response_text)
        agent_reply = data.get("agent_reply", "").strip() if isinstance(data.get("agent_reply"), str) else data.get("agent_reply", "")
        info_fields = data.get("info_gathering_fields", {})
        
        # Log parsing result for debugging
        if not agent_reply:
            logger.warning(f"LLM returned JSON but agent_reply was empty. Full response: {response_text[:500]}")
        
        return agent_reply, info_fields
    except json.JSONDecodeError as e:
        logger.warning(f"LLM response was not valid JSON (error: {e}), using as plain text. Response: {response_text[:500]}")
        return response_text.strip(), {"context_status": "idle"}
