"""Milestone 13 -- evidence storage, retrieval and retention (service layer).

The rules under test are the ones ``docs/SECURITY.md`` and ``docs/PRIVACY.md``
commit to: a stored artefact carries a relative path and a checksum, it has a
death date from the moment it is written, and the sweep actually deletes both
the row and the file when that date passes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.tenancy import TenantScope
from app.models import EventEvidence
from app.models.enums import EvidenceKind, UserRole
from app.services import evidence as evidence_service
from app.services.maintenance import run_retention_sweep
from tests import factories as f


@pytest.fixture(autouse=True)
def _evidence_root(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "EVIDENCE_STORAGE_PATH", str(tmp_path))
    return tmp_path


@pytest.fixture()
def event(db_session):
    org = f.make_organization(db_session)
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    return f.make_event(db_session, camera)


def scope_for(event, role=UserRole.ORGANIZATION_ADMIN, **kwargs) -> TenantScope:
    return TenantScope(
        user_id=uuid.uuid4(),
        organization_id=event.organization_id,
        role=role,
        **kwargs,
    )


JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64  # not a real image; bytes are opaque here


def test_store_artifact_writes_the_file_and_records_the_row(db_session, event, _evidence_root):
    evidence = evidence_service.store_artifact(
        db_session,
        event,
        kind=EvidenceKind.SNAPSHOT,
        data=JPEG,
        mime_type="image/jpeg",
        width=640,
        height=480,
        bbox=[0.1, 0.1, 0.4, 0.9],
    )
    db_session.flush()

    assert not evidence.storage_path.startswith("/")
    assert str(event.id) in evidence.storage_path
    assert evidence.size_bytes == len(JPEG)
    assert evidence.checksum and len(evidence.checksum) == 64
    on_disk = _evidence_root / evidence.storage_path
    assert on_disk.read_bytes() == JPEG


def test_retention_window_differs_for_clips_and_stills(db_session, event):
    settings = get_settings()
    snap = evidence_service.store_artifact(db_session, event, kind=EvidenceKind.SNAPSHOT, data=JPEG)
    clip = evidence_service.store_artifact(
        db_session, event, kind=EvidenceKind.CLIP, data=JPEG, mime_type="video/mp4"
    )
    assert snap.expires_at - snap.captured_at == timedelta(days=settings.SNAPSHOT_RETENTION_DAYS)
    assert clip.expires_at - clip.captured_at == timedelta(days=settings.EVIDENCE_RETENTION_DAYS)


def test_resolve_evidence_requires_the_view_permission(db_session, event):
    evidence = evidence_service.store_artifact(
        db_session, event, kind=EvidenceKind.SNAPSHOT, data=JPEG
    )
    db_session.flush()

    with pytest.raises(PermissionDeniedError):
        evidence_service.resolve_evidence(
            db_session, scope_for(event, UserRole.ANALYST), evidence.id
        )

    found = evidence_service.resolve_evidence(db_session, scope_for(event), evidence.id)
    assert found.id == evidence.id


def test_resolve_evidence_is_tenant_scoped(db_session, event):
    evidence = evidence_service.store_artifact(
        db_session, event, kind=EvidenceKind.SNAPSHOT, data=JPEG
    )
    db_session.flush()

    other_org = f.make_organization(db_session)
    other_scope = TenantScope(
        user_id=uuid.uuid4(),
        organization_id=other_org.id,
        role=UserRole.ORGANIZATION_ADMIN,
    )
    with pytest.raises(NotFoundError):
        evidence_service.resolve_evidence(db_session, other_scope, evidence.id)


def test_prune_expired_evidence_deletes_row_and_file(db_session, event, _evidence_root):
    keep = evidence_service.store_artifact(db_session, event, kind=EvidenceKind.SNAPSHOT, data=JPEG)
    drop = evidence_service.store_artifact(db_session, event, kind=EvidenceKind.SNAPSHOT, data=JPEG)
    drop.expires_at = datetime.now(UTC) - timedelta(days=1)
    db_session.flush()

    drop_path = _evidence_root / drop.storage_path
    assert drop_path.exists()

    removed = evidence_service.prune_expired_evidence(db_session)
    assert removed == 1
    assert not drop_path.exists()
    assert (_evidence_root / keep.storage_path).exists()

    remaining = db_session.scalars(select(EventEvidence)).all()
    assert [e.id for e in remaining] == [keep.id]


def test_retention_sweep_reports_counts(db_session, event):
    stale = evidence_service.store_artifact(
        db_session, event, kind=EvidenceKind.SNAPSHOT, data=JPEG
    )
    stale.expires_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()

    result = run_retention_sweep()
    assert result["evidence_deleted"] == 1
    assert "tokens_deleted" in result
