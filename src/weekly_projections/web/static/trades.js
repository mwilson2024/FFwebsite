(() => {
  const partnerForm = document.querySelector('.trade-selectors');
  partnerForm?.querySelector('select[name="target"]')?.addEventListener('change', () => partnerForm.requestSubmit());
  partnerForm?.addEventListener('submit', () => {
    const button = partnerForm.querySelector('button');
    button.textContent = 'Loading team…'; button.disabled = true;
  });
  document.querySelector('.trade-selectors select[name="league"]')?.addEventListener('change', () => {
    document.querySelector('.trade-selectors select[name="target"]').value = '';
  });
  const form = document.querySelector('#trade-builder');
  if (!form) return;
  const button = form.querySelector('#review-trade');
  const update = () => {
    const counts = ['give', 'receive'].map(side => {
      const count = form.querySelectorAll(`input[name="${side}"]:checked`).length;
      form.querySelector(`[data-count="${side}"]`).textContent = `${count} selected`;
      return count;
    });
    button.disabled = counts.some(count => !count);
    form.querySelector('#trade-selection-status').textContent = button.disabled
      ? 'Choose at least one player from each team.'
      : `You send ${counts[0]} and receive ${counts[1]}. Ready to review.`;
  };
  form.addEventListener('change', update);
  form.addEventListener('submit', event => {
    update();
    if (button.disabled) event.preventDefault();
  });
  update();
})();
