"""
Conversation engine — LLM pipeline and state machine.

Loads conversation state → selects prompt → calls Gemini (primary) or OpenAI (fallback)
→ parses response → upserts state.
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

from db.connection import ClientDBConnection, CoreDBConnection
from db.schema import core_table, get_tenant_id
from services.prompt_loader import get_prompt, get_prompt_name_for_status

logger = logging.getLogger(__name__)

# Primary model (Gemini)
GEMINI_MODEL = "gemini-2.5-flash"
# Fallback model (OpenAI)
OPENAI_MODEL = "gpt-4o-mini"

# Expose for test script
MODEL_NAME = GEMINI_MODEL

_gemini_configured = False
_openai_client: AsyncOpenAI | None = None


def _ensure_gemini_configured():
    """Lazily configure Gemini API key."""
    global _gemini_configured
    if not _gemini_configured:
        api_key = os.getenv("GOOGLE_API_KEY", "")
        if api_key:
            genai.configure(api_key=api_key)
            _gemini_configured = True
            logger.info("Gemini API configured successfully")
        else:
            logger.warning("GOOGLE_API_KEY not set — Gemini unavailable, will use OpenAI fallback")


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
) -> str | None:
    """Main conversation processing pipeline. Returns the AI reply text."""

    t_pipeline = _time.time()

    # 1. Load or create conversation state
    t0 = _time.time()
    state = await _load_conversation_state(phone_number)

    if state is None:
        state = await _create_conversation_state(phone_number, lead_id, contact_name)
    logger.info(f"[TIMING] load_conversation_state: {_time.time()-t0:.3f}s")

    context_status = state.get("context_status", "onboarding_greeting")

    # 2. Build context for the prompt
    metadata = state.get("metadata", {})
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}

    member_info = {
        "name": state.get("member_name", contact_name),
        "company_name": state.get("company_name"),
        "industry": state.get("industry"),
        "designation": state.get("designation"),
        "services_offered": state.get("services_offered"),
        "ideal_customer_profile": state.get("ideal_customer_profile"),
        "phone": phone_number,
    }

    if context_status == "icp_discovery":
        member_info["icp_step"] = metadata.get("icp_step", 1)
        member_info["icp_answers"] = metadata.get("icp_answers", {})
        website_data = metadata.get("website_data")
        if website_data:
            member_info["website_data"] = website_data

    member_json = json.dumps(member_info, indent=2)

    # Load recent messages + prompt IN PARALLEL
    t0 = _time.time()
    prompt_name = get_prompt_name_for_status(context_status)
    logger.info(f"Loading prompt '{prompt_name}' for status '{context_status}'")
    conversation_json, prompt_template = await asyncio.gather(
        _get_recent_messages(conversation_id, limit=10),
        get_prompt(prompt_name),
    )
    logger.info(f"[TIMING] get_messages+load_prompt (parallel): {_time.time()-t0:.3f}s")

    try:
        system_prompt = prompt_template.format(
            conversation_json=conversation_json,
            member_json=member_json,
            match_json=metadata.get("match_json", "{}"),
            meeting_json=metadata.get("meeting_json", "{}"),
            stats_json=metadata.get("stats_json", "{}"),
            current_date=datetime.utcnow().strftime("%Y-%m-%d"),
        )
    except Exception as e:
        logger.error(f"Prompt formatting failed: {e}", exc_info=True)
        return "I'm sorry, I'm having trouble processing your message. Please try again."

    # 4. Call LLM — try Gemini first, fall back to OpenAI
    t0 = _time.time()
    llm_response = await _call_gemini(system_prompt, message_text, phone_number)

    if llm_response:
        logger.info(f"[TIMING] gemini_api_call: {_time.time()-t0:.3f}s")
    else:
        logger.warning("Gemini failed, falling back to OpenAI")
        t0 = _time.time()
        llm_response = await _call_openai(system_prompt, message_text, phone_number)
        logger.info(f"[TIMING] openai_fallback_api_call: {_time.time()-t0:.3f}s")

    if not llm_response:
        return "I'm sorry, I'm having trouble processing your message. Please try again."

    # 5. Parse response
    agent_reply, info_fields = _parse_llm_response(llm_response)

    if not agent_reply:
        return "I'm sorry, I couldn't understand that. Could you rephrase?"

    # 6. Update conversation state
    t0 = _time.time()
    new_status = info_fields.get("context_status", context_status)
    await _update_conversation_state(phone_number, lead_id, info_fields, new_status)
    logger.info(f"[TIMING] update_conversation_state: {_time.time()-t0:.3f}s")

    # 7. Handle side effects based on state transitions
    t0 = _time.time()
    overridden_status = await _handle_state_transition(
        phone_number, lead_id, conversation_id, context_status, new_status, info_fields
    )
    logger.info(f"[TIMING] handle_state_transition: {_time.time()-t0:.3f}s")

    # 8. If state was overridden (e.g. onboarding_complete → match_suggested),
    #    chain another LLM call so the user gets the next phase immediately.
    if overridden_status and overridden_status != new_status:
        logger.info(f"State overridden: {new_status} → {overridden_status}, chaining LLM call")
        chained_reply = await _chain_next_phase(
            phone_number, lead_id, conversation_id, overridden_status, agent_reply
        )
        if chained_reply:
            agent_reply = f"{agent_reply}\n\n{chained_reply}"

    logger.info(f"[TIMING] process_conversation TOTAL: {_time.time()-t_pipeline:.3f}s")
    return agent_reply


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

async def _load_conversation_state(phone_number: str) -> dict | None:
    """Load conversation state from bni_conversation_manager."""
    try:
        async with ClientDBConnection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM bni_conversation_manager WHERE member_phone = $1",
                phone_number,
            )
            if row:
                result = dict(row)
                if isinstance(result.get("metadata"), str):
                    try:
                        result["metadata"] = json.loads(result["metadata"])
                    except Exception:
                        result["metadata"] = {}
                return result
            return None
    except Exception as e:
        logger.error(f"Error loading conversation state: {e}")
        return None


async def _create_conversation_state(
    phone_number: str, lead_id: str, contact_name: str
) -> dict:
    """Create initial conversation state for a new member.

    Looks up existing member data in community_roi_members first.
    If profile fields (company, industry) already exist, skip straight
    to icp_discovery instead of onboarding_greeting.
    """
    state_id = str(uuid.uuid4())

    # Check if member already exists in community_roi_members
    member_name = contact_name
    company_name = None
    industry = None
    designation = None
    services_offered = None
    initial_status = "onboarding_greeting"

    try:
        async with CoreDBConnection() as conn:
            existing = await conn.fetchrow(
                f"""
                SELECT name, company_name, industry, designation, metadata
                FROM {core_table('community_roi_members')}
                WHERE REPLACE(REPLACE(REPLACE(phone, ' ', ''), '+', ''), '-', '') = $1
                  AND tenant_id = $2::uuid AND is_deleted = false
                """,
                phone_number,
                get_tenant_id(),
            )
            if existing:
                # Use member's real name (strip chapter suffix if present)
                raw_name = existing["name"] or contact_name
                if "(" in raw_name:
                    raw_name = raw_name[:raw_name.index("(")].strip()
                member_name = raw_name
                company_name = existing["company_name"]
                industry = existing["industry"]
                designation = existing["designation"]

                ex_meta = existing["metadata"]
                if isinstance(ex_meta, str):
                    try:
                        ex_meta = json.loads(ex_meta)
                    except Exception:
                        ex_meta = {}
                services_offered = ex_meta.get("services_offered")

                # If core profile fields exist, skip to ICP discovery
                if company_name and industry:
                    initial_status = "icp_discovery"
                    logger.info(
                        f"Existing member found for {phone_number}: {member_name} "
                        f"({company_name}). Skipping to icp_discovery."
                    )
                else:
                    # Some fields missing — start from onboarding_profile
                    initial_status = "onboarding_profile"
                    logger.info(
                        f"Existing member found for {phone_number}: {member_name} "
                        f"but profile incomplete. Starting onboarding_profile."
                    )
    except Exception as e:
        logger.warning(f"Could not look up existing member for {phone_number}: {e}")

    try:
        async with ClientDBConnection() as conn:
            await conn.execute(
                """
                INSERT INTO bni_conversation_manager
                    (id, lead_id, member_phone, member_name, context_status,
                     company_name, industry, designation, services_offered,
                     metadata, created_at, updated_at)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, '{}', NOW(), NOW())
                ON CONFLICT (member_phone) DO NOTHING
                """,
                state_id,
                lead_id,
                phone_number,
                member_name,
                initial_status,
                company_name,
                industry,
                designation,
                services_offered,
            )
    except Exception as e:
        logger.error(f"Error creating conversation state: {e}")

    return {
        "id": state_id,
        "lead_id": lead_id,
        "member_phone": phone_number,
        "member_name": member_name,
        "context_status": initial_status,
        "company_name": company_name,
        "industry": industry,
        "designation": designation,
        "services_offered": services_offered,
        "metadata": {},
    }


async def _update_conversation_state(
    phone_number: str, lead_id: str, info_fields: dict, new_status: str
):
    """Update conversation state from LLM response fields."""
    try:
        async with ClientDBConnection() as conn:
            await conn.execute(
                """
                UPDATE bni_conversation_manager SET
                    context_status = $1,
                    company_name = COALESCE($2, company_name),
                    industry = COALESCE($3, industry),
                    designation = COALESCE($4, designation),
                    services_offered = COALESCE($5, services_offered),
                    ideal_customer_profile = COALESCE($6, ideal_customer_profile),
                    updated_at = NOW()
                WHERE member_phone = $7
                """,
                new_status,
                info_fields.get("company_name"),
                info_fields.get("industry"),
                info_fields.get("designation"),
                info_fields.get("services_offered"),
                info_fields.get("ideal_customer_profile"),
                phone_number,
            )

            icp_step = info_fields.get("icp_step")
            icp_answers = info_fields.get("icp_answers")
            if icp_step is not None or icp_answers is not None:
                metadata_updates = {}
                if icp_step is not None:
                    metadata_updates["icp_step"] = icp_step
                if icp_answers is not None:
                    metadata_updates["icp_answers"] = icp_answers

                await conn.execute(
                    """
                    UPDATE bni_conversation_manager
                    SET metadata = COALESCE(metadata, '{}')::jsonb || $1::jsonb
                    WHERE member_phone = $2
                    """,
                    json.dumps(metadata_updates),
                    phone_number,
                )

    except Exception as e:
        logger.error(f"Error updating conversation state: {e}")


async def _handle_state_transition(
    phone_number: str,
    lead_id: str,
    conversation_id: str,
    old_status: str,
    new_status: str,
    info_fields: dict,
) -> str | None:
    """Handle side effects when conversation state changes.

    Returns the overridden status if the state was changed programmatically
    (e.g. onboarding_complete → match_suggested), or None if no override.
    """
    overridden_status = None

    if new_status == "onboarding_complete" and old_status != "onboarding_complete":
        from services.member_service import enrich_member_profile, find_best_match
        await enrich_member_profile(phone_number, info_fields)
        logger.info(f"Member profile enriched for {phone_number}")

        # Auto-suggest a 1-to-1 match right after onboarding
        match = await find_best_match(phone_number)
        if match:
            match_json_str = json.dumps(match)
            async with ClientDBConnection() as conn:
                await conn.execute(
                    """
                    UPDATE bni_conversation_manager
                    SET context_status = 'match_suggested',
                        metadata = jsonb_set(
                            COALESCE(metadata, '{}')::jsonb,
                            '{match_json}',
                            $1::jsonb
                        ),
                        updated_at = NOW()
                    WHERE member_phone = $2
                    """,
                    match_json_str,
                    phone_number,
                )
            match_type = match.get("type", "unknown")
            members_count = len(match.get("members", []))
            logger.info(f"Match result for {phone_number}: type={match_type}, members={members_count}")
            overridden_status = "match_suggested"
        else:
            logger.info(f"No suitable match found for {phone_number} after onboarding")

    # Scrape website after ICP Q1 answer (icp_step transitions to 2)
    if new_status == "icp_discovery" or (old_status == "icp_discovery" and new_status == "icp_discovery"):
        icp_step = info_fields.get("icp_step")
        icp_answers = info_fields.get("icp_answers", {})
        q1_answer = icp_answers.get("q1", "")

        if icp_step == 2 and q1_answer:
            # Extract URL from Q1 answer
            import re
            urls = re.findall(r'(?:https?://)?(?:www\.)?[\w.-]+\.\w{2,}(?:/\S*)?', q1_answer)
            if urls:
                from services.website_scraper import scrape_website_for_clients
                url = urls[0]
                logger.info(f"Scraping website from ICP Q1: {url}")
                scraped = await scrape_website_for_clients(url)

                if scraped.get("clients") or scraped.get("services"):
                    async with ClientDBConnection() as conn:
                        await conn.execute(
                            """
                            UPDATE bni_conversation_manager
                            SET metadata = jsonb_set(
                                COALESCE(metadata, '{}')::jsonb,
                                '{website_data}',
                                $1::jsonb
                            ),
                            updated_at = NOW()
                            WHERE member_phone = $2
                            """,
                            json.dumps(scraped),
                            phone_number,
                        )
                    logger.info(
                        f"Website scraped for {phone_number}: "
                        f"{len(scraped.get('clients', []))} clients, "
                        f"{len(scraped.get('services', []))} services"
                    )

    if new_status == "coordination_a_availability" and old_status == "match_suggested":
        if info_fields.get("match_accepted"):
            from services.meeting_scheduler import initiate_meeting_from_match
            await initiate_meeting_from_match(phone_number, conversation_id, lead_id)

    if new_status == "kpi_query" and old_status != "kpi_query":
        from services.member_service import get_member_stats_json
        stats = await get_member_stats_json(phone_number)
        async with ClientDBConnection() as conn:
            await conn.execute(
                """
                UPDATE bni_conversation_manager
                SET metadata = jsonb_set(COALESCE(metadata, '{}')::jsonb, '{stats_json}', $1::jsonb)
                WHERE member_phone = $2
                """,
                json.dumps(stats),
                phone_number,
            )

    return overridden_status


async def _chain_next_phase(
    phone_number: str,
    lead_id: str,
    conversation_id: str,
    new_status: str,
    previous_reply: str,
) -> str | None:
    """Run a follow-up LLM call for the overridden state.

    This makes the conversation flow seamlessly — e.g. after ICP completes,
    the match suggestion appears in the same response without waiting for
    another user message.
    """
    # Reload state (now has the overridden status + any new metadata like match_json)
    state = await _load_conversation_state(phone_number)
    if not state:
        return None

    metadata = state.get("metadata", {})
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}

    member_info = {
        "name": state.get("member_name", ""),
        "company_name": state.get("company_name"),
        "industry": state.get("industry"),
        "designation": state.get("designation"),
        "services_offered": state.get("services_offered"),
        "ideal_customer_profile": state.get("ideal_customer_profile"),
        "phone": phone_number,
    }
    member_json = json.dumps(member_info, indent=2)

    prompt_name = get_prompt_name_for_status(new_status)
    logger.info(f"[CHAIN] Loading prompt '{prompt_name}' for overridden status '{new_status}'")

    conversation_json, prompt_template = await asyncio.gather(
        _get_recent_messages(conversation_id, limit=10),
        get_prompt(prompt_name),
    )

    try:
        system_prompt = prompt_template.format(
            conversation_json=conversation_json,
            member_json=member_json,
            match_json=metadata.get("match_json", "{}"),
            meeting_json=metadata.get("meeting_json", "{}"),
            stats_json=metadata.get("stats_json", "{}"),
            current_date=datetime.utcnow().strftime("%Y-%m-%d"),
        )
    except Exception as e:
        logger.error(f"[CHAIN] Prompt formatting failed: {e}", exc_info=True)
        return None

    # Use a synthetic message so the LLM knows the previous reply context
    synthetic_msg = f"[System: The member just completed their profile. Your previous reply was: \"{previous_reply}\". Now present the next phase naturally.]"

    llm_response = await _call_gemini(system_prompt, synthetic_msg, phone_number)
    if not llm_response:
        llm_response = await _call_openai(system_prompt, synthetic_msg, phone_number)

    if not llm_response:
        return None

    agent_reply, info_fields = _parse_llm_response(llm_response)

    # Update state with the chained response fields
    chained_status = info_fields.get("context_status", new_status)
    await _update_conversation_state(phone_number, lead_id, info_fields, chained_status)

    return agent_reply


async def _get_recent_messages(conversation_id: str, limit: int = 10) -> str:
    """Load recent messages for conversation context."""
    try:
        async with ClientDBConnection() as conn:
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
    system_prompt: str, user_message: str, phone_number: str
) -> str:
    """Synchronous Gemini call (runs in thread pool)."""
    _ensure_gemini_configured()

    model = genai.GenerativeModel(
        GEMINI_MODEL,
        generation_config={
            "temperature": 0.3,
            "response_mime_type": "application/json",
        },
    )

    # Build chat history (Gemini format)
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

    chat = model.start_chat(history=chat_history)
    response = chat.send_message(user_message)
    return response.text


async def _call_gemini(
    system_prompt: str, user_message: str, phone_number: str
) -> str | None:
    """Call Gemini LLM (primary). Returns None on failure so caller can fallback."""
    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        logger.warning("GOOGLE_API_KEY not set — skipping Gemini")
        return None

    try:
        logger.info(f"Calling Gemini for {phone_number}, model={GEMINI_MODEL}")

        result = await asyncio.to_thread(
            _call_gemini_sync, system_prompt, user_message, phone_number
        )

        logger.info(f"Gemini response received for {phone_number} ({len(result)} chars)")
        _update_history(phone_number, user_message, result)
        return result

    except Exception as e:
        logger.error(f"Gemini API error for {phone_number}: {type(e).__name__}: {e}", exc_info=True)
        return None


async def _call_openai(
    system_prompt: str, user_message: str, phone_number: str
) -> str | None:
    """Call OpenAI LLM (fallback). Returns None on failure."""
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        logger.error("OPENAI_API_KEY not set — fallback unavailable")
        return None

    try:
        logger.info(f"Calling OpenAI fallback for {phone_number}, model={OPENAI_MODEL}")
        client = _get_openai_client()

        messages = [{"role": "system", "content": system_prompt}]

        # Add chat history (already in OpenAI format)
        history = _chat_histories.get(phone_number, [])
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
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
    """Update in-memory chat history (shared format for both providers)."""
    if phone_number not in _chat_histories:
        _chat_histories[phone_number] = []
    _chat_histories[phone_number].append({"role": "user", "content": user_message})
    _chat_histories[phone_number].append({"role": "assistant", "content": assistant_reply})
    if len(_chat_histories[phone_number]) > MAX_HISTORY_PAIRS * 2:
        _chat_histories[phone_number] = _chat_histories[phone_number][-(MAX_HISTORY_PAIRS * 2):]


def _parse_llm_response(response_text: str) -> tuple[str, dict]:
    """Parse LLM JSON response into (agent_reply, info_gathering_fields)."""
    try:
        data = json.loads(response_text)
        agent_reply = data.get("agent_reply", "")
        info_fields = data.get("info_gathering_fields", {})
        return agent_reply, info_fields
    except json.JSONDecodeError:
        logger.warning("LLM response was not valid JSON, using as plain text")
        return response_text.strip(), {"context_status": "idle"}
