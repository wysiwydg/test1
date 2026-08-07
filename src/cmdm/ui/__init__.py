"""Business and steward consoles, server-rendered.

Server-side rendering rather than a single-page app so that authorization lives
in one place and a VIEWER's browser never receives the PII it is not allowed to
see.
"""

from cmdm.ui.console import router

__all__ = ["router"]
