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
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const leagueId = document.querySelector('#header-league')?.value || '';
  const statusItems = [...document.querySelectorAll('[data-push-status]')];
  const testButtons = [...document.querySelectorAll('[data-test-push]')];
  const disableButtons = [...document.querySelectorAll('[data-disable-notifications]')];
  const setStatus = message => statusItems.forEach(item => { item.textContent = message; });
  const decodeKey = value => {
    const padding = '='.repeat((4 - value.length % 4) % 4);
    const raw = atob((value + padding).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from([...raw].map(character => character.charCodeAt(0)));
  };
  const postSubscription = async (path, subscription) => {
    const value = subscription.toJSON();
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':csrf}, body:JSON.stringify({endpoint:value.endpoint, keys:value.keys, expiration_time:value.expirationTime, league_id:leagueId})});
    if (!response.ok) { const problem = await response.json().catch(() => ({})); throw new Error(problem.detail || 'Background alerts could not be updated.'); }
  };
  const refreshPushState = async () => {
    if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) return;
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.getSubscription();
    testButtons.forEach(button => { button.hidden = !subscription; });
    disableButtons.forEach(button => { button.hidden = !subscription; });
    if (subscription) setStatus('Background alerts are active on this device.');
  };
  document.querySelectorAll('[data-enable-notifications]').forEach(button => button.addEventListener('click', async () => {
    try {
      if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) throw new Error('Background alerts are unsupported on this browser.');
      const configResponse = await fetch('/api/push/config', {headers:{'Accept':'application/json'}});
      const config = await configResponse.json();
      if (!config.available || !config.public_key) throw new Error('The server still needs its Web Push keys.');
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') throw new Error('Notification permission was not granted.');
      const registration = await navigator.serviceWorker.ready;
      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) subscription = await registration.pushManager.subscribe({userVisibleOnly:true, applicationServerKey:decodeKey(config.public_key)});
      await postSubscription('/api/push/subscribe', subscription);
      localStorage.setItem(preference, '1');
      document.querySelectorAll('[data-alert-pref="briefing"]').forEach(box => { box.checked = true; });
      button.textContent = 'Background alerts enabled';
      await refreshPushState();
    } catch (error) { setStatus(error.message); }
  }));
  testButtons.forEach(button => button.addEventListener('click', async () => {
    try { const response = await fetch('/api/push/test', {method:'POST',headers:{'X-CSRF-Token':csrf}}); if (!response.ok) { const problem = await response.json().catch(() => ({})); throw new Error(problem.detail || 'Test failed.'); } setStatus('Test alert sent.'); }
    catch (error) { setStatus(error.message); }
  }));
  disableButtons.forEach(button => button.addEventListener('click', async () => {
    try { const registration = await navigator.serviceWorker.ready; const subscription = await registration.pushManager.getSubscription(); if (subscription) { await postSubscription('/api/push/unsubscribe', subscription); await subscription.unsubscribe(); } localStorage.setItem(preference,'0'); setStatus('Background alerts are disabled on this device.'); await refreshPushState(); }
    catch (error) { setStatus(error.message); }
  }));
  refreshPushState().catch(() => {});

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
