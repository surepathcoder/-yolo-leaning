# training/

Dataset tooling and training for **custom product detection** (milestone 15).

The default COCO model recognises people, bags and generic objects. It does
not know a *Azam 500 ml juice carton* from an *Azam 1 L*. This package
fine-tunes a small YOLO model on a retailer's own products so the inventory
engine can measure a shelf by real product class instead of "any box that is
not a person".

## The flow

```
1. Collect and label shelf images  ->  YOLO-format dataset (see layout below)
2. python -m training.train --dataset <dir> --name products --version v1
3. app.services.models.register_model(...)   (the manifest from training.register)
4. Review metrics, then POST /api/v1/models/{id}/activate    (mAP gate applies)
5. PUT /api/v1/models/{id}/cameras/{camera_id}
6. The camera's pipeline loads the model (org-scoped, checksum-verified) and
   runs it as a second detection pass over the shelf region.
```

Full detail: [`../docs/CUSTOM_MODELS.md`](../docs/CUSTOM_MODELS.md).

## Dataset layout

```
<root>/
  images/train/*.jpg   images/val/*.jpg
  labels/train/*.txt   labels/val/*.txt      # one "<class_id> cx cy w h" line per object, all 0..1
  classes.txt                                # one class name per line, index = class id
```

`training.datasets.validate()` rejects a dataset with an unlabelled image, an
out-of-range class id, a non-normalised box, or an empty split — before a
training run is wasted on it.

## The bundled sample

`training/samples/products/` is a tiny synthetic dataset (coloured "cartons"
on a shelf background, two classes). It exists so the test suite can do a real
short training run in CI. Regenerate it with `python -m training.build_sample`;
the output is committed so it cannot drift.

## Requirements

`vision/requirements.txt` (ultralytics, torch, opencv, numpy) plus `pyyaml`
and `pillow`, which ultralytics already pulls in. CPU training is the assumed
path; pass a real GPU box a larger `--imgsz` and `--epochs`.
