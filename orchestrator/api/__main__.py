"""Process entry point: ``python -m orchestrator.api``.

A dedicated module rather than a ``__main__`` guard inside ``cli``: this package's
``__init__`` imports ``cli`` eagerly, so running that module with ``-m`` finds it already
in ``sys.modules`` and runpy writes a RuntimeWarning to stderr. The CLI contract requires
stderr to carry errors and stdout to stay machine-readable, and a warning on every
invocation breaks both.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
