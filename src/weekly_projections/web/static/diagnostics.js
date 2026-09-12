(() => {
  let reported = 0;
  const report = (kind, line = 0) => {
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
    const page = window.location.pathname;
    if (!csrf || reported >= 5 || !["/dashboard", "/lineup", "/moves", "/scores"].includes(page)) return;
    reported += 1;
    // Do not send messages, stacks, URLs, form values or any account information.
    fetch("/api/client-error", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
      body: JSON.stringify({kind, page, line: Math.min(Math.max(Number(line) || 0, 0), 1000000)})
    }).catch(() => {});
  };
  window.addEventListener("error", (event) => report("script", event.lineno));
  window.addEventListener("unhandledrejection", () => report("promise"));
})();
