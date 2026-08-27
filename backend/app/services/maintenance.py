"""Periodic housekeeping the process runs on itself.

Right now that is retention: deleting evidence past its ``expires_at`` (a
privacy obligation, ``docs/PRIVACY.md``) and clearing refresh tokens that
expired long ago. Kept in one place so a deployment that runs an external cron
instead can call :func:`run_retention_sweep` directly and leave the in-process
loop disabled.

Event pruning is deliberately *not* here: it is per-organization and governed
by plan limits, which is a different concern from "the disk should not fill
with footage nobody is allowed to keep".
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.db.session import session_scope
from app.services import auth as auth_service
from app.services import evidence as evidence_service

log = get_logger(__name__)


def run_retention_sweep() -> dict[str, int]:
    """Delete expired evidence and refresh tokens. Returns the counts."""
    with session_scope() as session:
        evidence_deleted = evidence_service.prune_expired_evidence(session)
        tokens_deleted = auth_service.prune_expired_tokens(session)

    result = {"evidence_deleted": evidence_deleted, "tokens_deleted": tokens_deleted}
    log.info("retention_sweep_complete", **result)
    return result
