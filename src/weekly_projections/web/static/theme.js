(() => {
  const valid = value => ['michigan','lions','aurora','tigers','redwings','pistons'].includes(value) ? value : 'michigan';
  const metaColors = {michigan:'#00274c',lions:'#0076b6',aurora:'#091426',tigers:'#0c2340',redwings:'#ce1126',pistons:'#1d428a'};
  const league = document.querySelector('meta[name="wp-theme-league"]')?.content || '';
  const season = document.querySelector('meta[name="wp-theme-season"]')?.content || '';
  const leagueStorageKey = league && season ? `wp_theme:${season}:${league}` : '';
  const accountTheme = document.querySelector('meta[name="wp-account-theme"]')?.content || '';
  const accountScope = document.querySelector('meta[name="wp-theme-scope"]')?.content === 'league' ? 'league' : 'global';
  let scope = accountTheme ? accountScope : 'global';
  let theme = 'michigan';
  if (accountTheme) theme = valid(accountTheme);
  else {
    try {
      scope = localStorage.getItem('wp_theme_scope') === 'league' && leagueStorageKey ? 'league' : 'global';
      theme = valid(localStorage.getItem(scope === 'league' ? leagueStorageKey : 'wp_theme'));
    } catch (_) {}
  }
  const persist = (value, selectedScope) => {
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
    if (!csrf) return;
    const body = new URLSearchParams({theme: value, scope: selectedScope, league, csrf_token: csrf});
    fetch('/preferences/theme', {
      method: 'POST', body,
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      credentials: 'same-origin', keepalive: true,
    }).catch(() => {});
  };
  const apply = value => {
    theme = valid(value);
    document.documentElement.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = metaColors[theme];
    document.querySelectorAll('[data-theme-picker]').forEach(picker => { picker.value = theme; });
  };
  apply(theme); // In the head: apply before the first painted frame.
  document.addEventListener('DOMContentLoaded', () => {
    const controls = document.createElement('div'); controls.className = 'theme-controls';
    const label = document.createElement('label'); label.className = 'theme-picker';
    const text = document.createElement('span'); text.textContent = 'Theme';
    const select = document.createElement('select'); select.dataset.themePicker = ''; select.setAttribute('aria-label','Website color theme');
    [['michigan','Maize & blue'],['lions','Detroit Lions'],['aurora','Midnight Aurora'],['tigers','Detroit Tigers'],['redwings','Detroit Red Wings'],['pistons','Detroit Pistons']].forEach(([value, title]) => {
      const option = document.createElement('option'); option.value = value; option.textContent = title; select.append(option);
    });
    select.value = theme;
    select.addEventListener('change', () => {
      apply(select.value);
      try {
        localStorage.setItem('wp_theme_scope', scope);
        localStorage.setItem(scope === 'league' && leagueStorageKey ? leagueStorageKey : 'wp_theme', theme);
      } catch (_) {}
      persist(theme, scope);
    });
    label.append(text, select);
    controls.append(label);
    if (leagueStorageKey) {
      const scopeLabel = document.createElement('label'); scopeLabel.className = 'theme-scope-picker';
      const scopeText = document.createElement('span'); scopeText.textContent = 'Apply theme to';
      const scopeSelect = document.createElement('select'); scopeSelect.setAttribute('aria-label','Apply theme to all leagues or this league');
      [['global','All leagues'],['league','This league only']].forEach(([value, title]) => {
        const option = document.createElement('option'); option.value = value; option.textContent = title; scopeSelect.append(option);
      });
      scopeSelect.value = scope;
      scopeSelect.addEventListener('change', () => {
        scope = scopeSelect.value === 'league' ? 'league' : 'global';
        try {
          localStorage.setItem('wp_theme_scope', scope);
          localStorage.setItem(scope === 'league' ? leagueStorageKey : 'wp_theme', theme);
        } catch (_) {}
        persist(theme, scope);
      });
      scopeLabel.append(scopeText, scopeSelect); controls.append(scopeLabel);
    }
    const header = document.querySelector('.topbar');
    if (header) (header.querySelector('[data-theme-slot]') || header).append(controls);
    else { controls.classList.add('standalone-theme-picker'); document.body.prepend(controls); }
    if (!accountTheme) persist(theme, scope);
  });
  window.addEventListener('storage', event => {
    if (event.key === 'wp_theme' || event.key === leagueStorageKey) apply(event.newValue);
  });
})();
