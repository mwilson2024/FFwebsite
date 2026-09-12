(() => {
  const leaguePicker = document.querySelector('.header-league-picker');
  if (leaguePicker) {
    leaguePicker.querySelector('select').addEventListener('change', () => leaguePicker.requestSubmit());
    leaguePicker.querySelector('button').hidden = true;
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
  document.querySelectorAll('[data-kickoff]').forEach((time) => {
    time.textContent = new Intl.DateTimeFormat(undefined, {weekday:'short', hour:'numeric', minute:'2-digit', timeZoneName:'short'}).format(new Date(Number(time.dataset.kickoff) * 1000));
  });
  document.querySelectorAll('.player-headshot').forEach((image) => {
    image.addEventListener('error', () => { image.hidden = true; });
    if (image.complete && !image.naturalWidth) image.hidden = true;
  });
  const card = document.querySelector('#player-card');
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
    card.querySelector('#card-scoring-basis').textContent = '';
    card.querySelector('#card-scoring-rules').replaceChildren();
    card.querySelector('#card-scoring').open = false;
    card.querySelector('#card-scoring-league').textContent = '';
    card.querySelector('#card-team').textContent = ''; card.querySelector('#card-source').textContent = ''; photo.hidden = true;
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
      card.querySelector('#card-scoring-basis').textContent = player.projection_source;
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
    } catch { if (requestId === cardRequest) status.textContent = 'Player details are unavailable. Close this card and try again.'; }
  }));
  const rows = [...document.querySelectorAll("[data-player-row]")];
  const search = document.querySelector("#player-filter");
  const position = document.querySelector("#position-filter");
  const status = document.querySelector("#status-filter");
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
    if (selectedCopy) selectedCopy.textContent = picked?.dataset.playerName || "Choose an unlocked player above";
    if (reviewButton) reviewButton.disabled = !(picked && dropSelect?.value);
    if (moveShortcut) moveShortcut.hidden = !picked || moveBuilderVisible;
  };

  const applyFilters = () => {
    const needle = (search?.value || "").trim().toLowerCase();
    const wantedPosition = position?.value || "all";
    const wantedStatus = status?.value || "all";
    let shown = 0;
    rows.forEach((row) => {
      const matchesSearch = !needle || row.dataset.search.includes(needle);
      const matchesPosition = wantedPosition === "all" || row.dataset.position === wantedPosition;
      const matchesStatus = wantedStatus === "all" || row.dataset.status === wantedStatus;
      row.hidden = !(matchesSearch && matchesPosition && matchesStatus);
      if (!row.hidden) shown += 1;
    });
    if (empty) empty.hidden = shown !== 0;
  };

  const updateModeFields = () => {
    const mode = modeSelect?.value || "fcfs";
    document.querySelectorAll("[data-mode-field]").forEach((field) => {
      const kind = field.dataset.modeField;
      field.hidden = mode === "fcfs" || (kind === "bid" && mode !== "blind-bid");
      const input = field.querySelector("input");
      if (input) input.required = (kind === "round" && mode === "waiver") || (kind === "bid" && mode === "blind-bid");
    });
  };

  [search, position, status].forEach((control) => {
    control?.addEventListener(control === search ? "input" : "change", applyFilters);
  });
  document.querySelectorAll('input[name="add_id"]').forEach((radio) => radio.addEventListener("change", updateReviewState));
  rows.forEach((row) => row.addEventListener("click", (event) => {
    if (event.target.closest("input, a, button")) return;
    const radio = row.querySelector('input[name="add_id"]:not(:disabled)');
    if (radio) {
      radio.checked = true;
      updateReviewState();
    }
  }));
  dropSelect?.addEventListener("change", updateReviewState);
  modeSelect?.addEventListener("change", updateModeFields);
  applyFilters();
  updateModeFields();
  updateReviewState();

  const moveBuilder = document.querySelector("#move-builder");
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
        if (row) { row.querySelector('[data-slot-label]').textContent = slot.label; section.append(row); }
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
