(() => {
  const leaguePicker = document.querySelector('.header-league-picker');
  if (leaguePicker) {
    leaguePicker.querySelector('select').addEventListener('change', () => leaguePicker.requestSubmit());
    leaguePicker.querySelector('button').hidden = true;
  }

  const syncPlayerAutocomplete = (input) => {
    const target = document.getElementById(input.dataset.playerAutocomplete || '');
    const list = document.getElementById(input.getAttribute('list') || '');
    if (!target || !list) return;
    const entered = input.value.trim().toLocaleLowerCase();
    const option = [...list.options].find(item => item.value.trim().toLocaleLowerCase() === entered);
    target.value = option?.dataset.playerId || '';
    input.setCustomValidity(input.value && !target.value ? 'Choose a player from the suggestions.' : '');
    input.form?.dispatchEvent(new CustomEvent('player-autocomplete-change'));
  };
  document.querySelectorAll('[data-player-autocomplete]').forEach(input => {
    input.addEventListener('input', () => syncPlayerAutocomplete(input));
    input.addEventListener('change', () => syncPlayerAutocomplete(input));
    syncPlayerAutocomplete(input);
  });
  document.querySelectorAll('[data-autocomplete-form]').forEach(form => {
    const submit = form.querySelector('[data-autocomplete-submit]');
    const requiredTargets = () => [...form.querySelectorAll('[data-autocomplete-required]')];
    const update = () => { if (submit) submit.disabled = requiredTargets().some(input => !input.value); };
    form.addEventListener('player-autocomplete-change', update);
    form.addEventListener('submit', event => {
      requiredTargets().forEach(input => {
        if (!input.value) event.preventDefault();
      });
    });
    update();
  });

  const rosterSearch = document.querySelector('#roster-player-search');
  if (rosterSearch) {
    const rosterRows = [...document.querySelectorAll('[data-roster-player]')];
    const resultCount = document.querySelector('#roster-player-result-count');
    const empty = document.querySelector('#roster-player-empty');
    const filterRoster = () => {
      const words = rosterSearch.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
      let visible = 0;
      rosterRows.forEach(row => {
        const haystack = (row.dataset.search || '').toLocaleLowerCase();
        row.hidden = !words.every(word => haystack.includes(word));
        if (!row.hidden) visible += 1;
      });
      if (resultCount) resultCount.textContent = String(visible);
      if (empty) empty.hidden = visible !== 0;
    };
    rosterSearch.addEventListener('input', filterRoster);
    rosterSearch.addEventListener('change', filterRoster);
  }

  // Keep automatic stat reads bounded, including when several players finish.
  const statQueue = [];
  let activeStatReads = 0;
  const queueStatRead = (read) => new Promise((resolve, reject) => {
    statQueue.push({read, resolve, reject});
    const drain = () => {
      while (activeStatReads < 2 && statQueue.length) {
        const next = statQueue.shift(); activeStatReads += 1;
        next.read().then(next.resolve, next.reject).finally(() => { activeStatReads -= 1; drain(); });
      }
    };
    drain();
  });
  const pointsCard = document.querySelector('#points-card');
  let activePointsPanel = null;
  pointsCard?.addEventListener('click', event => {
    if (event.target === pointsCard) {
      const rect = pointsCard.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) pointsCard.close();
    }
  });
  document.querySelectorAll('[data-scoring-player]').forEach((panel) => {
    let loading = false;
    let loadedAt = 0;
    const statLine = document.getElementById(panel.dataset.statLine || '');
    const content = document.createElement('div');
    const syncCard = () => {
      if (activePointsPanel === panel && pointsCard?.open) {
        pointsCard.querySelector('#points-card-content').replaceChildren(...[...content.childNodes].map(node => node.cloneNode(true)));
      }
    };
    const loadStats = async () => {
      if (loading || Date.now() - loadedAt < 60000) return;
      loading = true;
      content.textContent = 'Loading stats…';
      syncCard();
      try {
        const query = new URLSearchParams({league:panel.dataset.league, week:panel.dataset.week, franchise:panel.dataset.franchise});
        const response = await queueStatRead(() => fetch(`/api/scoring/${encodeURIComponent(panel.dataset.scoringPlayer)}?${query}`, {signal:AbortSignal.timeout(25000)}));
        const data = await response.json();
        if (!response.ok) throw new Error(response.status === 409 ? data.detail : 'Scoring details are unavailable. Close and reopen to retry.');
        content.replaceChildren();
        const statsContainer = statLine || content;
        if (statLine) statLine.replaceChildren();
        const heading = panel.closest('.player-scoring-panel')?.querySelector('.stat-line-heading > strong');
        if (heading) heading.textContent = data.state === 'Final' ? 'Final stat line' : 'Stat line';
        (data.stat_lines || []).forEach((line) => {
          const text = document.createElement('p'); text.className = 'stat-line'; text.textContent = line; statsContainer.append(text);
        });
        if (!data.stat_lines?.length) {
          const empty = document.createElement('p');
          empty.textContent = data.state === 'Upcoming' ? 'Stats will appear after kickoff.' : 'Detailed stats unavailable. Official points are shown below.';
          statsContainer.append(empty);
        }
        const list = document.createElement('dl');
        const addRow = (label, points, strong = false) => {
          const row = document.createElement('div'); const term = document.createElement('dt'); const value = document.createElement('dd');
          term.textContent = label; value.textContent = Number(points).toFixed(2);
          if (strong) row.className = 'breakdown-total';
          row.append(term, value); list.append(row);
        };
        (data.components || []).forEach((entry) => addRow(`${entry.label} · ${entry.stat}`, entry.points));
        if (data.difference) addRow('Unallocated / feed difference*', data.difference);
        addRow('Official MFL points', data.official_points, true);
        content.append(list);
        const note = document.createElement('p'); note.className = 'stat-note'; note.textContent = data.note;
        if (data.difference) note.textContent += ' *Includes unsupported scoring events, special rules, or differences between feeds; this is not a verified scoring adjustment.';
        const source = document.createElement('small'); source.textContent = data.source;
        content.append(note, source);
        loadedAt = Date.now();
      } catch (error) {
        content.textContent = error.name === 'TimeoutError' ? 'Stats took too long. Close and reopen to retry.' : error.message;
        if (statLine) statLine.textContent = 'Stats unavailable. Open Points breakdown to retry.';
      } finally { loading = false; syncCard(); }
    };
    panel.addEventListener('click', () => {
      if (!pointsCard) return;
      activePointsPanel = panel;
      pointsCard.querySelector('#points-card-title').textContent = `${panel.dataset.playerName} · Points breakdown`;
      if (!content.childNodes.length) content.textContent = `Official MFL points: ${Number(panel.dataset.officialPoints).toFixed(2)}. Loading breakdown…`;
      if (!pointsCard.open) pointsCard.showModal();
      syncCard(); loadStats();
    });
    if (statLine && panel.dataset.gameState !== 'upcoming') loadStats();
  });
  const touchdownCard = document.querySelector('#touchdown-card');
  let activeTouchdownButton = null;
  touchdownCard?.addEventListener('click', event => {
    if (event.target === touchdownCard) {
      const rect = touchdownCard.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) touchdownCard.close();
    }
  });
  document.querySelectorAll('[data-touchdown-player]').forEach(button => {
    let loadedAt = 0;
    let loading = false;
    let cachedContent = null;
    const render = (data) => {
      const content = document.createElement('div');
      const plays = Array.isArray(data.plays) ? data.plays : [];
      if (!plays.length) {
        const empty = document.createElement('p');
        empty.className = 'touchdown-empty';
        empty.textContent = data.note || 'No touchdown was found for this player and week.';
        content.append(empty);
        return content;
      }
      plays.forEach((play, index) => {
        const article = document.createElement('article');
        article.className = 'touchdown-card-item';
        if (play.thumbnail_url) {
          const image = document.createElement('img');
          image.src = play.thumbnail_url;
          image.alt = '';
          image.loading = 'lazy';
          image.addEventListener('error', () => image.remove());
          article.append(image);
        }
        const body = document.createElement('div');
        const meta = document.createElement('span');
        meta.className = 'touchdown-card-meta';
        const quarter = Number(play.period) > 0 ? `Q${Number(play.period)}` : 'Scoring play';
        meta.textContent = `${quarter}${play.clock ? ` · ${play.clock}` : ''}${play.team ? ` · ${play.team}` : ''}`;
        const title = document.createElement('h3');
        title.textContent = play.title || `Touchdown ${index + 1}`;
        const description = document.createElement('p');
        description.textContent = play.description || '';
        const link = document.createElement('a');
        link.href = play.clip_url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = play.direct_clip ? 'Watch this touchdown on ESPN ↗' : 'Open ESPN game highlights ↗';
        body.append(meta, title, description, link);
        if (!play.direct_clip) {
          const fallback = document.createElement('small');
          fallback.textContent = 'ESPN has not published a direct clip link for this play.';
          body.append(fallback);
        }
        article.append(body);
        content.append(article);
      });
      const note = document.createElement('p');
      note.className = 'touchdown-source-note';
      note.textContent = data.note || 'Highlight availability is supplied by ESPN.';
      content.append(note);
      return content;
    };
    button.addEventListener('click', async () => {
      if (!touchdownCard) return;
      activeTouchdownButton = button;
      touchdownCard.querySelector('#touchdown-card-title').textContent = `${button.dataset.playerName} · Touchdowns`;
      const host = touchdownCard.querySelector('#touchdown-card-content');
      host.replaceChildren(cachedContent ? cachedContent.cloneNode(true) : document.createTextNode('Finding ESPN scoring plays…'));
      if (!touchdownCard.open) touchdownCard.showModal();
      if (loading || (cachedContent && Date.now() - loadedAt < 60000)) return;
      loading = true;
      try {
        const query = new URLSearchParams({league:button.dataset.league, week:button.dataset.week});
        const response = await fetch(`/api/touchdowns/${encodeURIComponent(button.dataset.touchdownPlayer)}?${query}`, {signal:AbortSignal.timeout(15000)});
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || 'Touchdown highlights are unavailable.');
        cachedContent = render(data);
        loadedAt = Date.now();
        if (activeTouchdownButton === button && touchdownCard.open) host.replaceChildren(cachedContent.cloneNode(true));
      } catch (error) {
        if (activeTouchdownButton === button) host.textContent = error.name === 'TimeoutError' ? 'ESPN took too long to respond. Close and reopen to retry.' : error.message;
      } finally {
        loading = false;
      }
    });
  });
  document.querySelectorAll('[data-kickoff]').forEach((time) => {
    time.textContent = new Intl.DateTimeFormat(undefined, {weekday:'short', hour:'numeric', minute:'2-digit', timeZoneName:'short'}).format(new Date(Number(time.dataset.kickoff) * 1000));
  });
  document.querySelectorAll('.player-headshot').forEach((image) => {
    image.addEventListener('error', () => { image.hidden = true; });
    if (image.complete && !image.naturalWidth) image.hidden = true;
  });
  const card = document.querySelector('#player-card');
  const watchButton = card?.querySelector('#card-watch');
  const compareLink = card?.querySelector('#card-compare');
  let cardRequest = 0;
  document.querySelectorAll('[data-player-card]').forEach((button) => button.addEventListener('click', async () => {
    if (!card) return;
    const requestId = ++cardRequest;
    const name = card.querySelector('#card-name');
    const status = card.querySelector('#card-status');
    const photo = card.querySelector('#card-photo');
    const details = card.querySelector('#card-details');
    name.textContent = 'Player details'; status.textContent = 'Loading…'; details.replaceChildren();
    card.querySelector('#card-points').hidden = true;
    card.querySelector('#card-ranks').hidden = true;
    card.querySelector('#card-scoring-basis').textContent = '';
    card.querySelector('#card-performance').hidden = true;
    card.querySelector('#card-weekly-points').replaceChildren();
    card.querySelector('#card-season-summary').replaceChildren();
    card.querySelector('#card-trend').textContent = '';
    card.querySelector('#card-trend').className = '';
    card.querySelector('#card-ranking-basis').textContent = '';
    card.querySelector('#card-scoring-rules').replaceChildren();
    card.querySelector('#card-scoring').open = false;
    card.querySelector('#card-scoring-league').textContent = '';
    card.querySelector('#card-team').textContent = ''; card.querySelector('#card-source').textContent = ''; photo.hidden = true;
    if (watchButton) { watchButton.hidden = true; watchButton.disabled = false; watchButton.dataset.player = ''; watchButton.dataset.league = ''; }
    if (compareLink) compareLink.hidden = true;
    card.showModal();
    try {
      const query = new URLSearchParams({league:button.dataset.league});
      if (button.dataset.week) query.set('week',button.dataset.week);
      const response = await fetch(`/api/players/${encodeURIComponent(button.dataset.playerCard)}?${query}`);
      if (!response.ok) throw new Error('Player details unavailable');
      const player = await response.json();
      if (requestId !== cardRequest) return;
      name.textContent = player.name; status.textContent = '';
      card.querySelector('#card-points').hidden = false;
      card.querySelector('#card-week').textContent = `Week ${player.week}`;
      card.querySelector('#card-projection').textContent = player.projection == null ? '—' : Number(player.projection).toFixed(1);
      card.querySelector('#card-actual').textContent = player.actual_points == null ? '—' : Number(player.actual_points).toFixed(2);
      const rankValue = value => Number.isInteger(Number(value)) ? String(Number(value)) : Number(value).toFixed(1);
      if (player.league_rank || player.primary_rank) {
        card.querySelector('#card-ranks').hidden = false;
        card.querySelector('#card-overall-rank').textContent = player.league_rank ? `#${player.league_rank.overall} overall` : '—';
        card.querySelector('#card-position-rank').textContent = player.league_rank ? `#${player.league_rank.position_rank} ${player.league_rank.position} by MFL YTD points` : 'Official YTD rank unavailable';
        card.querySelector('#card-primary-rank').textContent = player.primary_rank ? `#${rankValue(player.primary_rank.rank)} ${player.primary_rank.position}` : '—';
        card.querySelector('#card-primary-rank-source').textContent = player.primary_rank?.label || 'Primary projection rank unavailable';
      }
      card.querySelector('#card-scoring-basis').textContent = player.projection_source;
      const performance = card.querySelector('#card-performance');
      const weeklyList = card.querySelector('#card-weekly-points');
      const scoredWeeks = (player.weekly_points || []).filter((item) => item.points != null);
      const chartMaximum = Math.max(1, ...scoredWeeks.map((item) => Number(item.points)));
      (player.weekly_points || []).forEach((item) => {
        const row = document.createElement('li');
        const weekLabel = document.createElement('span'); weekLabel.textContent = `W${item.week}`;
        const meter = document.createElement('meter'); meter.min = 0; meter.max = chartMaximum; meter.value = item.points == null ? 0 : Number(item.points);
        meter.setAttribute('aria-label', item.points == null ? `Week ${item.week}: no score` : `Week ${item.week}: ${Number(item.points).toFixed(2)} points`);
        const points = document.createElement('strong'); points.textContent = item.points == null ? '—' : Number(item.points).toFixed(1);
        row.append(weekLabel, meter, points); weeklyList.append(row);
      });
      if (!player.weekly_points?.length) {
        const empty = document.createElement('li'); empty.className = 'card-history-empty'; empty.textContent = 'Weekly totals are not available yet.'; weeklyList.append(empty);
      }
      const summary = player.season_summary || {};
      [['YTD',summary.ytd],['Season avg',summary.average],['Last 3 avg',summary.recent_average],['Recent high',summary.high]].forEach(([label,value]) => {
        const group = document.createElement('div'); const dt = document.createElement('dt'); const dd = document.createElement('dd');
        dt.textContent = label; dd.textContent = value == null ? '—' : Number(value).toFixed(1); group.append(dt,dd); card.querySelector('#card-season-summary').append(group);
      });
      const trend = card.querySelector('#card-trend'); trend.textContent = player.trend?.label || 'Trend unavailable'; trend.className = `trend-${player.trend?.direction || 'neutral'}`;
      card.querySelector('#card-ranking-basis').textContent = `Primary list: ${player.ranking_preference}. Official season ranks, weekly totals, and averages use MFL league scoring.`;
      performance.hidden = false;
      card.querySelector('#card-scoring-league').textContent = player.scoring_league;
      const events = {'#P':'Passing TDs','PY':'Passing yards','IN':'Interceptions thrown','P2':'Passing two-point conversions','#R':'Rushing TDs','RY':'Rushing yards','R2':'Rushing two-point conversions','#C':'Receiving TDs','CY':'Receiving yards','CC':'Receptions','C2':'Receiving two-point conversions','EP':'Extra points','FL':'Fumbles lost','FG':'Field goal distance','FC':'Fumbles recovered','IC':'Interceptions caught','SK':'Sacks','SF':'Safeties','TPA':'Points allowed','#T':'Defensive TDs','#FR':'Fumble return TDs','#UT':'Punt return TDs','#KT':'Kickoff return TDs'};
      (player.scoring_rules || []).forEach((rule) => {
        const item = document.createElement('li');
        const points = rule.points.startsWith('*') ? `${rule.points.slice(1)} per unit` : `${rule.points} points`;
        item.textContent = `${events[rule.event] || rule.event}: ${points} (${rule.range})`;
        card.querySelector('#card-scoring-rules').append(item);
      });
      if (!player.scoring_rules?.length) card.querySelector('#card-scoring-league').textContent += ' · Rules unavailable; displayed points still come from MFL.';
      card.querySelector('#card-team').textContent = `${player.team} · ${player.position}${player.jersey ? ' · #' + player.jersey : ''}`;
      if (player.photo) { photo.onerror = () => { photo.hidden = true; }; photo.src = player.photo; photo.hidden = false; }
      [['College',player.college],['Height',player.height ? player.height + ' in' : ''],['Weight',player.weight ? player.weight + ' lb' : ''],['Draft year',player.draft_year]].forEach(([label,value]) => {
        if (!value) return;
        const dt = document.createElement('dt'); const dd = document.createElement('dd'); dt.textContent = label; dd.textContent = value; details.append(dt,dd);
      });
      card.querySelector('#card-source').textContent = player.source;
      if (watchButton) {
        watchButton.dataset.player = button.dataset.playerCard;
        watchButton.dataset.league = button.dataset.league;
        watchButton.dataset.watched = player.watched ? 'true' : 'false';
        watchButton.textContent = player.watched ? 'Remove from watchlist' : 'Add to watchlist';
        watchButton.hidden = false;
      }
      if (compareLink) {
        compareLink.href = `/compare?${new URLSearchParams({league:button.dataset.league,p1:button.dataset.playerCard})}`;
        compareLink.hidden = false;
      }
    } catch { if (requestId === cardRequest) status.textContent = 'Player details are unavailable. Close this card and try again.'; }
  }));
  watchButton?.addEventListener('click', async () => {
    if (!watchButton.dataset.player || !watchButton.dataset.league) return;
    watchButton.disabled = true;
    const previous = watchButton.textContent;
    watchButton.textContent = 'Saving…';
    try {
      const query = new URLSearchParams({league:watchButton.dataset.league});
      const response = await fetch(`/api/watchlist/${encodeURIComponent(watchButton.dataset.player)}?${query}`, {
        method:'POST', headers:{'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]')?.content || ''},
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Watchlist update failed');
      watchButton.dataset.watched = data.watched ? 'true' : 'false';
      watchButton.textContent = data.watched ? 'Remove from watchlist' : 'Add to watchlist';
    } catch (error) {
      watchButton.textContent = previous;
      const status = card?.querySelector('#card-status');
      if (status) status.textContent = error.message;
    } finally { watchButton.disabled = false; }
  });
  const rows = [...document.querySelectorAll("[data-player-row]")];
  const search = document.querySelector("#player-filter");
  const position = document.querySelector("#position-filter");
  const status = document.querySelector("#status-filter");
  const nflTeam = document.querySelector("#nfl-team-filter");
  const fantasyTeam = document.querySelector("#fantasy-team-filter");
  const projectionFilter = document.querySelector("#projection-filter");
  const playerSort = document.querySelector("#player-sort");
  const filterPanel = document.querySelector(".board-filter-panel");
  const resultCount = document.querySelector("#player-result-count");
  const clearFilters = document.querySelector("#clear-player-filters");
  const playerTableBody = document.querySelector(".waiver-table tbody");
  const empty = document.querySelector("#filtered-empty");
  const selectedCopy = document.querySelector("#selected-player");
  const dropSelect = document.querySelector('select[name="drop_id"]');
  const reviewButton = document.querySelector("#review-move");
  const modeSelect = document.querySelector("#move-mode");
  const moveShortcut = document.querySelector("#jump-to-move");
  let moveBuilderVisible = false;
  document.querySelectorAll(".position-token").forEach((token) => {
    token.dataset.position = token.textContent.trim().toUpperCase();
  });

  const updateReviewState = () => {
    const picked = document.querySelector('input[name="add_id"]:checked');
    if (selectedCopy) selectedCopy.textContent = picked
      ? `${picked.dataset.playerName}${picked.dataset.waiverOnly === "true" ? " · waiver claim only" : ""}`
      : "Choose a free agent or waiver target above";
    if (reviewButton) reviewButton.disabled = !(picked && dropSelect?.value);
    if (moveShortcut) moveShortcut.hidden = !picked || moveBuilderVisible;
  };

  const applyFilters = () => {
    const needle = (search?.value || "").trim().toLowerCase();
    const wantedPosition = position?.value || "all";
    const wantedStatus = status?.value || "all";
    const wantedNflTeam = nflTeam?.value || "all";
    const wantedFantasyTeam = fantasyTeam?.value || "all";
    const wantedProjection = projectionFilter?.value || "all";
    let shown = 0;
    rows.forEach((row) => {
      const matchesSearch = !needle || row.dataset.search.includes(needle);
      const matchesPosition = wantedPosition === "all" || row.dataset.position === wantedPosition;
      const matchesStatus = wantedStatus === "all"
        || (wantedStatus === "waiver" && ["waiver", "locked"].includes(row.dataset.status))
        || row.dataset.status === wantedStatus;
      const matchesNflTeam = wantedNflTeam === "all" || row.dataset.nflTeam === wantedNflTeam;
      const matchesFantasyTeam = wantedFantasyTeam === "all" || row.dataset.fantasyTeam === wantedFantasyTeam;
      const matchesProjection = wantedProjection === "all" || row.dataset.projected === wantedProjection;
      row.hidden = !(matchesSearch && matchesPosition && matchesStatus && matchesNflTeam && matchesFantasyTeam && matchesProjection);
      if (!row.hidden) shown += 1;
    });
    if (empty) empty.hidden = shown !== 0;
    if (resultCount) resultCount.textContent = shown === rows.length ? `${shown} shown` : `${shown} of ${rows.length} shown`;
  };

  const applySort = () => {
    if (!playerTableBody) return;
    const selected = playerSort?.value || playerSort?.dataset.defaultSort || "espn-rank";
    const number = (row, key) => Number(row.dataset[key] || -9999);
    const text = (row, key) => row.dataset[key] || "";
    const sorted = rows.slice().sort((left, right) => {
      if (selected === "espn-rank") return number(left, "espnRank") - number(right, "espnRank") || number(right, "projection") - number(left, "projection");
      if (selected === "mfl-rank") return number(left, "mflRank") - number(right, "mflRank") || number(right, "projection") - number(left, "projection");
      if (selected === "fantasypros-rank") return number(left, "fantasyprosRank") - number(right, "fantasyprosRank") || number(right, "projection") - number(left, "projection");
      if (selected === "cbs-rank") return number(left, "cbsRank") - number(right, "cbsRank") || number(right, "projection") - number(left, "projection");
      if (selected === "combined-rank") return number(left, "combinedRank") - number(right, "combinedRank") || number(right, "projection") - number(left, "projection");
      if (selected === "projection") return number(right, "projection") - number(left, "projection") || text(left, "name").localeCompare(text(right, "name"));
      if (selected === "ytd") return number(right, "ytd") - number(left, "ytd") || number(right, "avg") - number(left, "avg");
      if (selected === "avg") return number(right, "avg") - number(left, "avg") || number(right, "ytd") - number(left, "ytd");
      if (selected === "median") return number(right, "median") - number(left, "median") || number(right, "avg") - number(left, "avg");
      if (selected === "matchup") return number(left, "matchup") - number(right, "matchup") || number(right, "projection") - number(left, "projection");
      if (selected === "edge") return number(right, "edge") - number(left, "edge") || number(right, "projection") - number(left, "projection");
      if (selected === "name") return text(left, "name").localeCompare(text(right, "name"));
      if (selected === "nfl-team") return text(left, "nflTeam").localeCompare(text(right, "nflTeam")) || text(left, "name").localeCompare(text(right, "name"));
      if (selected === "fantasy-team") return text(left, "fantasyTeam").localeCompare(text(right, "fantasyTeam")) || text(left, "name").localeCompare(text(right, "name"));
      return number(left, "originalOrder") - number(right, "originalOrder");
    });
    sorted.forEach((row) => playerTableBody.append(row));
  };

  const updateModeFields = () => {
    const picked = document.querySelector('input[name="add_id"]:checked');
    const waiverOnly = picked?.dataset.waiverOnly === "true";
    const immediateOption = modeSelect?.querySelector('option[value="fcfs"]');
    if (immediateOption) immediateOption.disabled = waiverOnly;
    if (waiverOnly && modeSelect?.value === "fcfs") modeSelect.value = "waiver";
    const mode = modeSelect?.value || "fcfs";
    document.querySelectorAll("[data-mode-field]").forEach((field) => {
      const kind = field.dataset.modeField;
      field.hidden = mode === "fcfs" || (kind === "bid" && mode !== "blind-bid");
      const input = field.querySelector("input");
      if (input) input.required = (kind === "round" && mode === "waiver") || (kind === "bid" && mode === "blind-bid");
    });
  };

  [search, position, status, nflTeam, fantasyTeam, projectionFilter].forEach((control) => {
    control?.addEventListener(control === search ? "input" : "change", applyFilters);
  });
  playerSort?.addEventListener("change", applySort);
  clearFilters?.addEventListener("click", () => {
    if (search) search.value = "";
    [position, status, nflTeam, fantasyTeam, projectionFilter].forEach((control) => {
      if (control) control.value = "all";
    });
    if (playerSort) playerSort.value = playerSort.dataset.defaultSort || "espn-rank";
    applyFilters();
    applySort();
    search?.focus();
  });
  document.querySelectorAll('input[name="add_id"]').forEach((radio) => radio.addEventListener("change", () => {
    if (radio.checked && ["waiver", "locked"].includes(radio.dataset.marketStatus) && modeSelect) modeSelect.value = "waiver";
    if (radio.checked && radio.dataset.marketStatus === "open" && modeSelect?.value === "waiver") modeSelect.value = "fcfs";
    updateModeFields();
    updateReviewState();
  }));
  rows.forEach((row) => row.addEventListener("click", (event) => {
    if (event.target.closest("input, a, button")) return;
    const radio = row.querySelector('input[name="add_id"]:not(:disabled)');
    if (radio) {
      radio.checked = true;
      if (["waiver", "locked"].includes(radio.dataset.marketStatus) && modeSelect) modeSelect.value = "waiver";
      if (radio.dataset.marketStatus === "open" && modeSelect?.value === "waiver") modeSelect.value = "fcfs";
      updateModeFields();
      updateReviewState();
    }
  }));
  dropSelect?.addEventListener("change", updateReviewState);
  modeSelect?.addEventListener("change", () => { updateModeFields(); updateReviewState(); });
  applyFilters();
  applySort();
  updateModeFields();
  updateReviewState();
  if (filterPanel && window.matchMedia("(max-width: 760px)").matches && !search?.value) filterPanel.open = false;

  const marketWorkspace = document.querySelector("[data-player-market-enrichment]");
  const replaceMetric = (cell, value, secondary = "", className = "") => {
    if (!cell) return;
    cell.replaceChildren();
    if (value === null || value === undefined || value === "") {
      const missing = document.createElement("span");
      missing.className = "no-data";
      missing.textContent = "—";
      cell.append(missing);
      return;
    }
    const primary = document.createElement("strong");
    primary.className = className;
    primary.textContent = value;
    cell.append(primary);
    if (secondary) {
      const note = document.createElement("small");
      note.textContent = secondary;
      cell.append(note);
    }
  };
  const numberOr = (value, fallback) => value === null || value === undefined ? fallback : Number(value);
  const applyMarketVerification = (data) => {
    if (data.reload_required) {
      window.location.reload();
      return false;
    }
    if (marketWorkspace) marketWorkspace.dataset.marketVerified = "true";
    rows.forEach((row) => {
      const player = data.players?.[row.dataset.playerId];
      if (!player) return;
      row.dataset.status = player.market_status;
      row.dataset.fantasyTeam = player.fantasy_team_id || "none";
      const addControl = row.querySelector('input[name="add_id"]');
      if (addControl) {
        addControl.disabled = Boolean(player.is_rostered || !player.is_claimable);
        addControl.dataset.marketStatus = player.market_status;
        addControl.dataset.waiverOnly = ["waiver", "locked"].includes(player.market_status) ? "true" : "false";
      }
    });
    const summaryTargets = {
      "market-player-count": data.summary?.player_count,
      "market-available-count": data.summary?.available_count,
      "market-rostered-count": data.summary?.rostered_count,
      "market-locked-count": data.summary?.locked_count,
    };
    Object.entries(summaryTargets).forEach(([id, value]) => {
      const target = document.getElementById(id);
      if (target && value !== undefined) target.textContent = value;
    });
    const lockedDrops = new Set(data.roster_locked || []);
    dropSelect?.querySelectorAll("[data-drop-player]").forEach((option) => {
      const locked = lockedDrops.has(option.dataset.dropPlayer);
      option.disabled = locked;
      option.textContent = `${option.dataset.dropLabel}${locked ? " · LOCKED" : ""}`;
      if (locked && option.selected) dropSelect.value = "";
    });
    const lockStatus = document.getElementById("drop-lock-status");
    if (lockStatus) lockStatus.textContent = "Ownership and kickoff locks are verified. Started games stay locked and cannot be dropped.";
    applyFilters();
    updateReviewState();
    return true;
  };
  const scheduleMarketEnrichment = () => {
    if (!marketWorkspace?.dataset.playerMarketEnrichment) return;
    if ("requestIdleCallback" in window) window.requestIdleCallback(enrichMarket, { timeout: 1500 });
    else window.setTimeout(enrichMarket, 0);
  };
  const enrichMarket = async () => {
    const url = marketWorkspace?.dataset.playerMarketEnrichment;
    if (!url || !rows.length) return;
    try {
      const response = await fetch(url, { headers: { "Accept": "application/json" } });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Player intelligence is unavailable.");
      if (!applyMarketVerification(data)) return;
      rows.forEach((row) => {
        const player = data.players?.[row.dataset.playerId];
        if (!player) return;
        row.dataset.projected = player.projection === null ? "missing" : "projected";
        row.dataset.projection = String(numberOr(player.projection, -9999));
        row.dataset.espnRank = String(numberOr(player.espn_rank, 9999));
        row.dataset.mflRank = String(numberOr(player.mfl_rank, 9999));
        row.dataset.fantasyprosRank = String(numberOr(player.fantasypros_rank, 9999));
        row.dataset.cbsRank = String(numberOr(player.cbs_rank, 9999));
        row.dataset.combinedRank = String(numberOr(player.combined_rank, 9999));
        row.dataset.ytd = String(numberOr(player.ytd, -9999));
        row.dataset.avg = String(numberOr(player.average, -9999));
        row.dataset.median = String(numberOr(player.median, -9999));
        row.dataset.matchup = String(numberOr(player.matchup?.rank, 9999));
        row.dataset.edge = String(numberOr(player.roster_delta, -9999));

        replaceMetric(row.querySelector(".projection-cell"), player.projection === null ? null : Number(player.projection).toFixed(1), "MFL league-scored");
        replaceMetric(row.querySelector(".ytd-cell"), player.ytd === null ? null : Number(player.ytd).toFixed(1));
        replaceMetric(row.querySelector(".median-cell"), player.median === null ? null : Number(player.median).toFixed(1), player.median === null ? "" : `Last ${player.median_window}`);
        replaceMetric(row.querySelector(".avg-cell"), player.average === null ? null : Number(player.average).toFixed(1));
        replaceMetric(row.querySelector(".espn-rank-cell"), player.espn_rank === null ? null : `#${Number(player.espn_rank).toFixed(1)}`);
        replaceMetric(row.querySelector(".mfl-rank-cell"), player.mfl_rank === null ? null : `#${Number(player.mfl_rank).toFixed(1)}`);
        replaceMetric(row.querySelector(".fantasypros-rank-cell"), player.fantasypros_rank === null ? null : `#${Number(player.fantasypros_rank).toFixed(1)}`);
        replaceMetric(row.querySelector(".cbs-rank-cell"), player.cbs_rank === null ? null : `#${Number(player.cbs_rank).toFixed(1)}`);
        const matchupCell = row.querySelector(".matchup-cell");
        matchupCell?.replaceChildren();
        if (matchupCell && player.matchup) {
          const badge = document.createElement("span");
          badge.className = `opponent-strength ${player.matchup.tone || "neutral"}`;
          badge.textContent = `${player.matchup.opponent} · #${player.matchup.rank} ${String(player.matchup.label || "").toLowerCase()}`;
          const detail = document.createElement("small");
          detail.textContent = `${Number(player.matchup.points_allowed).toFixed(1)} ${player.matchup.position} pts allowed`;
          matchupCell.append(badge, detail);
        } else if (matchupCell) {
          const missing = document.createElement("span");
          missing.className = "no-data";
          missing.textContent = "—";
          matchupCell.append(missing);
        }
        const delta = player.roster_delta;
        replaceMetric(
          row.querySelector(".edge-cell"),
          delta === null ? null : `${delta > 0 ? "+" : ""}${Number(delta).toFixed(1)}`,
          player.suggested_drop ? `vs ${player.suggested_drop}` : "",
          delta >= 0.5 ? "positive" : delta < 0 ? "negative" : "",
        );
        const recommendationCell = row.querySelector(".recommendation-cell");
        if (recommendationCell) {
          recommendationCell.replaceChildren();
          const label = document.createElement("span");
          label.className = `recommendation ${player.recommendation_tone || "muted"}`;
          label.textContent = player.recommendation;
          const reason = document.createElement("small");
          reason.className = "recommendation-reason";
          reason.textContent = player.reason;
          recommendationCell.append(label, reason);
        }
      });
      const projectedCount = document.getElementById("market-projected-count");
      if (projectedCount) projectedCount.textContent = data.summary.projected_count;
      const source = document.getElementById("market-projection-source");
      const ml = document.getElementById("market-ml-matched");
      const combined = document.getElementById("market-combined-matched");
      const copy = document.getElementById("market-enrichment-copy");
      if (source) source.textContent = data.projection.source;
      if (ml) ml.textContent = `${data.projection.ml_matched} players`;
      if (combined) combined.textContent = `${data.projection.combined_matched} players`;
      if (copy) copy.textContent = `${data.projection.ranking_label} leads the default sort. Rankings and projections loaded after MFL verified ownership, availability, and locks; every submitted move is checked again before it is sent.`;
      const waiver = document.getElementById("market-waiver-enrichment");
      const defense = document.getElementById("market-defense-enrichment");
      if (waiver) waiver.innerHTML = data.waiver_html;
      if (defense) defense.innerHTML = data.defense_html;
      applyFilters();
      applySort();
      updateReviewState();
    } catch (error) {
      const source = document.getElementById("market-projection-source");
      const copy = document.getElementById("market-enrichment-copy");
      if (source) source.textContent = "Extra data unavailable";
      const verified = marketWorkspace?.dataset.marketVerified === "true";
      if (copy) copy.textContent = verified
        ? `${error.message} Ownership and kickoff locks are verified, so moves remain available; only advisory rankings are missing.`
        : `${error.message} The player pool remains available; moves stay disabled until the kickoff lock check succeeds.`;
      ["market-waiver-enrichment", "market-defense-enrichment"].forEach((id) => {
        const target = document.getElementById(id);
        if (target) target.textContent = "Advisory details could not be loaded. The saved player pool remains available for research.";
      });
    }
  };
  const verifyMarket = async () => {
    const url = marketWorkspace?.dataset.playerMarketVerification;
    if (!url || !rows.length) {
      scheduleMarketEnrichment();
      return;
    }
    try {
      const response = await fetch(url, { headers: { "Accept": "application/json" } });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "MFL verification is unavailable.");
      if (!applyMarketVerification(data)) return;
      const copy = document.getElementById("market-enrichment-copy");
      if (copy) copy.textContent = "Ownership, availability, and kickoff locks are ready. Projection points, season totals, rankings, matchup context, and waiver intelligence are loading next.";
    } catch (error) {
      const copy = document.getElementById("market-enrichment-copy");
      if (copy) copy.textContent = `${error.message} The saved player pool remains browse-only while the full refresh tries again.`;
    }
    scheduleMarketEnrichment();
  };
  if (marketWorkspace) verifyMarket();

  const moveBuilder = document.querySelector("#move-builder");
  const useDefenseSuggestion = (button) => {
    const radio = [...document.querySelectorAll('input[name="add_id"]')].find((input) => input.value === button.dataset.streamPick);
    if (!radio || radio.disabled) return;
    radio.click(); // Preserve normal availability/move-mode validation and review.
    if (button.dataset.streamBid !== undefined && modeSelect) {
      modeSelect.value = "blind-bid";
      const bidInput = document.querySelector('#waiver-form input[name="bid"]');
      if (bidInput) bidInput.value = button.dataset.streamBid;
      updateModeFields();
      updateReviewState();
    }
    moveBuilder?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    dropSelect?.focus({ preventScroll: true });
  };
  const useQueueSuggestion = (button) => {
    const radio = [...document.querySelectorAll('input[name="add_id"]')].find((input) => input.value === button.dataset.queueAdd);
    const dropOption = dropSelect?.querySelector(`option[value="${CSS.escape(button.dataset.queueDrop || "")}"]:not(:disabled)`);
    if (!radio || radio.disabled || !dropOption) return;
    radio.click();
    if (modeSelect) modeSelect.value = button.dataset.queueBid !== "" ? "blind-bid" : "waiver";
    dropSelect.value = button.dataset.queueDrop;
    const bidInput = document.querySelector('#waiver-form input[name="bid"]');
    const roundInput = document.querySelector('#waiver-form input[name="round_number"]');
    if (bidInput && button.dataset.queueBid !== "") bidInput.value = button.dataset.queueBid;
    if (roundInput) roundInput.value = button.dataset.queueRound || "";
    updateModeFields();
    updateReviewState();
    moveBuilder?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    modeSelect?.focus({ preventScroll: true });
  };
  document.addEventListener("click", (event) => {
    const streamButton = event.target.closest?.("[data-stream-pick]");
    if (streamButton) {
      useDefenseSuggestion(streamButton);
      return;
    }
    const queueButton = event.target.closest?.("[data-queue-add]");
    if (queueButton) useQueueSuggestion(queueButton);
  });
  if (moveBuilder && moveShortcut && "IntersectionObserver" in window) {
    new IntersectionObserver(([entry]) => {
      moveBuilderVisible = entry.isIntersecting;
      updateReviewState();
    }, { rootMargin: "0px 0px -150px 0px" }).observe(moveBuilder);
  }

  const lineupForm = document.querySelector("#lineup-form");
  if (lineupForm) {
    const requiredStarters = Number(lineupForm.dataset.starterCount || 0);
    const lineupRows = [...lineupForm.querySelectorAll("[data-lineup-player]")];
    const starterChecks = [...lineupForm.querySelectorAll('input[type="checkbox"][name="starter_ids"]')];
    const countCopy = document.querySelector("#starter-count");
    const validationCopy = document.querySelector("#lineup-validation");
    const lineupButtons = [...document.querySelectorAll("#review-lineup, [data-review-lineup]")];
    const selectedProjection = document.querySelector("#selected-projection");
    const draftStatus = document.querySelector("#lineup-draft-status");
    const slotSpecs = JSON.parse(lineupForm.dataset.slotSpecs || '[]');
    const dragStatus = document.querySelector('#drag-status');
    const checkFor = (row) => row.querySelector('input[type="checkbox"][name="starter_ids"]');
    const assignSlots = () => {
      lineupForm.querySelectorAll('[data-empty-lineup-slot]').forEach((row) => row.remove());
      const selected = lineupRows.filter((row) => checkFor(row).checked);
      const assigned = new Map();
      const slotOrder = slotSpecs.map((_,i) => i).sort((a,b) => slotSpecs[a].positions.length - slotSpecs[b].positions.length || a-b);
      const place = (row, seen) => {
        for (const index of slotOrder) {
          if (seen.has(index) || !slotSpecs[index].positions.includes(row.dataset.position.replace('D/ST','DEF'))) continue;
          seen.add(index);
          if (!assigned.has(index) || place(assigned.get(index), seen)) { assigned.set(index,row); return true; }
        }
        return false;
      };
      selected.slice().sort((a,b) => checkFor(a).value.localeCompare(checkFor(b).value)).forEach((row) => place(row,new Set()));
      lineupRows.forEach((row) => { row.querySelector('[data-slot-label]').textContent = checkFor(row).checked ? '—' : 'BN'; });
      const section = lineupForm.querySelector('[data-roster-section="starters"]');
      slotSpecs.forEach((slot,index) => {
        const row = assigned.get(index);
        if (row) {
          row.querySelector('[data-slot-label]').textContent = slot.label;
          section.append(row);
          return;
        }
        const empty = document.createElement('article');
        empty.className = 'editor-player lineup-empty-slot';
        empty.dataset.emptyLineupSlot = 'true';
        empty.setAttribute('aria-label', `${slot.label} starter slot is empty`);
        empty.innerHTML = `<div class="roster-position"><span>${slot.label}</span></div><div class="lineup-empty-copy"><strong>Open starter slot</strong><small>No player selected</small></div><div class="roster-game"><span>—</span></div><div class="editor-player-projection">—</div><div class="roster-score">—</div><div></div>`;
        section.append(empty);
      });
    };

    const updateLineupCount = () => {
      const count = starterChecks.filter((input) => input.checked).length;
      if (countCopy) countCopy.textContent = String(count);
      const valid = count === requiredStarters;
      lineupButtons.forEach((button) => { button.disabled = !valid; });
      const projection = lineupRows.reduce((total, row) => {
        const checked = row.querySelector('input[type="checkbox"][name="starter_ids"]')?.checked;
        return total + (checked ? Number(row.dataset.projection || 0) : 0);
      }, 0);
      const missingProjection = lineupRows.some((row) => checkFor(row).checked && row.dataset.projection === '');
      if (selectedProjection) selectedProjection.textContent = missingProjection ? '—' : projection.toFixed(1);
      const changed = lineupRows.some((row) => {
        const checked = row.querySelector('input[type="checkbox"][name="starter_ids"]')?.checked;
        return checked !== (row.dataset.current === "true");
      });
      if (draftStatus) draftStatus.textContent = changed ? "Unsaved changes" : lineupForm.dataset.savedVisible === 'false' ? 'No saved lineup visible' : 'Saved on MFL';
      const focused = document.activeElement;
      lineupRows.forEach((row) => {
        const checked = row.querySelector('input[type="checkbox"][name="starter_ids"]')?.checked;
        const section = lineupForm.querySelector(`[data-roster-section="${checked ? 'starters' : 'bench'}"]`);
        if (section && row.parentElement !== section) section.append(row);
        row.querySelectorAll('[data-move-to]').forEach((button) => { button.disabled = checkFor(row).disabled || ((button.dataset.moveTo === 'starters') === checked); });
      });
      assignSlots();
      if (focused instanceof HTMLInputElement && starterChecks.includes(focused)) focused.focus({preventScroll:true});
      if (validationCopy) {
        validationCopy.textContent = valid
          ? "Ready to review"
          : `Select ${requiredStarters - count > 0 ? requiredStarters - count + " more" : count - requiredStarters + " fewer"}`;
        validationCopy.classList.toggle("invalid", !valid);
      }
    };

    const applyLineup = (stateName) => {
      lineupRows.forEach((row) => {
        const input = row.querySelector('input[name="starter_ids"]');
        if (input && !input.disabled) input.checked = row.dataset[stateName] === "true";
      });
      updateLineupCount();
    };

    starterChecks.forEach((input) => input.addEventListener("change", updateLineupCount));
    const movePlayer = (row, destination, targetRow = null) => {
      const input = checkFor(row);
      if (input.disabled) return;
      const start = destination === 'starters';
      if (input.checked === start) return;
      if (start && starterChecks.filter((check) => check.checked).length >= requiredStarters && targetRow && targetRow !== row) {
        const other = checkFor(targetRow);
        if (other.disabled) { dragStatus.textContent = 'That player is locked and cannot be swapped.'; return; }
        if (other.checked) other.checked = false;
      }
      input.checked = start;
      row.querySelector('.player-actions').open = false;
      updateLineupCount();
      dragStatus.textContent = `${input.getAttribute('aria-label').replace(/^Start /,'')} moved to ${start ? 'starters' : 'bench'}. Review before saving.`;
    };
    lineupRows.forEach((row) => {
      row.querySelectorAll('[data-move-to]').forEach((button) => button.addEventListener('click', () => movePlayer(row,button.dataset.moveTo)));
      row.querySelector('.player-actions').addEventListener('toggle', (event) => {
        if (event.target.open) lineupForm.querySelectorAll('.player-actions[open]').forEach((menu) => { if (menu !== event.target) menu.open = false; });
      });
    });
    document.addEventListener('click', (event) => { if (!event.target.closest('.player-actions')) lineupForm.querySelectorAll('.player-actions[open]').forEach((menu) => { menu.open = false; }); });
    document.addEventListener('keydown', (event) => { if (event.key === 'Escape') lineupForm.querySelectorAll('.player-actions[open]').forEach((menu) => { menu.open = false; }); });
    let draggedRow = null;
    const targets = [...lineupForm.querySelectorAll('[data-roster-section], [data-roster-target]')];
    const targetAt = (element) => element?.closest('[data-roster-section], [data-roster-target]');
    const clearDrag = () => { draggedRow?.classList.remove('dragging'); draggedRow = null; targets.forEach((target) => target.classList.remove('drop-active')); };
    const dropOn = (element) => {
      const target = targetAt(element);
      if (draggedRow && target) movePlayer(draggedRow,target.dataset.rosterSection || target.dataset.rosterTarget,element.closest('[data-lineup-player]'));
      clearDrag();
    };
    lineupRows.forEach((row) => {
      row.addEventListener('dragstart', (event) => {
        if (checkFor(row).disabled || event.target.closest('button, summary, input, a')) { event.preventDefault(); return; }
        draggedRow = row; row.classList.add('dragging'); event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain',checkFor(row).value);
      });
      row.addEventListener('dragend',clearDrag);
      const handle = row.querySelector('.drag-handle');
      let gesture = null;
      let scrollFrame = 0;
      const autoScroll = () => {
        if (!gesture || !draggedRow) return;
        if (gesture.y < 80) window.scrollBy(0,-12);
        if (gesture.y > window.innerHeight-80) window.scrollBy(0,12);
        scrollFrame = requestAnimationFrame(autoScroll);
      };
      handle.addEventListener('pointerdown', (event) => {
        if (handle.disabled || event.button !== 0) return;
        gesture = {x:event.clientX,y:event.clientY,startX:event.clientX,startY:event.clientY};
        row.draggable = false;
        handle.setPointerCapture(event.pointerId);
      });
      handle.addEventListener('pointermove', (event) => {
        if (!gesture) return;
        gesture.x = event.clientX; gesture.y = event.clientY;
        if (!draggedRow && Math.hypot(gesture.x-gesture.startX,gesture.y-gesture.startY) > 6) {
          draggedRow = row; row.classList.add('dragging'); autoScroll();
        }
        if (draggedRow) {
          const target = targetAt(document.elementFromPoint(event.clientX,event.clientY));
          targets.forEach((item) => item.classList.toggle('drop-active',item === target));
        }
      });
      handle.addEventListener('pointerup', (event) => {
        if (gesture && draggedRow) dropOn(document.elementFromPoint(event.clientX,event.clientY));
        gesture = null; row.draggable = !checkFor(row).disabled; cancelAnimationFrame(scrollFrame); clearDrag();
      });
      handle.addEventListener('pointercancel', () => { gesture = null; row.draggable = !checkFor(row).disabled; cancelAnimationFrame(scrollFrame); clearDrag(); });
    });
    targets.forEach((target) => {
      target.addEventListener('dragover', (event) => { if (draggedRow) { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; target.classList.add('drop-active'); } });
      target.addEventListener('dragleave', () => target.classList.remove('drop-active'));
      target.addEventListener('drop', (event) => { if (draggedRow) { event.preventDefault(); dropOn(event.target); } });
    });
    document.querySelector("#use-recommended")?.addEventListener("click", () => applyLineup("recommended"));
    document.querySelector("#restore-current")?.addEventListener("click", () => applyLineup("current"));
    document.querySelector("#editor-league")?.addEventListener("change", (event) => {
      const week = document.querySelector("#lineup-week")?.value || "1";
      window.location.assign("/lineup?" + new URLSearchParams({ league: event.target.value, week }));
    });
    lineupForm.addEventListener("submit", () => {
      lineupButtons.forEach((button) => { button.disabled = true; button.textContent = "Checking lineup…"; });
    });
    updateLineupCount();
  }

  const refreshSeconds = Number(document.body.dataset.liveRefresh || 0);
  if (document.body.dataset.liveWindow) {
    let remaining = refreshSeconds;
    const countdown = document.querySelector("#refresh-countdown");
    const status = document.querySelector('#refresh-status');
    let timer = null;
    let wake = null;
    let checking = false;
    const busy = () => document.hidden || document.querySelector('#points-card[open], .team-points-breakdown[open], #player-card[open]');
    const scheduleKickoff = timestamp => {
      clearTimeout(wake);
      if (!timestamp) return;
      const delay = Number(timestamp) * 1000 + 60000 - Date.now();
      if (delay > 0 && delay < 2147483647) wake = setTimeout(checkWindow, delay);
    };
    const checkWindow = async () => {
      if (checking) return;
      if (busy()) { wake = setTimeout(checkWindow, 15000); return; }
      checking = true;
      try {
        const response = await fetch(document.body.dataset.liveWindow, {signal:AbortSignal.timeout(20000)});
        if (!response.ok) throw new Error('Game status unavailable');
        const state = await response.json();
        if (state.active) { window.location.reload(); return; }
        clearInterval(timer); timer = null;
        if (status) status.textContent = state.next_kickoff ? 'Auto-updates paused until the next NFL kickoff.' : 'Auto-updates paused · no NFL game confirmed live.';
        scheduleKickoff(state.next_kickoff);
      } catch (_) {
        clearInterval(timer); timer = null;
        if (status) status.textContent = 'Auto-updates paused · game status unavailable. Refresh manually to retry.';
      } finally { checking = false; }
    };
    if (refreshSeconds > 0) timer = window.setInterval(() => {
      if (busy()) {
        if (countdown) countdown.textContent = 'paused while reading';
        return;
      }
      remaining -= 1;
      if (countdown) countdown.textContent = String(Math.max(remaining, 0));
      if (remaining <= 0) checkWindow();
    }, 1000);
    else scheduleKickoff(document.body.dataset.nextKickoff);
  }

  const context = document.modelContext;
  if (!context?.registerTool) return;
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;

  context.registerTool({
    name: "find_mfl_free_agents",
    title: "Rank MFL available players",
    description: "List available players in a connected MFL league with lock status, league-scored projections, and roster-relative recommendations.",
    inputSchema: {
      type: "object",
      properties: { league_id: { type: "string" }, query: { type: "string" } },
      required: ["league_id"],
      additionalProperties: false
    },
    annotations: { readOnlyHint: true, untrustedContentHint: true },
    async execute({ league_id, query = "" }) {
      const response = await fetch(`/api/free-agents?league=${encodeURIComponent(league_id)}&q=${encodeURIComponent(query)}`);
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }
  });

  context.registerTool({
    name: "stage_mfl_add_drop",
    title: "Stage an MFL add/drop",
    description: "Validate an unlocked add/drop and open its review screen. This does not submit the move to MFL.",
    inputSchema: {
      type: "object",
      properties: {
        league_id: { type: "string" }, add_id: { type: "string" }, drop_id: { type: "string" },
        mode: { type: "string", enum: ["fcfs", "waiver", "blind-bid"] },
        bid: { type: "integer", minimum: 0 }, round: { type: "integer", minimum: 1 }
      },
      required: ["league_id", "add_id", "drop_id", "mode"],
      additionalProperties: false
    },
    annotations: { readOnlyHint: false, untrustedContentHint: false },
    async execute(input) {
      const response = await fetch("/api/stage-move", {
        method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify(input)
      });
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json();
      window.location.assign(result.preview_url);
      return result;
    }
  });
})();
