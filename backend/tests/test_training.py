"""Milestone 15 -- dataset validation and a real (short) training run.

The training run is marked slow and vision: it fine-tunes a nano model on the
bundled synthetic dataset for a handful of epochs. It asserts the *shape* of
the result -- weights produced, loadable, metrics extracted -- not an exact
mAP, because training is not bit-deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from training.datasets import DatasetError, load_dataset, validate, write_data_yaml

SAMPLE = str(Path(__file__).resolve().parents[2] / "training" / "samples" / "products")


# --------------------------------------------------------------- validation
def test_valid_sample_dataset_loads_and_validates():
    spec = validate(load_dataset(SAMPLE))
    assert spec.class_names == ["red_carton", "blue_carton"]
    assert spec.train_images and spec.val_images


def test_missing_classes_file(tmp_path):
    (tmp_path / "images" / "train").mkdir(parents=True)
    with pytest.raises(DatasetError, match=r"classes.txt"):
        load_dataset(tmp_path)


def test_missing_label_is_rejected(tmp_path):
    _skeleton(tmp_path, with_labels=False)
    with pytest.raises(DatasetError, match="no label file"):
        validate(load_dataset(tmp_path))


def test_out_of_range_class_id_is_rejected(tmp_path):
    _skeleton(tmp_path, label_text="5 0.5 0.5 0.2 0.2\n")
    with pytest.raises(DatasetError, match="out of range"):
        validate(load_dataset(tmp_path))


def test_non_normalised_box_is_rejected(tmp_path):
    _skeleton(tmp_path, label_text="0 0.5 0.5 40 40\n")
    with pytest.raises(DatasetError, match="normalised"):
        validate(load_dataset(tmp_path))


def test_empty_val_split_is_rejected(tmp_path):
    _skeleton(tmp_path, val=False)
    with pytest.raises(DatasetError, match="val"):
        validate(load_dataset(tmp_path))


def test_data_yaml_round_trips(tmp_path):
    import yaml

    spec = validate(load_dataset(SAMPLE))
    out = write_data_yaml(spec, tmp_path / "data.yaml")
    loaded = yaml.safe_load(out.read_text())
    assert loaded["names"] == {0: "red_carton", 1: "blue_carton"}
    assert loaded["train"] == "images/train"


def _skeleton(root, *, with_labels=True, label_text="0 0.5 0.5 0.2 0.2\n", val=True):
    """A minimal one-image-per-split dataset for the validation tests."""
    import numpy as np
    from PIL import Image

    (root / "classes.txt").write_text("red_carton\nblue_carton\n")
    splits = ["train", "val"] if val else ["train"]
    for split in splits:
        (root / "images" / split).mkdir(parents=True, exist_ok=True)
        (root / "labels" / split).mkdir(parents=True, exist_ok=True)
        Image.fromarray(np.zeros((32, 32, 3), dtype=np.uint8)).save(
            root / "images" / split / "a.jpg"
        )
        if with_labels:
            (root / "labels" / split / "a.txt").write_text(label_text)


# ------------------------------------------------------------- real training
@pytest.mark.slow
@pytest.mark.vision
def test_a_short_training_run_produces_a_registrable_model(tmp_path):
    from ultralytics import YOLO

    from training import manifest
    from training.train import train

    result = train(SAMPLE, epochs=6, imgsz=160, batch=8, out_dir=tmp_path, augment=False)

    assert result.weights_path.is_file()
    assert result.class_names == ["red_carton", "blue_carton"]
    assert isinstance(result.map50, float) and 0.0 <= result.map50 <= 1.0
    assert result.dataset_size == 52

    # The weights load and inference runs (whether the toy model is any good is
    # not this test's concern).
    model = YOLO(str(result.weights_path))
    model.predict(f"{SAMPLE}/images/val/val_000.jpg", imgsz=160, device="cpu", verbose=False)

    payload = manifest(result, name="products", version="test-v1")
    assert payload["checksum"] and len(payload["checksum"]) == 64
    assert payload["class_names"] == ["red_carton", "blue_carton"]
    assert isinstance(payload["weights"], bytes) and payload["weights"]
