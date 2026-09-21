(() => {
  const valid = value => ['michigan','lions','aurora','tigers'].includes(value) ? value : 'michigan';
  let theme = 'michigan';
  try { theme = valid(localStorage.getItem('wp_theme')); } catch (_) {}
  const apply = value => {
    theme = valid(value);
    document.documentElement.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme === 'lions' ? '#0076b6' : theme === 'aurora' ? '#091426' : theme === 'tigers' ? '#0c2340' : '#00274c';
    document.querySelectorAll('[data-theme-picker]').forEach(picker => { picker.value = theme; });
  };
  apply(theme); // In the head: apply before the first painted frame.
  document.addEventListener('DOMContentLoaded', () => {
    const label = document.createElement('label'); label.className = 'theme-picker';
    const text = document.createElement('span'); text.textContent = 'Theme';
    const select = document.createElement('select'); select.dataset.themePicker = ''; select.setAttribute('aria-label','Website color theme');
    [['michigan','Maize & blue'],['lions','Detroit Lions'],['aurora','Midnight Aurora'],['tigers','Detroit Tigers']].forEach(([value, title]) => {
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
