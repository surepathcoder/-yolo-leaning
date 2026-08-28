"""YOLO-format dataset loading and validation.

A dataset is a directory laid out the way Ultralytics expects::

    <root>/
      images/train/*.jpg      images/val/*.jpg
      labels/train/*.txt      labels/val/*.txt
      classes.txt             one class name per line, index = class id

Each label file has one ``<class_id> <cx> <cy> <w> <h>`` line per object, all
four box values normalised to 0..1.

Validation is strict on purpose. A dataset that trains "fine" but has an
image with no label, or a class id past the end of ``classes.txt``, produces a
model that is quietly wrong -- it learns to ignore real objects, or maps a
product to the wrong name. Both are caught here before a training run is
wasted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp"}


class DatasetError(Exception):
    """The dataset is not usable for training. The message names the fix."""


@dataclass(slots=True)
class DatasetSpec:
    """A validated view of a dataset directory."""

    root: Path
    class_names: list[str]
    train_images: list[Path] = field(default_factory=list)
    val_images: list[Path] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.train_images) + len(self.val_images)

    def label_for(self, image: Path) -> Path:
        """The label file that pairs with an image path."""
        split = image.parent.name  # "train" | "val"
        return self.root / "labels" / split / f"{image.stem}.txt"


def _images_in(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)


def load_dataset(root: str | Path) -> DatasetSpec:
    """Read a dataset directory into a :class:`DatasetSpec` (no validation yet)."""
    root = Path(root)
    if not root.is_dir():
        raise DatasetError(f"Dataset directory not found: {root}")

    classes_file = root / "classes.txt"
    if not classes_file.is_file():
        raise DatasetError(f"Missing {classes_file} -- one class name per line.")
    class_names = [line.strip() for line in classes_file.read_text().splitlines() if line.strip()]
    if not class_names:
        raise DatasetError("classes.txt is empty.")

    return DatasetSpec(
        root=root,
        class_names=class_names,
        train_images=_images_in(root / "images" / "train"),
        val_images=_images_in(root / "images" / "val"),
    )


def _check_label(path: Path, class_count: int) -> list[str]:
    """Return a list of problems with one label file (empty = fine)."""
    problems: list[str] = []
    if not path.is_file():
        return [f"{path.name}: no label file"]
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 5:
            problems.append(f"{path.name}:{lineno}: expected 5 values, got {len(parts)}")
            continue
        try:
            class_id = int(parts[0])
            box = [float(v) for v in parts[1:]]
        except ValueError:
            problems.append(f"{path.name}:{lineno}: non-numeric value")
            continue
        if not 0 <= class_id < class_count:
            problems.append(
                f"{path.name}:{lineno}: class id {class_id} out of range 0..{class_count - 1}"
            )
        if any(not 0.0 <= v <= 1.0 for v in box):
            problems.append(f"{path.name}:{lineno}: box values must be normalised 0..1")
    return problems


def validate(spec: DatasetSpec) -> DatasetSpec:
    """Raise :class:`DatasetError` unless the dataset can be trained on.

    Returns the spec so ``validate(load_dataset(path))`` reads cleanly.
    """
    if not spec.train_images:
        raise DatasetError("images/train is empty.")
    if not spec.val_images:
        raise DatasetError(
            "images/val is empty. Training with no validation split gives no "
            "metrics, and a model with no metrics can never be activated."
        )

    problems: list[str] = []
    for image in spec.train_images + spec.val_images:
        problems.extend(_check_label(spec.label_for(image), len(spec.class_names)))

    if problems:
        shown = "\n  ".join(problems[:20])
        more = "" if len(problems) <= 20 else f"\n  ... and {len(problems) - 20} more"
        raise DatasetError(f"Dataset has {len(problems)} label problem(s):\n  {shown}{more}")
    return spec


def write_data_yaml(spec: DatasetSpec, out_path: str | Path) -> Path:
    """Write the Ultralytics ``data.yaml`` for this dataset. Returns its path."""
    import yaml

    out_path = Path(out_path)
    payload = {
        "path": str(spec.root.resolve()),
        "train": "images/train",
        "val": "images/val",
        "names": dict(enumerate(spec.class_names)),
    }
    out_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return out_path
