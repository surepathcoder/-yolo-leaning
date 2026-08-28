"""Milestone 15 -- model registration, the activation gate, and isolation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.tenancy import TenantScope
from app.models import ModelVersion
from app.models.enums import ModelStatus, UserRole
from app.services import models as model_service
from tests import factories as f

WEIGHTS = b"PK\x03\x04 fake-yolo-weights " + b"x" * 200


@pytest.fixture(autouse=True)
def _model_store(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "MODEL_STORAGE_PATH", str(tmp_path))
    return tmp_path


@pytest.fixture()
def org(db_session):
    return f.make_organization(db_session)


def scope_for(org, role=UserRole.ORGANIZATION_ADMIN) -> TenantScope:
    return TenantScope(user_id=uuid.uuid4(), organization_id=org.id, role=role)


def register(session, scope, *, name="products", version="v1", map50=0.8, **kw):
    metrics = {"map50": map50, "map50_95": 0.5} if map50 is not None else None
    return model_service.register_model(
        session,
        scope,
        name=name,
        version=version,
        weights=WEIGHTS,
        class_names=["red_carton", "blue_carton"],
        metrics=metrics,
        **kw,
    )


def test_register_writes_weights_and_records_the_row(db_session, org, _model_store):
    model = register(db_session, scope_for(org))
    db_session.flush()

    assert model.status == ModelStatus.VALIDATED  # metrics present
    assert not model.weights_path.startswith("/")
    assert model.checksum and len(model.checksum) == 64
    assert (_model_store / model.weights_path).read_bytes() == WEIGHTS


def test_register_without_metrics_is_a_draft(db_session, org):
    model = register(db_session, scope_for(org), map50=None)
    assert model.status == ModelStatus.DRAFT
    assert model.map50 is None


def test_duplicate_name_and_version_conflicts(db_session, org):
    register(db_session, scope_for(org))
    with pytest.raises(ConflictError):
        register(db_session, scope_for(org))


def test_activation_gate_rejects_a_weak_model(db_session, org):
    threshold = get_settings().MODEL_MIN_MAP50
    weak = register(db_session, scope_for(org), version="v1", map50=threshold - 0.05)
    with pytest.raises(ConflictError, match="below"):
        model_service.activate_model(db_session, scope_for(org), weak.id)


def test_activation_rejects_a_model_with_no_metrics(db_session, org):
    draft = register(db_session, scope_for(org), map50=None)
    assert draft.status == ModelStatus.DRAFT  # no metrics -> never left DRAFT
    with pytest.raises(ConflictError, match="DRAFT cannot be activated"):
        model_service.activate_model(db_session, scope_for(org), draft.id)


def test_activating_one_version_archives_the_previous(db_session, org):
    v1 = register(db_session, scope_for(org), version="v1", map50=0.7)
    v2 = register(db_session, scope_for(org), version="v2", map50=0.8)

    model_service.activate_model(db_session, scope_for(org), v1.id)
    assert db_session.get(ModelVersion, v1.id).status == ModelStatus.ACTIVE

    model_service.activate_model(db_session, scope_for(org), v2.id)
    db_session.refresh(v1)
    assert v1.status == ModelStatus.ARCHIVED
    assert db_session.get(ModelVersion, v2.id).status == ModelStatus.ACTIVE


def test_a_model_is_invisible_to_another_organization(db_session, org):
    model = register(db_session, scope_for(org))
    db_session.flush()

    other = f.make_organization(db_session)
    other_scope = TenantScope(
        user_id=uuid.uuid4(), organization_id=other.id, role=UserRole.ORGANIZATION_ADMIN
    )
    assert model_service.list_models(db_session, other_scope) == []
    with pytest.raises(NotFoundError):
        model_service.get_model(db_session, other_scope, model.id)


def test_a_shared_model_is_visible_to_everyone(db_session, org):
    shared = ModelVersion(
        organization_id=None,
        name="coco-base",
        version="v1",
        weights_path="shared/coco-base/v1/best.pt",
        class_names=["person"],
        image_size=640,
        metrics={},
        status=ModelStatus.ACTIVE,
    )
    db_session.add(shared)
    db_session.flush()

    assert any(m.id == shared.id for m in model_service.list_models(db_session, scope_for(org)))


@pytest.mark.parametrize("role", [UserRole.ANALYST, UserRole.STORE_MANAGER])
def test_only_an_org_admin_can_register(db_session, org, role):
    # MODEL_WRITE / MODEL_ACTIVATE sit on ORGANIZATION_ADMIN alone.
    with pytest.raises(PermissionDeniedError):
        register(db_session, scope_for(org, role))


def test_assigning_a_non_active_model_to_a_camera_conflicts(db_session, org):
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    model = register(db_session, scope_for(org))  # VALIDATED, not ACTIVE
    db_session.flush()

    with pytest.raises(ConflictError, match="not ACTIVE"):
        model_service.assign_model_to_camera(
            db_session, scope_for(org), camera_id=camera.id, model_id=model.id
        )

    model_service.activate_model(db_session, scope_for(org), model.id)
    camera = model_service.assign_model_to_camera(
        db_session, scope_for(org), camera_id=camera.id, model_id=model.id
    )
    assert camera.model_version_id == model.id

    camera = model_service.assign_model_to_camera(
        db_session, scope_for(org), camera_id=camera.id, model_id=None
    )
    assert camera.model_version_id is None


def test_pipeline_config_loads_an_active_same_org_model(db_session, org, _model_store):
    from app.services.pipeline_manager import build_product_model_config

    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    model = register(db_session, scope_for(org), map50=0.9)
    model_service.activate_model(db_session, scope_for(org), model.id)
    model_service.assign_model_to_camera(
        db_session, scope_for(org), camera_id=camera.id, model_id=model.id
    )
    db_session.flush()

    config = build_product_model_config(db_session, camera)
    assert config is not None
    assert config.model_version_id == str(model.id)
    assert config.class_names == ("red_carton", "blue_carton")


def test_pipeline_config_refuses_a_checksum_mismatch(db_session, org, _model_store):
    from app.services.pipeline_manager import build_product_model_config

    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    model = register(db_session, scope_for(org), map50=0.9)
    model_service.activate_model(db_session, scope_for(org), model.id)
    camera.model_version_id = model.id
    # Tamper with the stored weights after registration.
    (_model_store / model.weights_path).write_bytes(WEIGHTS + b"tampered")
    db_session.flush()

    assert build_product_model_config(db_session, camera) is None


def test_pipeline_config_refuses_a_non_active_or_missing_model(db_session, org):
    from app.services.pipeline_manager import build_product_model_config

    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    validated = register(db_session, scope_for(org), map50=0.9)  # not activated
    camera.model_version_id = validated.id
    db_session.flush()
    assert build_product_model_config(db_session, camera) is None

    camera.model_version_id = uuid.uuid4()  # dangling
    assert build_product_model_config(db_session, camera) is None


def test_record_product_detections_resolves_product_by_class_name(db_session, org):
    store = f.make_store(db_session, org)
    camera = f.make_camera(db_session, store)
    from app.services import inventory as inventory_service

    juice = f.make_product(db_session, org, detection_class_name="red_carton")
    db_session.flush()

    n = inventory_service.record_product_detections(
        db_session,
        organization_id=org.id,
        camera_id=camera.id,
        model_version_id=None,
        detections=[
            {"class_name": "red_carton", "confidence": 0.9, "bbox": (0.1, 0.1, 0.3, 0.4)},
            {"class_name": "unknown_thing", "confidence": 0.8, "bbox": (0.5, 0.5, 0.6, 0.6)},
        ],
        detected_at=datetime.now(UTC),
    )
    assert n == 2

    from app.models import ProductDetection

    rows = db_session.scalars(select(ProductDetection).order_by(ProductDetection.class_name)).all()
    by_class = {r.class_name: r for r in rows}
    assert by_class["red_carton"].product_id == juice.id
    assert by_class["unknown_thing"].product_id is None
