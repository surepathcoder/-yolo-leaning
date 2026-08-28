"""Generate the bundled sample dataset.

A real dataset is a retailer's photographed shelves. For the test suite and
for anyone kicking the tyres, that is overkill: this draws coloured rectangles
("cartons") on textured noise and writes the matching YOLO labels. It is
deliberately learnable in a two-epoch run so `test_training.py` stays fast.

Run once; the output under ``training/samples/products/`` is committed so the
test does not depend on regenerating it (and so it cannot drift):

    python -m training.build_sample
"""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np
from PIL import Image

SAMPLE_ROOT = Path(__file__).parent / "samples" / "products"
CLASS_NAMES = ["red_carton", "blue_carton"]
CLASS_COLOURS = {0: (220, 40, 40), 1: (40, 70, 220)}
IMG_SIZE = 128
SPLITS = {"train": 40, "val": 12}
SEED = 20260828


_Label = tuple[int, float, float, float, float]


def _draw_image(rng: random.Random) -> tuple[Image.Image, list[_Label]]:
    # A near-flat shelf background: a little texture so it is not degenerate,
    # but the cartons are unmistakable so the toy model learns in a few epochs.
    shade = rng.randint(150, 190)
    canvas = np.full((IMG_SIZE, IMG_SIZE, 3), shade, dtype=np.uint8)
    canvas += (
        np.random.default_rng(rng.randint(0, 1 << 30))
        .integers(-8, 9, size=canvas.shape, dtype=np.int16)
        .clip(0, 255)
        .astype(np.uint8)
        // 8
    )

    labels: list[_Label] = []
    # One carton of each class, so every image teaches both.
    for class_id in (0, 1):
        w = rng.randint(30, 44)
        h = rng.randint(38, 56)
        x1 = rng.randint(3, IMG_SIZE - w - 3)
        y1 = rng.randint(3, IMG_SIZE - h - 3)
        colour = CLASS_COLOURS[class_id]
        canvas[y1 : y1 + h, x1 : x1 + w] = colour
        canvas[y1 : y1 + 5, x1 : x1 + w] = tuple(min(255, c + 30) for c in colour)  # lid
        labels.append(
            (
                class_id,
                (x1 + w / 2) / IMG_SIZE,
                (y1 + h / 2) / IMG_SIZE,
                w / IMG_SIZE,
                h / IMG_SIZE,
            )
        )
    return Image.fromarray(canvas, "RGB"), labels


def build(root: Path = SAMPLE_ROOT) -> Path:
    rng = random.Random(SEED)
    for split, count in SPLITS.items():
        (root / "images" / split).mkdir(parents=True, exist_ok=True)
        (root / "labels" / split).mkdir(parents=True, exist_ok=True)
        for i in range(count):
            image, labels = _draw_image(rng)
            stem = f"{split}_{i:03d}"
            image.save(root / "images" / split / f"{stem}.jpg", quality=92)
            (root / "labels" / split / f"{stem}.txt").write_text(
                "\n".join(f"{c} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}" for c, cx, cy, w, h in labels)
                + "\n"
            )
    (root / "classes.txt").write_text("\n".join(CLASS_NAMES) + "\n")
    return root


if __name__ == "__main__":
    out = build()
    print(f"sample dataset written to {out}")
