"""
LinkedIn Module — AI flow template for LinkedIn DM conversations.

Provides:
- Professional-tone LinkedIn DM conversation flow
- Lead qualification focused on understanding needs and booking meetings
- Registers the "linkedin" FlowTemplate on import.
"""
from __future__ import annotations

import logging
from services.flow_registry import FlowTemplate, register_flow

logger = logging.getLogger(__name__)

# LinkedIn-specific status → prompt name mapping
LINKEDIN_STATUS_TO_PROMPT = {
    "greeting": "LINKEDIN_GREETING",
    "active": "LINKEDIN_ACTIVE",
    "qualifying": "LINKEDIN_QUALIFYING",
    "booking": "LINKEDIN_BOOKING",
    "idle": "LINKEDIN_IDLE",
}

# Profile fields to extract from AI responses for LinkedIn leads
LINKEDIN_PROFILE_FIELDS = [
    "company_name",
    "job_title",
    "industry",
    "pain_point",
    "budget_range",
    "timeline",
]

# Default system prompt used when no DB prompt is configured for the tenant.
# The conversation_engine will use this if "LINKEDIN_ACTIVE" prompt not found in DB.
LINKEDIN_DEFAULT_SYSTEM_PROMPT = """You are an AI assistant managing LinkedIn DMs on behalf of {business_name}.

Your role:
- Respond professionally and concisely — LinkedIn is a professional network
- Keep replies under 3 sentences unless the prospect asks detailed questions
- Goal: qualify the lead, understand their core need, and offer to book a brief call
- Never use emojis unless the prospect uses them first
- Always respond in the same language as the incoming message
- If the prospect asks to speak to a human, politely acknowledge and say a team member will follow up

Tone: Professional, warm, and direct. No corporate jargon.

When qualifying:
1. First response: friendly acknowledgment + one open question about their business/need
2. Second response: summarize their need + propose a 15-minute discovery call
3. Third+ response: answer questions directly, guide toward booking

If {context} is available, use it to personalize the response.
"""


def register_linkedin_flow():
    """Register the LinkedIn flow template. Called from main.py on startup."""
    linkedin_flow = FlowTemplate(
        name="linkedin",
        status_to_prompt=LINKEDIN_STATUS_TO_PROMPT,
        initial_status="greeting",
        profile_fields=LINKEDIN_PROFILE_FIELDS,
        state_transition_handler=None,  # No complex state machine — prompt-driven
        create_state_handler=None,
    )
    register_flow(linkedin_flow)
    logger.info("LinkedIn flow template registered")


__all__ = ["register_linkedin_flow", "LINKEDIN_DEFAULT_SYSTEM_PROMPT"]
