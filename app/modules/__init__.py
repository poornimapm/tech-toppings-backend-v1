"""Feature modules. Each sub-package with a ``manifest.py`` is discovered at startup (ADR-0001).

A module imports only ``app.platform`` (never ``app.core``, a platform sub-package or another
module), keeps its data in its own collections, and describes itself in ``manifest.py``.
"""
