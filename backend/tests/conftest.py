"""Shared pytest fixtures.

Unit tests run against an in-memory SQLite database so the suite is fast and
hermetic; tests marked ``integration`` use the real PostgreSQL instance from
docker compose.  SQLite is a *test* convenience only -- the production
architecture is PostgreSQL (see docs/ARCHITECTURE.md).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make `app` (backend/) and `vision` (repo root) importable.
BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
for path in (str(BACKEND_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

# Configure the test environment before any app module reads settings.
os.environ.setdefault("VISIONGUARD_ENV_FILE", str(REPO_ROOT / ".env.test"))
os.environ["ENVIRONMENT"] = "test"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["JWT_SECRET"] = "test-secret-not-used-in-production"
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
# The suite drives the retention sweep explicitly; a background loop would race
# the in-memory database teardown between tests.
os.environ.setdefault("RETENTION_SWEEP_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import reload_settings
from app.db.base import Base


@pytest.fixture(scope="session", autouse=True)
def _settings():
    return reload_settings()


@pytest.fixture(scope="session")
def sqlite_engine():
    """One shared in-memory SQLite engine for the whole test session."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    import app.models  # noqa: F401  ensure all models are registered

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _bind_global_engine(sqlite_engine):
    """Point the process-wide engine at the test database.

    Request handlers get their session through the ``get_db`` dependency, which
    tests override. Background work does not: pipeline threads, the retention
    worker and the edge sync all call ``session_scope()``, which builds a
    session from the global engine.

    Without this, that code silently talks to a *different* empty database and
    its failures are swallowed by the same error handling that makes it robust
    in production -- so a broken worker would look like a passing test.
    """
    from app.db import session as session_module

    original_engine = session_module._engine
    original_factory = session_module._SessionLocal

    session_module._engine = sqlite_engine
    session_module._SessionLocal = sessionmaker(
        bind=sqlite_engine, autoflush=False, expire_on_commit=False
    )
    try:
        yield
    finally:
        session_module._engine = original_engine
        session_module._SessionLocal = original_factory


@pytest.fixture()
def db_session(sqlite_engine) -> Session:
    """A session whose data is wiped after each test.

    Tables are truncated rather than wrapping each test in a transaction that
    is rolled back.  Several services commit deliberately -- a failed login
    must persist its lockout counter even though the request then aborts --
    and a committing service inside a rollback-isolated test either leaks
    state into the next test or has its commit silently neutered.  Truncating
    keeps isolation honest and lets tests exercise the real commit behaviour.
    """
    factory = sessionmaker(bind=sqlite_engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with sqlite_engine.begin() as connection:
            for table in reversed(Base.metadata.sorted_tables):
                connection.execute(table.delete())


@pytest.fixture()
def app(db_session):
    """FastAPI app with the database dependency pointed at the test session."""
    from app.db.session import get_db
    from app.main import create_app

    application = create_app()
    application.dependency_overrides[get_db] = lambda: db_session
    return application


@pytest.fixture()
def client(app) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Integration fixtures -- these use the real PostgreSQL from docker compose.
# SQLite proves the models are self-consistent; only PostgreSQL proves the
# schema actually deploys and that the constraints behave as intended.
# ---------------------------------------------------------------------------
PG_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://visionguard:visionguard@localhost:5434/visionguard_test",
)


def _postgres_available() -> bool:
    from sqlalchemy import create_engine as _ce
    from sqlalchemy import text as _text

    admin_url = PG_URL.rsplit("/", 1)[0] + "/postgres"
    try:
        engine = _ce(admin_url, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            conn.execute(_text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def postgres_engine():
    """A dedicated test database, created and dropped around the session."""
    from sqlalchemy import create_engine as _ce
    from sqlalchemy import text as _text

    if not _postgres_available():
        pytest.skip("PostgreSQL is not reachable -- start deployment/docker/compose.infra.yml")

    db_name = PG_URL.rsplit("/", 1)[1]
    admin_url = PG_URL.rsplit("/", 1)[0] + "/postgres"
    admin = _ce(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(_text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        conn.execute(_text(f'CREATE DATABASE "{db_name}"'))
    admin.dispose()

    engine = create_engine(PG_URL, future=True)
    import app.models  # noqa: F401

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()

    admin = _ce(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(_text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture()
def pg_session(postgres_engine) -> Session:
    connection = postgres_engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    session = factory()
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
