"""
Flow Template Registry

Defines conversation flow templates that control:
- What context_status values are valid
- How context_status maps to prompt names
- What profile fields to extract from LLM responses
- What side-effect handlers run on state transitions

Built-in flows:
- "generic": Simple greeting -> active -> idle (pure prompt-driven)
- "bni": Full BNI chapter flow (registered by modules/bni/ on startup)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional, Callable, Any, Awaitable

logger = logging.getLogger(__name__)

# Type alias for state transition handlers
StateTransitionHandler = Callable[..., Awaitable[Optional[str]]]


@dataclass
class FlowTemplate:
    """Defines a conversation flow for a type of client."""
    name: str
    status_to_prompt: dict[str, str]
    initial_status: str = "greeting"
    profile_fields: list[str] = field(default_factory=list)
    state_transition_handler: Optional[StateTransitionHandler] = None
    create_state_handler: Optional[Callable[..., Awaitable[Any]]] = None

    def get_prompt_name(self, context_status: str) -> str:
        """Map a context_status to the prompt name."""
        return self.status_to_prompt.get(context_status, self._default_prompt_name())

    def _default_prompt_name(self) -> str:
        """Fallback prompt name when status not in mapping."""
        if "ACTIVE" in self.status_to_prompt.values():
            return "ACTIVE"
        if "GENERAL_QA" in self.status_to_prompt.values():
            return "GENERAL_QA"
        # Return the last prompt in the mapping
        return list(self.status_to_prompt.values())[-1] if self.status_to_prompt else "ACTIVE"


# =====================
# Built-in: Generic flow
# =====================

GENERIC_FLOW = FlowTemplate(
    name="generic",
    status_to_prompt={
        "greeting": "GREETING",
        "active": "ACTIVE",
        "idle": "IDLE",
    },
    initial_status="greeting",
    profile_fields=[],  # Generic: no predefined fields, all in profile_data JSONB
)


# =====================
# Registry
# =====================

_registry: dict[str, FlowTemplate] = {
    "generic": GENERIC_FLOW,
}


def register_flow(template: FlowTemplate):
    """Register a flow template. Called by modules on startup."""
    _registry[template.name] = template
    logger.info(f"Registered flow template: '{template.name}' ({len(template.status_to_prompt)} statuses)")


def get_flow(name: str) -> FlowTemplate:
    """Get a flow template by name. Falls back to generic if not found."""
    flow = _registry.get(name)
    if flow is None:
        logger.warning(f"Flow template '{name}' not found, falling back to 'generic'")
        return _registry["generic"]
    return flow


def get_all_flows() -> dict[str, FlowTemplate]:
    """Get all registered flow templates."""
    return dict(_registry)


def is_flow_registered(name: str) -> bool:
    """Check if a flow template is registered."""
    return name in _registry
