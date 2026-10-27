(() => {
  const main = document.querySelector('main');
  if (main) { main.id = 'main-content'; main.tabIndex = -1; }
  const options = document.querySelector('.site-options');
  const login = document.querySelector('[data-mfl-login]');
  const updateLoginMethod = () => {
    if (!login) return;
    const selected = login.querySelector('input[name="login_method"]:checked')?.value || 'password';
    login.querySelectorAll('[data-login-fields]').forEach(group => {
      const enabled = group.dataset.loginFields === selected;
      group.hidden = !enabled;
      group.querySelectorAll('input').forEach(input => {
        input.disabled = !enabled;
        input.required = enabled;
      });
    });
  };
  login?.addEventListener('change', event => {
    if (event.target.matches('input[name="login_method"]')) updateLoginMethod();
  });
  updateLoginMethod();
  const globalPlayerSearch = document.querySelector('#global-player-search');
  const globalPlayerOptions = document.querySelector('[data-player-search-options]');
  const globalPlayerForm = globalPlayerSearch?.closest('form');
  let playerSearchTimer = 0;
  let playerSearchController = null;
  const loadPlayerSuggestions = () => {
    const query = globalPlayerSearch?.value.trim() || '';
    if (!globalPlayerOptions || query.length < 2) {
      globalPlayerOptions?.replaceChildren();
      return;
    }
    playerSearchController?.abort();
    playerSearchController = new AbortController();
    const league = globalPlayerForm?.querySelector('input[name="league"]')?.value || '';
    fetch(`/api/player-search?${new URLSearchParams({league, q:query})}`, {
      credentials:'same-origin', signal:playerSearchController.signal,
    }).then(response => {
      if (!response.ok) throw new Error();
      return response.json();
    }).then(payload => {
      globalPlayerOptions.replaceChildren();
      (payload.suggestions || []).forEach(player => {
        const option = document.createElement('option');
        option.value = player.name;
        option.label = `${player.position || '—'} · ${player.team || 'FA'}`;
        globalPlayerOptions.append(option);
      });
    }).catch(error => {
      if (error.name !== 'AbortError') globalPlayerOptions.replaceChildren();
    });
  };
  globalPlayerSearch?.addEventListener('input', () => {
    window.clearTimeout(playerSearchTimer);
    playerSearchTimer = window.setTimeout(loadPlayerSuggestions, 160);
  });
  globalPlayerSearch?.addEventListener('focus', loadPlayerSuggestions);
  const equalScrollCards = document.querySelector('[data-equal-scroll-cards]');
  const scrollHeightSource = equalScrollCards?.querySelector('[data-scroll-height-source]');
  const scrollHeightTarget = equalScrollCards?.querySelector('[data-scroll-height-target]');
  const syncActivityHeight = () => {
    if (!scrollHeightSource || !scrollHeightTarget) return;
    if (window.matchMedia('(max-width: 900px)').matches) {
      scrollHeightTarget.style.removeProperty('--activity-panel-height');
    } else {
      scrollHeightTarget.style.setProperty(
        '--activity-panel-height',
        `${Math.ceil(scrollHeightSource.getBoundingClientRect().height)}px`,
      );
    }
    scrollHeightTarget.classList.add('is-height-synced');
  };
  if (scrollHeightSource && scrollHeightTarget) {
    syncActivityHeight();
    new ResizeObserver(syncActivityHeight).observe(scrollHeightSource);
    window.addEventListener('resize', syncActivityHeight, { passive: true });
  }
  // Matchup indexes belong to a specific week; return to your game on week change.
  document.querySelector('#score-week')?.addEventListener('change', () => {
    const matchup = document.querySelector('#matchup-picker');
    if (matchup) matchup.value = '';
  });
  document.addEventListener('click', event => {
    if (options?.open && !options.contains(event.target)) options.open = false;
    document.querySelectorAll('.player-actions[open]').forEach(menu => {
      if (!menu.contains(event.target)) menu.open = false;
    });
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    const active = document.activeElement;
    document.querySelectorAll('.site-options[open],.player-actions[open]').forEach(menu => {
      if (menu.contains(active)) menu.querySelector('summary')?.focus();
      menu.open = false;
    });
  });
})();
