"""Alembic environment.

The database URL comes from the application settings, so migrations use the
same configuration as the app and no credentials are committed to alembic.ini.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
for path in (str(BACKEND_DIR), str(REPO_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import app.models  # noqa: F401  registers every table on Base.metadata
from app.core.config import get_settings
from app.db.base import GUID, Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().DATABASE_URL.replace("%", "%%"))

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Skip objects Alembic should not manage."""
    # Alembic cannot usefully diff the check constraints generated for
    # non-native enums; they are recreated with their table.
    return not (type_ == "table" and name in {"spatial_ref_sys"})


def render_item(type_, obj, autogen_context) -> str | bool:
    """Render VisionGuard's custom column types with their import.

    Without this, autogenerate emits a bare ``app.db.base.GUID()`` reference
    and the migration fails at import time.
    """
    if type_ == "type" and isinstance(obj, GUID):
        autogen_context.imports.add("from app.db.base import GUID")
        return "GUID()"
    return False


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting -- used to review a migration."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
            render_item=render_item,
            # Keep long constraint names within PostgreSQL's 63-char limit.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
