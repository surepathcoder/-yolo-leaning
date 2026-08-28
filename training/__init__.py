"""Custom-model dataset tooling and training.

This package is how a retailer's own products become detectable. It prepares a
YOLO-format dataset, fine-tunes a small base model on it, and hands the result
to ``app.services.models.register_model`` as a versioned :class:`ModelVersion`.

It has no dependency on the web framework or the database -- ``train.py`` can
run on a workstation with just ``vision/requirements.txt`` installed, and
``register.py`` produces a plain dict that the backend persists. Ultralytics
is imported lazily so importing this package costs nothing.

See ``docs/CUSTOM_MODELS.md`` for the end-to-end flow.
"""

from training.datasets import (
    DatasetError,
    DatasetSpec,
    load_dataset,
    validate,
    write_data_yaml,
)
from training.register import manifest, sha256_file
from training.train import TrainingResult, train

__all__ = [
    "DatasetError",
    "DatasetSpec",
    "TrainingResult",
    "load_dataset",
    "manifest",
    "sha256_file",
    "train",
    "validate",
    "write_data_yaml",
]
