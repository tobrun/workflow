"""Deliberate API surface that vulture cannot see a caller for.

Every name below is invoked by a framework or a base class rather than by
repository code, so the dead-code pass would otherwise report it. Vulture
parses this file, it never runs it, so referencing a name here is the whole
mechanism. Add one only when a framework really owns the call, and name that
framework in the comment.
"""

from factory.evals.fixture.webhook import WebhookHandler

# http.server dispatches these on a BaseHTTPRequestHandler subclass: do_POST
# per request verb, log_message for every access-log line.
FRAMEWORK_ENTRY_POINTS = (
    WebhookHandler.do_POST,
    WebhookHandler.log_message,
)

__all__ = ["FRAMEWORK_ENTRY_POINTS"]
