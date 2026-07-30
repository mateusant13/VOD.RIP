"""Legacy compat — prefer ``from services.preview import ...``.

Re-exports every name from the ``services.preview`` package so all
existing importers keep working without code changes.
"""
from services import preview  # noqa: E402

# Copy all public attributes from the preview package into this module
# so ``from services.preview_service import X`` and
# ``from services import preview_service as ps; ps.X`` both work.
for _attr in dir(preview):
    if _attr.startswith("__"):
        continue
    globals()[_attr] = getattr(preview, _attr)
