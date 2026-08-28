"""Fine-tune a small YOLO model on a custom product dataset.

CPU is the assumed target here -- the development box has no GPU and an edge
device rarely does either. The defaults (``imgsz=160``, ``epochs=3``,
``yolo26n`` base) are chosen so a smoke run finishes in a couple of minutes; a
real customer dataset wants more of all three.

The output is a :class:`TrainingResult` -- a plain dataclass with the weights
path, the class names in id order, and the validation metrics. That is exactly
what ``training.register.manifest`` turns into the payload for
``app.services.models.register_model``.
"""

from __future__ import annotations

import argparse
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from training.datasets import DatasetSpec, load_dataset, validate, write_data_yaml

DEFAULT_BASE_MODEL = "yolo26n.pt"


@dataclass(slots=True)
class TrainingResult:
    """Everything the backend needs to register a trained model."""

    weights_path: Path
    class_names: list[str]
    metrics: dict
    base_model: str
    image_size: int
    epochs: int
    dataset_name: str
    dataset_size: int
    trained_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def map50(self) -> float | None:
        value = self.metrics.get("map50")
        return float(value) if value is not None else None


def _extract_metrics(results, class_names: list[str]) -> dict:
    """Pull the numbers that matter out of an Ultralytics results object.

    Defensive across versions: the attribute names on ``results.box`` have
    moved between Ultralytics releases, so every read is guarded.
    """
    box = getattr(results, "box", None)
    if box is None:
        return {}

    def scalar(name: str) -> float | None:
        value = getattr(box, name, None)
        try:
            return round(float(value), 5) if value is not None else None
        except (TypeError, ValueError):
            return None

    per_class: dict[str, float] = {}
    maps = getattr(box, "maps", None)  # per-class mAP50-95
    if maps is not None:
        try:
            per_class = {
                class_names[i]: round(float(v), 5)
                for i, v in enumerate(maps)
                if i < len(class_names)
            }
        except (TypeError, ValueError):
            per_class = {}

    return {
        "map50": scalar("map50"),
        "map50_95": scalar("map"),
        "precision": scalar("mp"),
        "recall": scalar("mr"),
        "per_class_map50_95": per_class,
    }


def train(
    dataset_dir: str | Path,
    *,
    base_model: str = DEFAULT_BASE_MODEL,
    epochs: int = 30,
    imgsz: int = 320,
    out_dir: str | Path | None = None,
    batch: int = 8,
    seed: int = 0,
    augment: bool = True,
) -> TrainingResult:
    """Fine-tune ``base_model`` on the dataset at ``dataset_dir``.

    The dataset is validated first -- a bad label set fails here, before the
    hours a real run would take. ``augment=False`` turns off mosaic and photo
    augmentation, which helps on a very small dataset where the default heavy
    augmentation drowns the signal.
    """
    from ultralytics import YOLO

    spec: DatasetSpec = validate(load_dataset(dataset_dir))
    workdir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="vg-train-"))
    workdir.mkdir(parents=True, exist_ok=True)
    data_yaml = write_data_yaml(spec, workdir / "data.yaml")

    aug_off = {
        "mosaic": 0.0,
        "close_mosaic": 0,
        "hsv_h": 0.0,
        "hsv_s": 0.0,
        "hsv_v": 0.0,
        "degrees": 0.0,
        "translate": 0.0,
        "scale": 0.0,
        "fliplr": 0.0,
        "erasing": 0.0,
    }
    model = YOLO(base_model)
    model.train(
        data=str(data_yaml),
        epochs=epochs,
        imgsz=imgsz,
        batch=batch,
        seed=seed,
        device="cpu",
        project=str(workdir / "runs"),
        name="train",
        exist_ok=True,
        verbose=False,
        plots=False,
        **({} if augment else aug_off),
    )
    results = model.val(data=str(data_yaml), imgsz=imgsz, device="cpu", verbose=False, plots=False)

    weights = workdir / "runs" / "train" / "weights" / "best.pt"
    if not weights.is_file():
        raise RuntimeError(f"Training produced no weights file at {weights}")

    return TrainingResult(
        weights_path=weights,
        class_names=list(spec.class_names),
        metrics=_extract_metrics(results, spec.class_names),
        base_model=base_model,
        image_size=imgsz,
        epochs=epochs,
        dataset_name=spec.root.name,
        dataset_size=spec.size,
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description="Fine-tune a custom product detector.")
    parser.add_argument("--dataset", required=True, help="dataset directory (YOLO layout)")
    parser.add_argument("--base-model", default=DEFAULT_BASE_MODEL)
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--imgsz", type=int, default=320)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--no-augment", action="store_true")
    args = parser.parse_args()

    result = train(
        args.dataset,
        base_model=args.base_model,
        epochs=args.epochs,
        imgsz=args.imgsz,
        out_dir=args.out_dir,
        augment=not args.no_augment,
    )
    print(f"weights:  {result.weights_path}")
    print(f"classes:  {result.class_names}")
    print(f"metrics:  {result.metrics}")


if __name__ == "__main__":
    _main()
