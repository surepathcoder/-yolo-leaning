# training/configs/

Base weights for fine-tuning live here. `.gitignore` allows `*.pt` in this
directory (a documented, versioned base model is different from a stray
checkpoint), but none is committed by default -- `train.py` uses `yolo26n.pt`
from the repo root. Drop a larger base model here and pass `--base-model
training/configs/<file>.pt` to use it.
