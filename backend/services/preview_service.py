"""Legacy compat — prefer ``from services.preview import ...``.

Re-exports every name from the ``services.preview`` package so all
existing importers keep working without code changes.
"""
from services import preview  # noqa: E402

# ponytail: lazy __getattr__ instead of an eager dir() copy — the eager copy
# races the preview -> preview_service circular import (if services.preview.session
# is imported first, the package is mid-init and the snapshot misses names like
# _manager). Forwarding keeps every ``from services.preview_service import X``
# working regardless of import order.
def __getattr__(name: str):
    return getattr(preview, name)
