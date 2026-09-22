(() => {
  const weekDialog = document.querySelector('#current-week-preference');
  let hideWeekNotice = false;
  try { hideWeekNotice = localStorage.getItem('wp_hide_current_week_notice') === '1'; } catch (_) { /* Preference storage may be disabled. */ }
  if (weekDialog?.dataset.showNotice === 'true' && !hideWeekNotice) {
    weekDialog.showModal();
  }
  weekDialog?.addEventListener('close', () => {
    if (weekDialog.querySelector('[data-hide-week-notice]')?.checked) {
      try { localStorage.setItem('wp_hide_current_week_notice', '1'); } catch (_) { /* The dialog can still close normally. */ }
    }
  });
  weekDialog?.addEventListener('click', event => {
    if (event.target === weekDialog) weekDialog.close();
  });

  const panels = [...document.querySelectorAll('[data-hub-url]')];
  const load = async panel => {
    if (panel.dataset.loading === 'true') return;
    panel.dataset.loading = 'true'; panel.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(panel.dataset.hubUrl, {signal: AbortSignal.timeout(90000)});
      if (!response.ok) throw new Error(response.status === 401 ? 'Your session expired. Reconnect to MFL.' : 'This section is unavailable. Try again.');
      // Same-origin server template; every feed value is auto-escaped by Jinja.
      panel.innerHTML = await response.text();
    } catch (error) {
      const message = document.createElement('p'); message.textContent = error.name === 'TimeoutError' ? 'MFL is taking longer than expected. Try again.' : error.message;
      const button = document.createElement('button'); button.type = 'button'; button.dataset.hubRetry = ''; button.textContent = 'Retry';
      panel.replaceChildren(message, button);
    } finally { panel.dataset.loading = 'false'; panel.removeAttribute('aria-busy'); }
  };
  const queue = [];
  let workers = 0;
  const pump = () => {
    while (workers < 2 && queue.length) {
      workers += 1;
      load(queue.shift()).finally(() => { workers -= 1; pump(); });
    }
  };
  const enqueue = panel => {
    if (!panel || panel.dataset.queued === 'true') return;
    panel.dataset.queued = 'true'; queue.push(panel); pump();
  };
  panels.filter(panel => panel.hasAttribute('data-hub-priority')).forEach(enqueue);
  const deferred = panels.filter(panel => !panel.hasAttribute('data-hub-priority'));
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(entries => {
      entries.filter(entry => entry.isIntersecting).forEach(entry => {
        observer.unobserve(entry.target); enqueue(entry.target);
      });
    }, {rootMargin: '180px 0px'});
    deferred.forEach(panel => observer.observe(panel));
  } else {
    deferred.forEach(enqueue);
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-hub-retry]');
    if (button) load(button.closest('[data-hub-url]'));
  });
})();
