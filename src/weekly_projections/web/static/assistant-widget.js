(() => {
  const MAX_CONTEXT = 12000;

  function pageContext() {
    const source = document.querySelector('main');
    return (source?.innerText || '')
      .replace(/\u0000/g, '')
      .replace(/[ \t]+\n/g, '\n')
      .trim()
      .slice(0, MAX_CONTEXT);
  }

  document.querySelectorAll('[data-assistant-context-form]').forEach((form) => {
    const choice = form.querySelector('[data-assistant-include-context]');
    const preview = form.querySelector('[data-assistant-context-preview]');
    const refreshPreview = () => {
      const value = choice?.checked === false ? '' : pageContext();
      if (preview) preview.textContent = value || 'No page context will be sent.';
      return value;
    };
    form.querySelector('.assistant-context-preview')?.addEventListener('toggle', refreshPreview);
    choice?.addEventListener('change', refreshPreview);
    form.addEventListener('submit', () => {
      form.elements.page_title.value = document.title.slice(0, 200);
      form.elements.page_path.value = window.location.pathname.slice(0, 240);
      form.elements.page_context.value = refreshPreview();
    });
  });

  document.querySelectorAll('[data-assistant-widget]').forEach((widget) => {
    const opener = widget.querySelector('[data-assistant-open]');
    const closer = widget.querySelector('[data-assistant-close]');
    const drawer = widget.querySelector('.assistant-drawer');
    const form = widget.querySelector('[data-assistant-widget-form]');
    const transcript = widget.querySelector('[data-assistant-transcript]');
    const status = widget.querySelector('[data-assistant-status]');
    const textarea = form?.querySelector('textarea');
    const submit = form?.querySelector('button[type="submit"]');
    const contextChoice = form?.querySelector('[data-assistant-include-context]');
    const contextPreview = form?.querySelector('[data-assistant-context-preview]');
    const refreshPreview = () => {
      const value = contextChoice?.checked === false ? '' : pageContext();
      if (contextPreview) contextPreview.textContent = value || 'No page context will be sent.';
      return value;
    };

    const close = () => {
      drawer.hidden = true;
      opener.setAttribute('aria-expanded', 'false');
      opener.focus();
    };
    opener.addEventListener('click', () => {
      drawer.hidden = false;
      opener.setAttribute('aria-expanded', 'true');
      textarea?.focus();
      if (transcript) transcript.scrollTop = transcript.scrollHeight;
      refreshPreview();
    });
    closer?.addEventListener('click', close);
    drawer.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') close();
    });

    if (!form || !transcript || !textarea || !submit) return;

    const addMessage = (role, content) => {
      transcript.querySelector('[data-assistant-empty]')?.remove();
      const article = document.createElement('article');
      article.className = `assistant-message ${role}`;
      const label = document.createElement('span');
      label.textContent = role === 'user' ? 'You' : 'Fantasy HQ AI';
      const message = document.createElement('p');
      message.textContent = content;
      article.append(label, message);
      transcript.append(article);
      transcript.scrollTop = transcript.scrollHeight;
    };

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const message = textarea.value.trim();
      if (!message) return;
      submit.disabled = true;
      textarea.disabled = true;
      status.textContent = 'Thinking…';
      try {
        const response = await fetch('/api/assistant/message', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || '',
          },
          body: JSON.stringify({
            league: widget.dataset.leagueId,
            message,
            page_title: document.title.slice(0, 200),
            page_path: window.location.pathname.slice(0, 240),
            page_context: refreshPreview(),
          }),
        });
        let result = {};
        try { result = await response.json(); } catch (_) { /* safe generic error below */ }
        if (!response.ok) throw new Error(result.detail || 'The assistant is unavailable right now.');
        addMessage('user', message);
        addMessage('assistant', result.answer);
        textarea.value = '';
        status.textContent = '';
      } catch (error) {
        status.textContent = error.message || 'The assistant is unavailable right now.';
      } finally {
        submit.disabled = false;
        textarea.disabled = false;
        textarea.focus();
      }
    });
    form.querySelector('.assistant-context-preview')?.addEventListener('toggle', refreshPreview);
    contextChoice?.addEventListener('change', refreshPreview);
  });
})();
