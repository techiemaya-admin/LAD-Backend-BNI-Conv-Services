"""
Modules — pluggable business logic for different client types.

Each module registers its own FlowTemplate on import, providing:
- Conversation state machine
- State transition side effects
- Background tasks
"""
