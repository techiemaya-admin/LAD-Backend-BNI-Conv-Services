"""
BNI Module — Business networking chapter features.

Provides:
- Member onboarding flow (profile, ICP discovery)
- 1-to-1 matching algorithm
- Meeting coordination
- Post-meeting followup
- KPI tracking

Registers the "bni" FlowTemplate on import.
"""
from modules.bni.flow import register_bni_flow

__all__ = ["register_bni_flow"]
