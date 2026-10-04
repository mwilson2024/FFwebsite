(() => {
  const cards = [...document.querySelectorAll('[data-command-league]')];
  const boardData = new Map();
  let coreLoaded = 0;
  let queueLoaded = 0;
  const text = value => document.createTextNode(String(value));
  const element = (tag, className = '', value = '') => {
    const item = document.createElement(tag); if (className) item.className = className;
    if (value !== '') item.append(text(value)); return item;
  };
  const link = (label, href, className = '') => { const item = element('a', className, label); item.href = href; return item; };
  const formatPoints = value => value == null ? '—' : Number(value).toFixed(2);
  const renderLiveBoxscore = data => {
    if (data.game_state !== 'Live' || !Array.isArray(data.live_teams) || !data.live_teams.length) return null;
    const section = element('section', 'command-live-boxscore');
    const heading = element('div', 'command-section-title');
    heading.append(element('strong', '', 'Live starters'), link('Full box score', data.links.scores));
    section.append(heading);
    const teams = element('div', 'command-live-teams');
    data.live_teams.forEach(team => {
      const panel = element('article', `command-live-team${team.is_own ? ' is-own-team' : ''}`);
      const header = element('header');
      const identity = element('div');
      identity.append(element('small', '', team.is_own ? 'YOUR TEAM' : 'OPPONENT'), element('strong', '', team.name));
      const total = element('div', 'command-live-total');
      total.append(element('strong', '', formatPoints(team.score)), element('small', '', team.projected_score == null ? 'Projection unavailable' : `${formatPoints(team.projected_score)} projected`));
      header.append(identity, total); panel.append(header);
      const counts = element('div', 'command-live-counts');
      counts.append(element('span', 'is-live', `${team.playing} playing`), element('span', 'is-upcoming', `${team.left} left`), element('span', 'is-final', `${team.final} final`));
      panel.append(counts);
      const players = element('ul', 'command-live-players');
      team.players.forEach(player => {
        const state = String(player.game_state || 'Upcoming').toLowerCase();
        const row = element('li', `is-${['live','upcoming','final'].includes(state) ? state : 'upcoming'}`);
        const copy = element('div');
        copy.append(element('strong', '', player.name), element('small', '', `${player.position || '—'} · ${player.nfl_team || 'FA'} · ${state === 'live' ? 'Playing' : state === 'final' ? 'Final' : 'Yet to play'}`));
        const points = element('div', 'command-live-player-points');
        points.append(element('strong', '', formatPoints(player.score)), element('small', '', player.projection == null ? '— proj' : `${Number(player.projection).toFixed(1)} proj`));
        row.append(copy, points); players.append(row);
      });
      if (!team.players.length) players.append(element('li', 'command-live-empty', 'MFL has not published this team’s starters.'));
      panel.append(players); teams.append(panel);
    });
    section.append(teams); return section;
  };
  const updateSummary = () => {
    const values = [...boardData.values()].filter(item => !item.error);
    const actionCount = values.reduce((sum, item) => sum + Number(item.alert_count || 0), 0);
    const liveCount = values.filter(item => item.game_state === 'Live').length;
    const pending = values.reduce((sum, item) => sum + Number(item.queue?.pending_total || 0), 0);
    const edge = values.reduce((sum, item) => sum + Math.max(0, Number(item.lineup?.projected_gain || 0)), 0);
    document.querySelector('#command-action-count').textContent = String(actionCount);
    document.querySelector('#command-live-count').textContent = String(liveCount);
    document.querySelector('#command-pending-count').textContent = queueLoaded ? String(pending) : '…';
    document.querySelector('#command-edge-total').textContent = `${edge.toFixed(2)} pts`;
    const progress = document.querySelector('#command-load-progress');
    if (progress) progress.textContent = coreLoaded < cards.length ? `Loading ${coreLoaded} of ${cards.length}` : queueLoaded < cards.length ? `Checking queues ${queueLoaded} of ${cards.length}` : 'Board is up to date';
  };
  const applyFilter = (filter = document.querySelector('[data-command-filter].active')?.dataset.commandFilter || 'all') => {
    let visible = 0;
    cards.forEach(card => {
      const matches = filter === 'all' || (filter === 'action' && card.dataset.commandCategory === 'action') || (filter === 'live' && card.dataset.live === 'true') || (filter === 'ready' && card.dataset.commandCategory === 'ready');
      card.hidden = !matches; if (matches) visible += 1;
    });
    const empty = document.querySelector('#command-filter-empty'); if (empty) empty.hidden = visible !== 0;
  };
  document.querySelectorAll('[data-command-filter]').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('[data-command-filter]').forEach(item => { item.classList.toggle('active', item === button); item.setAttribute('aria-pressed', item === button ? 'true' : 'false'); });
    applyFilter(button.dataset.commandFilter);
  }));
  const render = (card, data) => {
    card.classList.remove('loading');
    const state = card.querySelector('.load-state');
    if (data.error) {
      card.classList.add('failed'); card.dataset.commandCategory = 'action'; state.textContent = 'Unavailable';
      const failure = element('div', 'command-load-error'); failure.append(element('strong', '', 'League unavailable'), element('p', '', data.error), link('Open league home', `/home?league=${encodeURIComponent(data.league_id || card.dataset.commandLeague)}`));
      card.querySelector('.command-skeleton')?.replaceWith(failure); return;
    }
    boardData.set(data.league_id, data);
    card.style.order = String(-Number(data.priority_rank || 0));
    card.dataset.commandCategory = Number(data.priority_rank) >= 3 ? 'action' : 'ready';
    card.dataset.live = data.game_state === 'Live' ? 'true' : 'false';
    card.classList.add(`priority-${String(data.priority || 'ready').toLowerCase().replace(/\s+/g, '-')}`);
    state.className = `load-state priority-pill priority-${String(data.priority || 'ready').toLowerCase().replace(/\s+/g, '-')}`;
    state.textContent = data.priority;
    const body = document.createElement('div'); body.className = 'command-body';
    const meta = element('div', 'command-meta'); meta.append(element('span', 'game-state', `${data.game_state} · Week ${data.week}`));
    if (data.standing?.rank) meta.append(link(`#${data.standing.rank} of ${data.standing.teams} · ${data.standing.record || 'Record unavailable'} · PF ${data.standing.points_for ?? '—'}`, data.links.standings));
    else meta.append(element('span', '', data.standing?.unavailable ? 'Standings temporarily unavailable' : 'Standing unavailable'));
    body.append(meta);

    const matchup = element('section', 'command-score');
    const scoreHead = element('div', 'command-score-head'); scoreHead.append(element('span', '', data.team || 'Your team'), element('span', '', data.opponent));
    const scoreLine = element('div', 'command-score-line'); scoreLine.append(element('strong', '', formatPoints(data.score)), element('span', '', 'vs'), element('strong', '', formatPoints(data.opponent_score)));
    const projection = element('p', 'command-projected', data.projected_score == null || data.opponent_projected_score == null ? 'Projected finish unavailable' : `Projected finish ${formatPoints(data.projected_score)} – ${formatPoints(data.opponent_projected_score)}`);
    const chanceLabel = data.win_probability == null ? 'Win estimate unavailable' : `${Number(data.win_probability).toFixed(2)}% estimated win`;
    const chance = element('div', 'command-chance'); chance.append(element('span', '', chanceLabel));
    if (data.win_probability != null) { const track = element('i'); const fill = element('b'); fill.style.width = `${Math.max(0, Math.min(100, Number(data.win_probability)))}%`; track.append(fill); chance.append(track); }
    const gameCounts = element('div', 'command-game-counts'); [['playing', data.playing], ['left', data.left], ['final', data.final]].forEach(([label,value]) => gameCounts.append(element('span', '', `${value} ${label}`)));
    matchup.append(scoreHead, scoreLine, projection, chance, gameCounts); body.append(matchup);
    const liveBoxscore = renderLiveBoxscore(data); if (liveBoxscore) body.append(liveBoxscore);

    const lineup = element('section', 'command-lineup');
    const lineupHead = element('div', 'command-section-title'); lineupHead.append(element('strong', '', 'Lineup health'), link('Manage', data.links.lineup));
    const lineupStatus = data.lineup.open_slots ? `${data.lineup.open_slots} open starter slot${data.lineup.open_slots === 1 ? '' : 's'}` : `${data.lineup.starters}/${data.lineup.required} starters filled`;
    lineup.append(lineupHead, element('p', data.lineup.open_slots ? 'lineup-danger' : 'lineup-ready', lineupStatus));
    const lineupMetrics = element('div', 'command-mini-metrics');
    lineupMetrics.append(element('span', '', `Current ${formatPoints(data.lineup.current_projection)}`), element('span', '', `Best ${formatPoints(data.lineup.recommended_projection)}`), element('span', '', `${data.lineup.changes} suggested change${data.lineup.changes === 1 ? '' : 's'}`), element('strong', '', data.lineup.projected_gain > .05 ? `+${formatPoints(data.lineup.projected_gain)} edge` : 'Optimized'));
    lineup.append(lineupMetrics); body.append(lineup);

    const queue = element('section', 'command-queue'); queue.dataset.commandQueue = '';
    const queueHead = element('div', 'command-section-title'); queueHead.append(element('strong', '', 'Transaction queue'), link('Review all', data.links.transactions));
    queue.append(queueHead, element('p', 'queue-loading', 'Checking MFL waivers and trades…'));
    const local = element('div', 'command-queue-chips'); local.append(link(`${data.local_reviews} local review${data.local_reviews === 1 ? '' : 's'}`, data.links.transactions), link(`${data.watchlist_count} watched`, data.links.watchlist)); queue.append(local); body.append(queue);

    const alerts = element('section', 'command-alerts');
    const alertHead = element('div', 'command-section-title'); alertHead.append(element('strong', '', 'Action feed'), link('All alerts', data.links.insights)); alerts.append(alertHead);
    if (data.alerts.length) {
      const list = document.createElement('ul');
      data.alerts.slice(0, 4).forEach(alert => { const row = element('li', `tone-${alert.tone}`); const copy = element('div'); copy.append(element('strong', '', alert.title), element('small', '', alert.detail)); row.append(copy, link(alert.label || 'Open', alert.href)); list.append(row); }); alerts.append(list);
    } else alerts.append(element('p', 'all-clear', 'No urgent lineup or injury alert.'));
    if (data.recent_issue) alerts.append(element('p', `command-operation ${data.recent_issue.status}`, `${data.recent_issue.title}: ${data.recent_issue.detail}`));
    body.append(alerts);

    const actions = document.createElement('nav'); actions.setAttribute('aria-label', `${data.name} quick actions`);
    [['Matchup','scores'],['Lineup','lineup'],['Waivers','moves'],['Trades','trades'],['League home','home']].forEach(([label,key], index) => actions.append(link(label, data.links[key], index === 0 ? 'primary-action' : ''))); body.append(actions);
    const footer = element('footer', 'command-card-footer'); footer.append(element('span', '', `Updated ${new Date(data.updated_at * 1000).toLocaleTimeString([], {hour:'numeric', minute:'2-digit'})}`), element('span', '', 'Queue loads next'));
    body.append(footer);
    card.querySelector('.command-skeleton').replaceWith(body);
    applyFilter(); updateSummary();
  };
  const renderQueue = (card, queueData) => {
    const data = boardData.get(queueData.league_id); if (!data) return;
    data.queue = queueData; boardData.set(queueData.league_id, data);
    const queue = card.querySelector('[data-command-queue]'); if (!queue) return;
    queue.querySelector('.queue-loading')?.remove();
    const chips = element('div', 'command-queue-chips command-queue-provider');
    chips.append(link(`${queueData.waiver_claims} waiver claim${queueData.waiver_claims === 1 ? '' : 's'}`, data.links.transactions));
    chips.append(link(`${queueData.incoming_trades} trade${queueData.incoming_trades === 1 ? '' : 's'} to answer`, data.links.transactions, queueData.incoming_trades ? 'queue-urgent' : ''));
    chips.append(link(`${queueData.outgoing_trades} sent`, data.links.transactions)); queue.append(chips);
    if (queueData.errors?.length) queue.append(element('small', 'queue-warning', `Could not refresh: ${queueData.errors.join(' and ')}. Other data is still current.`));
    card.querySelector('.command-card-footer span:last-child').textContent = 'Queue checked'; updateSummary();
  };
  (async () => {
    for (const card of cards) {
      try {
        const response = await fetch(`/api/command-center/${encodeURIComponent(card.dataset.commandLeague)}`, {headers:{'Accept':'application/json'}});
        const data = await response.json(); render(card, data);
      } catch (_) { render(card, {league_id:card.dataset.commandLeague, error:'This league could not load.'}); }
      coreLoaded += 1; updateSummary();
    }
    for (const card of cards) {
      if (!boardData.has(card.dataset.commandLeague)) { queueLoaded += 1; updateSummary(); continue; }
      try {
        const response = await fetch(`/api/command-center/${encodeURIComponent(card.dataset.commandLeague)}/queue`, {headers:{'Accept':'application/json'}});
        if (!response.ok) throw new Error('Queue unavailable'); renderQueue(card, await response.json());
      } catch (_) {
        const queue = card.querySelector('[data-command-queue]'); queue?.querySelector('.queue-loading')?.replaceWith(element('p', 'queue-warning', 'Pending transactions could not refresh. Open Review all to retry.'));
      }
      queueLoaded += 1; updateSummary();
    }
  })();
})();
