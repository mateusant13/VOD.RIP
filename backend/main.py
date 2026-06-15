"""
Backward-compatibility shim — imports the FastAPI ``app`` from the new modular structure.

Previously ``main.py`` contained all routes and helpers inline.  It has been
split into ``deps.py``, ``utils.py``, ``routers/*.py``, and ``app.py``.
This shim preserves ``from main import app`` imports (e.g. in
``__main_launcher__.py``) until all callers are updated.
"""

from app import app