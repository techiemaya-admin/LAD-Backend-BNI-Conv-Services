"""
BNI Flow Template — defines the conversation state machine for BNI chapters.

Registers the "bni" flow template with the flow registry.
"""
from __future__ import annotations

import logging
from services.flow_registry import FlowTemplate, register_flow

logger = logging.getLogger(__name__)

BNI_STATUS_TO_PROMPT = {
    "onboarding_greeting": "ONBOARDING_GREETING",
    "onboarding_profile": "ONBOARDING_PROFILE",
    "icp_discovery": "ICP_DISCOVERY",
    "onboarding_complete": "ONBOARDING_COMPLETE",
    "match_suggested": "MATCH_SUGGESTION",
    "coordination_a_availability": "COORDINATION_AVAILABILITY",
    "coordination_b_availability": "COORDINATION_AVAILABILITY",
    "coordination_overlap_proposed": "COORDINATION_AVAILABILITY",
    "post_meeting_followup": "POST_MEETING_FOLLOWUP",
    "kpi_query": "KPI_QUERY",
    "general_qa": "GENERAL_QA",
    "idle": "IDLE",
}

BNI_PROFILE_FIELDS = [
    "company_name",
    "industry",
    "designation",
    "services_offered",
    "ideal_customer_profile",
]


def register_bni_flow():
    """Register the BNI flow template. Called from modules/bni/__init__.py."""
    from modules.bni.state_handlers import bni_handle_state_transition, bni_create_state

    bni_flow = FlowTemplate(
        name="bni",
        status_to_prompt=BNI_STATUS_TO_PROMPT,
        initial_status="onboarding_greeting",
        profile_fields=BNI_PROFILE_FIELDS,
        state_transition_handler=bni_handle_state_transition,
        create_state_handler=bni_create_state,
    )
    register_flow(bni_flow)
    logger.info("BNI flow template registered")
