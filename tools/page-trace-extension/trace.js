(() => {
  const traceId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  const sent = (event, data = {}) => {
    try {
      chrome.runtime.sendMessage({ type: 'trace', event, traceId, url: location.href, data });
    } catch { /* extension may be reloaded while the page remains open */ }
  };

  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    const payload = event.data;
    if (!payload || payload.source !== 'vodrip-trace' || !payload.event) return;
    sent(payload.event, payload.data || {});
  });

  const text = (value, max = 120) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, max);
  const descriptor = (el) => {
    if (!(el instanceof Element)) return null;
    const field = el.closest('button, [role="button"], input, textarea, select, [role="slider"], [role="dialog"], a') || el;
    const attrs = {
      tag: field.tagName.toLowerCase(),
      id: field.id || null,
      role: field.getAttribute('role'),
      aria: field.getAttribute('aria-label'),
      name: field.getAttribute('name'),
      type: field.getAttribute('type'),
      automationId: field.getAttribute('data-a-target') || field.getAttribute('data-testid'),
      text: text(field.innerText || field.textContent),
    };
    if (field.matches('[role="slider"]')) {
      attrs.valuetext = field.getAttribute('aria-valuetext');
      attrs.valuenow = field.getAttribute('aria-valuenow');
      attrs.valuemin = field.getAttribute('aria-valuemin');
      attrs.valuemax = field.getAttribute('aria-valuemax');
    }
    return attrs;
  };

  const fieldState = (el) => ({
    ...descriptor(el),
    valueLength: 'value' in el ? String(el.value || '').length : null,
    checked: 'checked' in el ? !!el.checked : null,
    disabled: 'disabled' in el ? !!el.disabled : null,
  });

  sent('page', { readyState: document.readyState, title: text(document.title, 160) });

  document.addEventListener('click', (event) => {
    sent('click', { target: descriptor(event.target), button: event.button });
  }, true);

  document.addEventListener('input', (event) => {
    sent('input', { target: fieldState(event.target), inputType: event.inputType || null });
  }, true);

  document.addEventListener('change', (event) => {
    sent('change', { target: fieldState(event.target) });
  }, true);

  document.addEventListener('focusin', (event) => {
    sent('focus', { target: descriptor(event.target) });
  }, true);

  document.addEventListener('keydown', (event) => {
    if (!['Enter', 'Escape', ' ', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    sent('key', { key: event.key, target: descriptor(event.target) });
  }, true);

  const observed = new WeakMap();
  const snapshot = (el) => {
    if (!(el instanceof Element)) return null;
    const d = descriptor(el);
    if (!d) return null;
    return {
      ...d,
      disabled: 'disabled' in el ? !!el.disabled : null,
      hidden: el.getAttribute('aria-hidden') === 'true' || el.hidden,
      valueLength: 'value' in el ? String(el.value || '').length : null,
    };
  };

  const recordElement = (el, reason) => {
    const next = snapshot(el);
    if (!next) return;
    const prev = observed.get(el);
    const serialized = JSON.stringify(next);
    if (serialized === prev) return;
    observed.set(el, serialized);
    if (el.matches('[role="slider"], button, [role="button"], input, textarea, [role="dialog"]')) {
      sent('dom', { reason, element: next });
    }
  };

  const scan = (root) => {
    if (!(root instanceof Element)) return;
    recordElement(root, 'mutation');
    root.querySelectorAll?.('[role="slider"], button, [role="button"], input, textarea, [role="dialog"]').forEach((el) => recordElement(el, 'mutation'));
  };

  const observer = new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === 'attributes') recordElement(record.target, `attribute:${record.attributeName}`);
      for (const node of record.addedNodes) scan(node);
    }
  });

  const startObserver = () => {
    if (!document.documentElement) return;
    observer.observe(document.documentElement, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ['aria-valuetext', 'aria-valuenow', 'aria-valuemin', 'aria-valuemax', 'disabled', 'aria-hidden', 'data-state'],
    });
    scan(document.body || document.documentElement);
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startObserver, { once: true });
  } else {
    startObserver();
  }
  window.addEventListener('pagehide', () => sent('pagehide'), { once: true });
})();
