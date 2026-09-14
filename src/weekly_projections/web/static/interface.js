(() => {
  const main = document.querySelector('main');
  if (main) { main.id = 'main-content'; main.tabIndex = -1; }
  const options = document.querySelector('.site-options');
  const activityCard = document.querySelector('#activity-card');
  let activityTrigger = null;
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
  document.querySelectorAll('[data-activity-card]').forEach(trigger => {
    trigger.addEventListener('click', () => {
      const template = document.getElementById(trigger.dataset.activityCard || '');
      const content = activityCard?.querySelector('#activity-card-content');
      const title = activityCard?.querySelector('#activity-card-title');
      if (!activityCard || !template || !content || !title) return;
      activityTrigger = trigger;
      title.textContent = trigger.dataset.activityTitle || 'Transaction details';
      content.replaceChildren(template.content.cloneNode(true));
      if (!activityCard.open) activityCard.showModal();
    });
  });
  activityCard?.addEventListener('click', event => {
    if (event.target !== activityCard) return;
    const bounds = activityCard.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) activityCard.close();
  });
  activityCard?.addEventListener('close', () => activityTrigger?.focus());
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
