// Player page bootstrap — loaded BEFORE the vendored IVS/hls engines. The
// engines log their own diagnostics to the console (gap jumps, stall
// warnings, JSON parse retries, polyfill "unhandled rejection" notices);
// their real failures already arrive at the content script as {t:'ev',
// e:'error'} messages via player-bridge.js, so silence error/warn here to
// keep the chrome://extensions error badge clean. Must be a separate file:
// MV3 extension_pages CSP (script-src 'self') forbids inline scripts.
console.error = () => {};
console.warn = () => {};
