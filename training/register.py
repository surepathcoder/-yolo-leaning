"""Turn a :class:`~training.train.TrainingResult` into a registration payload.

The training package never touches the database. This module produces the
plain dict that ``app.services.models.register_model`` accepts, so the two
halves can be developed and tested apart.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from training.train import TrainingResult


def sha256_file(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of a file, streamed so a large weights file is not read at once."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest(result: TrainingResult, *, name: str, version: str, notes: str | None = None) -> dict:
    """The keyword arguments for ``models.register_model``, minus the session/scope.

    ``weights`` is the raw bytes -- the caller decides whether to hand them to
    the service directly (local run) or POST them to ``/models`` (remote run).
    """
    weights_path = Path(result.weights_path)
    return {
        "name": name,
        "version": version,
        "weights": weights_path.read_bytes(),
        "checksum": sha256_file(weights_path),
        "class_names": list(result.class_names),
        "base_model": result.base_model,
        "image_size": result.image_size,
        "metrics": dict(result.metrics),
        "dataset_name": result.dataset_name,
        "dataset_size": result.dataset_size,
        "training_config": {
            "epochs": result.epochs,
            "image_size": result.image_size,
            "base_model": result.base_model,
        },
        "trained_at": result.trained_at,
        "notes": notes,
    }
