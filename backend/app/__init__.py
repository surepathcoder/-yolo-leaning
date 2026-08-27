"""VisionGuard backend application.

The backend imports the ``vision`` package, which lives beside ``backend/`` at
the repository root rather than inside it -- deliberately, so the vision engine
can be deployed to an edge device without the web framework.

That layout means the repository root has to be importable. Docker sets
``PYTHONPATH=/app/backend:/app``, and pytest sets ``pythonpath = . ..``, but a
developer running ``uvicorn`` from ``backend/`` has neither. Rather than make
that a documented footgun, the path is ensured here, once, at import time.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
