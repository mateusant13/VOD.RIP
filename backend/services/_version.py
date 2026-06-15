"""VOD.RIP version — single source of truth."""

__version__ = "1.0.47"
VERSION = __version__

# Honest, self-identifying User-Agent for outgoing requests that don't already
# require a browser-fingerprint (curl_cffi impersonation is unaffected).
# A transparent UA *reduces* EDR suspicion compared to the default
# ``python-requests/X.Y`` string, and makes the network behavior audit-friendly.
USER_AGENT = f"VOD.RIP/{__version__} (+https://github.com/mateusant13/VOD.RIP)"
