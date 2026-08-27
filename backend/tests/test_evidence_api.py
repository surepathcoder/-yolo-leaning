"""Milestone 13 -- the evidence delivery endpoint.

Evidence is never a public URL. Each request is authorised and audited, and
the headline case from ``docs/PRIVACY.md`` -- an ANALYST cannot see pictures of
people -- is asserted directly.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.models import AuditLog, Organization
from app.models.enums import AuditAction, EvidenceKind, UserRole
from app.services import evidence as evidence_service
from tests import factories as f

PASSWORD = "tembo-anga-baridi-91"
JPEG = b"\xff\xd8\xff\xe0" + b"snapshot-bytes" * 8


@pytest.fixture(autouse=True)
def _evidence_root(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "EVIDENCE_STORAGE_PATH", str(tmp_path))
    return tmp_path


@pytest.fixture()
def ctx(client, db_session):
    """A registered org admin plus a stored snapshot on one of its events."""
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": "admin@duka.co.tz",
            "password": PASSWORD,
            "full_name": "Org Admin",
            "organization_name": "Duka la Amina",
        },
    ).json()
    headers = {"Authorization": f"Bearer {registered['tokens']['access_token']}"}
    org = db_session.get(Organization, uuid.UUID(registered["organization_id"]))

    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    event = f.make_event(db_session, camera)
    evidence = evidence_service.store_artifact(
        db_session, event, kind=EvidenceKind.SNAPSHOT, data=JPEG, width=640, height=480
    )
    db_session.commit()
    return {"headers": headers, "org": org, "event": event, "evidence": evidence}


def login(client, db_session, org, *, email, role) -> dict:
    f.make_user(db_session, org, email=email, hashed_password=hash_password(PASSWORD), role=role)
    db_session.commit()
    tokens = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).json()[
        "tokens"
    ]
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_view_returns_the_bytes_and_writes_one_audit_row(client, db_session, ctx):
    response = client.get(f"/api/v1/evidence/{ctx['evidence'].id}", headers=ctx["headers"])
    assert response.status_code == 200, response.text
    assert response.content == JPEG
    assert response.headers["content-type"].startswith("image/jpeg")
    assert "inline" in response.headers["content-disposition"]

    rows = db_session.scalars(
        select(AuditLog).where(AuditLog.action == AuditAction.EVIDENCE_ACCESSED)
    ).all()
    assert len(rows) == 1
    assert rows[0].resource_id == str(ctx["evidence"].id)


def test_analyst_cannot_view_evidence(client, db_session, ctx):
    headers = login(
        client, db_session, ctx["org"], email="analyst@duka.co.tz", role=UserRole.ANALYST
    )
    response = client.get(f"/api/v1/evidence/{ctx['evidence'].id}", headers=headers)
    assert response.status_code == 403


def test_download_requires_the_export_permission(client, db_session, ctx):
    operator = login(
        client, db_session, ctx["org"], email="guard@duka.co.tz", role=UserRole.SECURITY_OPERATOR
    )
    # Security operator may view...
    assert client.get(f"/api/v1/evidence/{ctx['evidence'].id}", headers=operator).status_code == 200
    # ...but not download as an attachment.
    denied = client.get(f"/api/v1/evidence/{ctx['evidence'].id}/download", headers=operator)
    assert denied.status_code == 403

    ok = client.get(f"/api/v1/evidence/{ctx['evidence'].id}/download", headers=ctx["headers"])
    assert ok.status_code == 200
    assert "attachment" in ok.headers["content-disposition"]


def test_another_tenant_gets_a_404(client, db_session, ctx):
    other_org = f.make_organization(db_session)
    headers = login(
        client,
        db_session,
        other_org,
        email="outsider@other.co.tz",
        role=UserRole.ORGANIZATION_ADMIN,
    )
    response = client.get(f"/api/v1/evidence/{ctx['evidence'].id}", headers=headers)
    assert response.status_code == 404


def test_a_missing_file_is_a_404_not_a_500(client, db_session, ctx, _evidence_root):
    (_evidence_root / ctx["evidence"].storage_path).unlink()
    response = client.get(f"/api/v1/evidence/{ctx['evidence'].id}", headers=ctx["headers"])
    assert response.status_code == 404


def test_unknown_id_is_a_404(client, ctx):
    response = client.get(f"/api/v1/evidence/{uuid.uuid4()}", headers=ctx["headers"])
    assert response.status_code == 404


def test_maintenance_endpoint_runs_the_sweep(client, db_session, ctx):
    from datetime import UTC, datetime, timedelta

    ctx["evidence"].expires_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()

    response = client.post("/api/v1/system/maintenance/retention", headers=ctx["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["evidence_deleted"] == 1
