"""VisionGuard vision engine.

Deliberately free of any dependency on the backend: this package can be
imported and run on an edge device with only Python, OpenCV and PyTorch
installed.  It emits plain dataclasses; something else decides what to do
with them.
"""

from __future__ import annotations

__version__ = "0.1.0"
