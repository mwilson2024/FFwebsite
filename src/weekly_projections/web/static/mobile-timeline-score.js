(() => {
  const dock = document.querySelector('[data-pulse-score]');
  if (!dock) return;
  const strip = dock.querySelector('.pulse-score-strip');
  const toggle = dock.querySelector('[data-pulse-open]');
  const close = dock.querySelector('[data-pulse-close]');
  const key = `wp-pulse-score:${dock.dataset.pulseKey}`;
  let expanded = false;
  // Per-tab display preference only; carry through the existing scoring reload.
  try { expanded = sessionStorage.getItem(key) === 'expanded'; } catch (_) {}
  const render = (open, focus = false) => {
    expanded = open;
    strip.hidden = !open;
    toggle.hidden = open;
    toggle.setAttribute('aria-expanded', String(open));
    if (focus) (open ? close : toggle).focus({preventScroll:true});
  };
  const setExpanded = open => {
    render(open, true);
    try { sessionStorage.setItem(key, open ? 'expanded' : 'collapsed'); } catch (_) {}
  };
  toggle.addEventListener('click', () => setExpanded(true));
  close.addEventListener('click', () => setExpanded(false));
  dock.addEventListener('keydown', event => {
    if (event.key === 'Escape' && expanded) { event.preventDefault(); setExpanded(false); }
  });
  dock.querySelectorAll('.pulse-score-logo img').forEach(img => {
    const hideFailed = () => { img.hidden = true; };
    img.addEventListener('error', hideFailed, {once:true});
    if (img.complete && !img.naturalWidth) hideFailed();
  });
  const nav = document.querySelector('.league-topbar .section-nav');
  const syncNav = () => {
    const height = nav && getComputedStyle(nav).position === 'fixed' ? Math.ceil(nav.getBoundingClientRect().height) : 0;
    dock.style.setProperty('--pulse-nav-height', `${height}px`);
  };
  if (nav) new ResizeObserver(syncNav).observe(nav);
  window.addEventListener('resize', syncNav);
  syncNav();
  const syncObscured = () => {
    const obscured = !!document.querySelector('dialog[open], .timeline-player.is-expanded');
    if (dock.classList.contains('is-obscured') !== obscured) dock.classList.toggle('is-obscured', obscured);
  };
  new MutationObserver(syncObscured).observe(document.body, {
    subtree:true, attributes:true, attributeFilter:['open', 'class'],
  });
  render(expanded);
  syncObscured();
})();
