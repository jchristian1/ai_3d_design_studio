"""Entry point so the worker can be started as a process.

    python -m blender_worker

All logic lives in ``main.py``; this file only exists so the module is executable.
"""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
