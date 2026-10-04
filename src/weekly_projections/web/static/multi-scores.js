(() => {
  const board = document.querySelector('[data-multi-score-board]');
  if (!board) return;
  const leagues = [...document.querySelectorAll('[data-multi-league]')];
  const status = document.querySelector('#multi-score-status');
  const liveCount = document.querySelector('#multi-live-count');
  const empty = document.querySelector('#multi-score-empty');
  const refresh = document.querySelector('#multi-score-refresh');
  const followCount = document.querySelector('#multi-follow-count');
  const storageKey = `wp_multi_score_following_${board.dataset.scoreSeason || 'current'}`;
  const loadFollowed = () => {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || '[]');
      return new Set(Array.isArray(value) ? value.map(String) : []);
    } catch (_) { return new Set(); }
  };
  const followed = loadFollowed();
  let scope = 'mine';
  let loading = false;
  let timer = 0;

  const text = value => document.createTextNode(String(value));
  const element = (tag, className = '', value = '') => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value !== '') item.append(text(value));
    return item;
  };
  const link = (label, href, className = '') => {
    const item = element('a', className, label);
    item.href = href;
    return item;
  };
  const points = value => value == null ? '—' : Number(value).toFixed(2);
  const probability = value => value == null ? 'Win estimate unavailable' : `${Number(value).toFixed(2)}% win`;
  const saveFollowed = () => {
    try { localStorage.setItem(storageKey, JSON.stringify([...followed])); } catch (_) { /* Device storage is optional. */ }
  };
  const syncFollowCard = card => {
    const active = followed.has(card.dataset.followKey);
    card.dataset.followed = active ? 'true' : 'false';
    card.classList.toggle('is-followed', active);
    const button = card.querySelector('[data-follow-game]');
    if (button) {
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
      button.textContent = active ? '★ Following' : '☆ Follow';
    }
  };

  const applyScope = () => {
    let visible = 0;
    document.querySelectorAll('[data-multi-matchup]').forEach(card => {
      syncFollowCard(card);
      const show = scope === 'all'
        || (scope === 'mine' && card.dataset.ownMatchup === 'true')
        || (scope === 'following' && card.dataset.followed === 'true');
      card.hidden = !show;
      if (show) visible += 1;
    });
    leagues.forEach(league => {
      const leagueCards = [...league.querySelectorAll('[data-multi-matchup]')];
      league.hidden = Boolean(leagueCards.length) && !leagueCards.some(card => !card.hidden);
    });
    followCount.textContent = String(document.querySelectorAll('[data-multi-matchup][data-followed="true"]').length);
    empty.textContent = scope === 'following' ? 'Follow a matchup to keep it in this view.' : 'No matchups match this view.';
    empty.hidden = visible !== 0 || leagues.some(league => league.classList.contains('is-loading'));
  };

  const renderWinProbability = matchup => {
    const odds = element('section', 'multi-win-probability');
    odds.append(element('strong', '', 'Win probability'));
    const teams = matchup.teams || [];
    if (teams.length < 2 || teams.some(team => team.win_probability == null)) {
      odds.append(element('span', 'multi-odds-unavailable', 'Estimate unavailable'));
      return odds;
    }
    const left = Number(teams[0].win_probability);
    const labels = element('div', 'multi-odds-labels');
    labels.append(element('span', '', `${teams[0].name} ${left.toFixed(2)}%`), element('span', '', `${teams[1].name} ${Number(teams[1].win_probability).toFixed(2)}%`));
    const track = element('div', 'multi-odds-track');
    track.setAttribute('role', 'img');
    track.setAttribute('aria-label', `${teams[0].name} ${left.toFixed(2)} percent win probability; ${teams[1].name} ${Number(teams[1].win_probability).toFixed(2)} percent.`);
    const fill = element('span'); fill.style.width = `${Math.max(0, Math.min(100, left))}%`; track.append(fill);
    odds.append(labels, track); return odds;
  };

  const renderTeam = team => {
    const panel = element('section', `multi-score-team${team.is_own ? ' is-own-team' : ''}`);
    const header = element('header');
    const identity = element('div');
    identity.append(element('small', '', team.is_own ? 'YOUR TEAM' : 'LEAGUE TEAM'), element('strong', '', team.name));
    const total = element('div', 'multi-team-total');
    total.append(element('strong', '', points(team.score)), element('small', '', probability(team.win_probability)));
    header.append(identity, total); panel.append(header);
    const counts = element('div', 'multi-team-counts');
    counts.append(element('span', 'is-live', `${team.playing} playing`), element('span', 'is-upcoming', `${team.left} left`), element('span', 'is-final', `${team.final} final`));
    panel.append(counts);
    const players = element('ul', 'multi-player-list');
    team.players.forEach(player => {
      const state = String(player.game_state || 'Upcoming').toLowerCase();
      const normalized = ['live', 'upcoming', 'final'].includes(state) ? state : 'upcoming';
      const row = element('li', `is-${normalized}`);
      const copy = element('div');
      copy.append(element('strong', '', player.name), element('small', '', `${player.position || '—'} · ${player.nfl_team || 'FA'} · ${normalized === 'live' ? 'Playing' : normalized === 'final' ? 'Final' : 'Yet to play'}`));
      const score = element('div', 'multi-player-score');
      score.append(element('strong', '', points(player.score)), element('small', '', player.projection == null ? '— proj' : `${Number(player.projection).toFixed(1)} proj`));
      row.append(copy, score); players.append(row);
    });
    if (!team.players.length) players.append(element('li', 'multi-player-empty', 'MFL has not published starters.'));
    panel.append(players); return panel;
  };

  const renderLeague = (container, data) => {
    container.classList.remove('is-loading', 'has-error');
    const state = container.querySelector(':scope > header > span');
    const body = container.querySelector('.multi-score-matchups');
    body.replaceChildren();
    if (data.error) {
      container.classList.add('has-error');
      state.textContent = 'Unavailable';
      body.append(element('p', 'multi-score-error', data.error));
      return;
    }
    const live = data.matchups.filter(matchup => matchup.game_state === 'Live').length;
    state.textContent = `${live} live · Week ${data.week}`;
    data.matchups.forEach(matchup => {
      const card = element('article', `multi-matchup-card state-${String(matchup.game_state).toLowerCase()}`);
      card.dataset.multiMatchup = '';
      card.dataset.ownMatchup = matchup.is_own_matchup ? 'true' : 'false';
      card.dataset.followKey = `${data.league_id}:${data.week}:${matchup.index}`;
      const header = element('header');
      const actions = element('div', 'multi-matchup-actions');
      const follow = element('button', 'multi-follow-game');
      follow.type = 'button'; follow.dataset.followGame = '';
      follow.addEventListener('click', () => {
        if (followed.has(card.dataset.followKey)) followed.delete(card.dataset.followKey);
        else followed.add(card.dataset.followKey);
        saveFollowed(); applyScope();
      });
      actions.append(follow, link('Open full matchup', matchup.href));
      header.append(element('span', 'multi-game-state', matchup.game_state), actions);
      card.append(header);
      card.append(renderWinProbability(matchup));
      const teams = element('div', 'multi-matchup-teams');
      matchup.teams.forEach(team => teams.append(renderTeam(team)));
      card.append(teams); body.append(card); syncFollowCard(card);
    });
    if (!data.matchups.length) body.append(element('p', 'multi-score-error', 'MFL has not published matchups for this week.'));
  };

  const loadAll = async () => {
    if (loading) return;
    loading = true;
    clearTimeout(timer);
    refresh.disabled = true;
    let live = 0;
    let loaded = 0;
    for (const league of leagues) {
      if (document.hidden) break;
      try {
        const response = await fetch(`/api/multi-scores/${encodeURIComponent(league.dataset.multiLeague)}`, {headers:{'Accept':'application/json'}});
        const data = await response.json();
        if (!response.ok && !data.error) data.error = 'This league could not refresh.';
        renderLeague(league, data);
        live += Array.isArray(data.matchups) ? data.matchups.filter(matchup => matchup.game_state === 'Live').length : 0;
      } catch (_) {
        renderLeague(league, {error:'This league could not refresh.'});
      }
      loaded += 1;
      status.textContent = `Loaded ${loaded} of ${leagues.length}`;
      applyScope();
    }
    liveCount.textContent = String(live);
    status.textContent = document.hidden ? 'Refresh paused while this tab is hidden' : `Updated ${new Date().toLocaleTimeString([], {hour:'numeric', minute:'2-digit'})}`;
    loading = false;
    refresh.disabled = false;
    applyScope();
    if (live > 0) timer = window.setTimeout(loadAll, 60000);
  };

  document.querySelectorAll('[data-multi-scope]').forEach(button => button.addEventListener('click', () => {
    scope = button.dataset.multiScope;
    document.querySelectorAll('[data-multi-scope]').forEach(item => {
      const active = item === button;
      item.classList.toggle('active', active);
      item.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    applyScope();
  }));
  refresh.addEventListener('click', loadAll);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !loading) loadAll();
  });
  loadAll();
})();
