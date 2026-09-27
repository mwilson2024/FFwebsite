(() => {
  const cards = [...document.querySelectorAll('[data-command-league]')];
  const text = value => document.createTextNode(String(value));
  const link = (label, href) => { const item = document.createElement('a'); item.href = href; item.append(text(label)); return item; };
  const render = (card, data) => {
    card.classList.remove('loading');
    const state = card.querySelector('.load-state');
    if (data.error) { card.classList.add('failed'); state.textContent = 'Unavailable'; card.querySelector('.command-skeleton').replaceWith(Object.assign(document.createElement('p'), {textContent: data.error})); return; }
    state.textContent = `Week ${data.week}`;
    const body = document.createElement('div'); body.className = 'command-body';
    const matchup = document.createElement('section'); matchup.className = 'command-score';
    const mine = document.createElement('strong'); mine.append(text(data.score == null ? '—' : Number(data.score).toFixed(2)));
    const vs = document.createElement('span'); vs.append(text(`vs ${data.opponent} · ${data.opponent_score == null ? '—' : Number(data.opponent_score).toFixed(2)}`));
    const chance = document.createElement('small'); chance.append(text(data.win_probability == null ? 'Win estimate unavailable' : `${Number(data.win_probability).toFixed(2)}% estimated win`));
    matchup.append(mine, vs, chance); body.append(matchup);
    const edge = document.createElement('p'); edge.className = 'command-edge'; edge.append(text(data.projected_gain > 0 ? `Best-lineup edge: +${Number(data.projected_gain).toFixed(2)} points` : 'No projected lineup gain')); body.append(edge);
    if (data.alerts.length) { const list = document.createElement('ul'); data.alerts.forEach(alert => { const row = document.createElement('li'); const a = link(alert.title, alert.href); const detail = document.createElement('small'); detail.append(text(alert.detail)); row.append(a, detail); list.append(row); }); body.append(list); }
    else { const clear = document.createElement('p'); clear.className = 'all-clear'; clear.append(text('No urgent lineup or injury alert.')); body.append(clear); }
    const actions = document.createElement('nav'); [['Home','home'],['Lineup','lineup'],['Scores','scores'],['Players','moves']].forEach(([label,key]) => actions.append(link(label, data.links[key]))); body.append(actions);
    card.querySelector('.command-skeleton').replaceWith(body);
  };
  (async () => { for (const card of cards) { try { const response = await fetch(`/api/command-center/${encodeURIComponent(card.dataset.commandLeague)}`, {headers: {'Accept':'application/json'}}); const data = await response.json(); render(card, data); } catch (_) { render(card, {error:'This league could not load.'}); } } })();
})();
