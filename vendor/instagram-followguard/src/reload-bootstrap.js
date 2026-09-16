// Dev-only bootstrap: reload-followguard.ps1 opens this page in the running
// Chrome so the extension can reload itself (chrome.runtime.reload() is only
// callable from the extension's own contexts; inline scripts are CSP-blocked
// in extension pages, hence the external file). On failure the title becomes
// "reload-failed", which the ps1 polls for.
'use strict';
setTimeout(() => {
  try {
    chrome.runtime.reload();
  } catch (err) {
    document.title = 'reload-failed';
    document.getElementById('status').textContent = 'Falha ao recarregar: ' + (err && err.message);
  }
}, 200);
