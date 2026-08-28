# Custom product detection

The default COCO model recognises *a person*, *a bottle*, *a backpack*. It
cannot tell an *Azam 500 ml* carton from an *Azam 1 L*. Milestone 15 lets a
retailer train a small model on their **own** products and run it as a second
detection pass, so the inventory engine measures a shelf by real product class
instead of "any box that is not a person".

## The pieces

| Layer | What |
|-------|------|
| `training/` | dataset validation, a train script wrapping Ultralytics, and a registration manifest. No database or web-framework dependency. |
| `ModelVersion` | one fine-tuned model, bound to an organization (or shared). Weights on disk under `MODEL_STORAGE_PATH` by a relative path; the row carries a SHA-256. |
| `app.services.models` | register, list, **activate** (mAP gate, one active per name), archive, assign to a camera. |
| `pipeline_manager` | builds a `ProductModelConfig` for a camera whose assigned model is `ACTIVE`, same-org and checksum-verified; the pipeline loads it. |
| `CameraPipeline` | runs the product model on its own interval (`MODEL_DETECTION_INTERVAL_SECONDS`), feeds the boxes to the occupancy engine, and never to the security rules. |
| `ProductDetection` | one recognised product, linked to a catalogue `Product` by `detection_class_name` when one matches. |

## Flow

```
1. Collect and label shelf images  ->  YOLO-format dataset
     images/{train,val}/*.jpg  labels/{train,val}/*.txt  classes.txt

2. python -m training.train --dataset <dir> --name products --version v1
     -> best.pt + validation metrics (mAP50, mAP50-95, precision, recall)

3. Register:
   - locally:  app.services.models.register_model(session, scope, **training.manifest(result, name=..., version=...))
   - remotely: POST /api/v1/models   (multipart: the .pt file + a metadata JSON)
     -> status VALIDATED (metrics present) or DRAFT (none)

4. Review the metrics, then:
   POST /api/v1/models/{id}/activate
     -> refused unless map50 >= MODEL_MIN_MAP50 (default 0.30)
     -> the previous ACTIVE version of this name becomes ARCHIVED

5. Set each product's detection_class_name to the class the model emits
   (PATCH /api/v1/products/{id}).

6. Assign the model to a camera:
   PUT /api/v1/models/{id}/cameras/{camera_id}
     -> a running pipeline picks it up without a restart

7. The camera's pipeline now runs the model every few seconds over the frame,
   the occupancy engine measures shelves by product class, and every box is
   written as a ProductDetection (GET /api/v1/inventory/product-detections).
```

## What it refuses to do

- **A weak model cannot go live.** `activate` rejects anything without recorded
  metrics, or whose `map50` is below `MODEL_MIN_MAP50`. A model that misses half
  the stock is worse than none: it teaches the occupancy engine to under-count,
  and nobody notices.
- **A model never crosses organizations.** The loader refuses a model whose
  `organization_id` is neither null (shared) nor the camera's. Class ids are
  meaningless across models, so `class_names` is stored with every version and
  the loader binds to that version, never to a name.
- **A tampered model will not load.** The weights' SHA-256 is checked against
  the recorded checksum every time the pipeline builds the detector.
- **A carton is not a person.** Product boxes feed occupancy only. They are
  never seen by the loitering, zone or line rules — a stocked shelf must not
  trip a security alert.
- **A missing or broken model does not blind the camera.** It falls back to the
  shared COCO model, exactly as it ran before a model was assigned, and logs
  why.

## Training on CPU

The bundled `training/samples/products/` dataset and the `train.py` defaults
(`imgsz=320`, `augment` off for a tiny set) let a nano model fine-tune in a few
minutes with no GPU. A real customer dataset wants more images, more epochs and
augmentation on. GPU hardware just needs a larger `--imgsz` and `--epochs`.

## Configuration

| Setting | Default | Effect |
|---------|---------|--------|
| `MODEL_STORAGE_PATH` | `data/models` | Where weights live |
| `MODEL_MIN_MAP50` | `0.30` | Activation gate |
| `MODEL_DETECTION_INTERVAL_SECONDS` | `5.0` | How often the product model runs on a camera |
