(() => {
  'use strict';
  const preferenceKey = 'udm-theme';
  const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
  let preference = null;
  try {
    const stored = localStorage.getItem(preferenceKey);
    if (stored === 'dark' || stored === 'light') preference = stored;
  } catch (_) { /* The switch still works when browser storage is unavailable. */ }

  function applyTheme() {
    const dark = preference === 'dark' || (preference === null && systemTheme.matches);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    const toggle = document.getElementById('theme-toggle');
    if (toggle) toggle.setAttribute('aria-pressed', String(dark));
  }

  // Apply before the stylesheet paints, including on the login screen.
  applyTheme();
  systemTheme.addEventListener('change', applyTheme);
  document.addEventListener('DOMContentLoaded', () => {
    applyTheme();
    document.getElementById('theme-toggle').addEventListener('click', () => {
      preference = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem(preferenceKey, preference); } catch (_) { /* Optional persistence. */ }
      applyTheme();
    });
  });
})();
