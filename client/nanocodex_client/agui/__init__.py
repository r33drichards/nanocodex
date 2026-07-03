"""AG-UI bridge for nanocodex: translate Codex app-server JSON-RPC into AG-UI
SSE events so any AG-UI client (CopilotKit, Dojo, ...) can drive nanocodex.

Phase 0 (spike/pins) and the pure event mapper (`mapper`) are in place; the
FastAPI router and thread store land in Phase 1. See PHASE0.md.
"""

from .mapper import RunState, map_notification, run_error, run_started

__all__ = ["RunState", "map_notification", "run_started", "run_error"]
