(() => {
  if ('serviceWorker' in navigator) window.addEventListener('load', () => navigator.serviceWorker.register('/service-worker.js'));
  const installButtons = [...document.querySelectorAll('[data-pwa-install]')];
  const iosHelp = [...document.querySelectorAll('[data-ios-install]')];
  let installPrompt = null;
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  if (!standalone && /iphone|ipad|ipod/i.test(navigator.userAgent)) iosHelp.forEach(item => { item.hidden = false; });
  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault(); installPrompt = event;
    installButtons.forEach(button => { button.hidden = false; });
  });
  installButtons.forEach(button => button.addEventListener('click', async () => {
    if (!installPrompt) return;
    await installPrompt.prompt(); installPrompt = null;
    installButtons.forEach(item => { item.hidden = true; });
  }));

  const preference = 'fantasy-hq-briefing-alerts';
  document.querySelectorAll('[data-alert-pref="briefing"]').forEach(box => {
    box.checked = localStorage.getItem(preference) === '1';
    box.addEventListener('change', () => localStorage.setItem(preference, box.checked ? '1' : '0'));
  });
  document.querySelectorAll('[data-enable-notifications]').forEach(button => button.addEventListener('click', async () => {
    if (!('Notification' in window)) { button.textContent = 'Alerts unsupported on this browser'; button.disabled = true; return; }
    const permission = await Notification.requestPermission();
    button.textContent = permission === 'granted' ? 'Device alerts enabled' : 'Device alerts not enabled';
    if (permission === 'granted') localStorage.setItem(preference, '1');
    document.querySelectorAll('[data-alert-pref="briefing"]').forEach(box => { box.checked = permission === 'granted'; });
  }));

  const alertCount = Number(document.body.dataset.insightAlertCount || 0);
  if ('setAppBadge' in navigator) alertCount ? navigator.setAppBadge(alertCount) : navigator.clearAppBadge();
  if (alertCount && localStorage.getItem(preference) === '1' && window.Notification?.permission === 'granted') {
    const signature = `${location.pathname}:${location.search}:${alertCount}`;
    if (sessionStorage.getItem('fantasy-hq-last-alert') !== signature) {
      navigator.serviceWorker?.ready.then(registration => registration.showNotification('Fantasy HQ briefing', {
        body: `${alertCount} roster item${alertCount === 1 ? '' : 's'} need your attention.`,
        icon: '/static/app-icon-192.png', badge: '/static/app-icon-192.png', tag: 'fantasy-hq-briefing',
      }));
      sessionStorage.setItem('fantasy-hq-last-alert', signature);
    }
  }
})();
