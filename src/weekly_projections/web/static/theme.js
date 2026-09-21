(() => {
  const valid = value => ['michigan','lions','aurora','tigers','redwings','pistons'].includes(value) ? value : 'michigan';
  const metaColors = {michigan:'#00274c',lions:'#0076b6',aurora:'#091426',tigers:'#0c2340',redwings:'#ce1126',pistons:'#1d428a'};
  let theme = 'michigan';
  try { theme = valid(localStorage.getItem('wp_theme')); } catch (_) {}
  const apply = value => {
    theme = valid(value);
    document.documentElement.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = metaColors[theme];
    document.querySelectorAll('[data-theme-picker]').forEach(picker => { picker.value = theme; });
  };
  apply(theme); // In the head: apply before the first painted frame.
  document.addEventListener('DOMContentLoaded', () => {
    const label = document.createElement('label'); label.className = 'theme-picker';
    const text = document.createElement('span'); text.textContent = 'Theme';
    const select = document.createElement('select'); select.dataset.themePicker = ''; select.setAttribute('aria-label','Website color theme');
    [['michigan','Maize & blue'],['lions','Detroit Lions'],['aurora','Midnight Aurora'],['tigers','Detroit Tigers'],['redwings','Detroit Red Wings'],['pistons','Detroit Pistons']].forEach(([value, title]) => {
      const option = document.createElement('option'); option.value = value; option.textContent = title; select.append(option);
    });
    select.value = theme;
    select.addEventListener('change', () => {
      apply(select.value);
      try { localStorage.setItem('wp_theme', theme); } catch (_) {}
    });
    label.append(text, select);
    const header = document.querySelector('.topbar');
    if (header) (header.querySelector('[data-theme-slot]') || header).append(label);
    else { label.classList.add('standalone-theme-picker'); document.body.prepend(label); }
  });
  window.addEventListener('storage', event => { if (event.key === 'wp_theme') apply(event.newValue); });
})();
