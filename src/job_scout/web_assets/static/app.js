(() => {
  document.documentElement.classList.add("js");

  if ("serviceWorker" in navigator && window.isSecureContext) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js").catch(() => {
        // PWA support is progressive; the local product remains fully usable without it.
      });
    });
  }

  document.querySelectorAll("[data-confirm]").forEach((control) => {
    control.addEventListener("click", (event) => {
      if (!window.confirm(control.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("[data-pending-form]").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector('button[type="submit"]');
      const status = form.querySelector(".pending-message");
      if (button) {
        button.disabled = true;
        button.setAttribute("aria-disabled", "true");
      }
      if (status) {
        status.hidden = false;
        status.textContent =
          form.dataset.pendingLabel || "Przetwarzanie może potrwać chwilę…";
      }
      form.setAttribute("aria-busy", "true");
    });
  });

  document.querySelectorAll(".guide-transcript").forEach((transcript) => {
    transcript.scrollTop = transcript.scrollHeight;
  });

  const scanDialog = document.querySelector("[data-scan-dialog]");
  const progress = document.querySelector("[data-scan-progress]");
  const scanLabel = document.querySelector("[data-scan-label]");
  const scanDetail = document.querySelector("[data-scan-detail]");
  const cancelScan = document.querySelector("[data-cancel-scan]");
  let scanTimer;
  const showScan = (state) => {
    if (!state) return;
    const active = state.status === "running";
    const text = active
      ? `${state.current_source || "Skanowanie"} · ${state.sources_checked}/${state.sources_total} źródeł`
      : `${state.status === "completed" ? "Skan zakończony" : state.status} · ${state.sources_checked || state.sources_ok || 0}/${state.sources_total || 0} źródeł`;
    if (scanLabel) scanLabel.textContent = text;
    const detail = `Odkryte: ${state.discovered || state.offers_saved || 0} · odrzucone tytuł: ${state.rejected_title || state.rejected_title_count || 0} · lokalizacja: ${state.rejected_location || state.rejected_location_count || 0} · błędy: ${state.errors || state.processing_error_count || 0}`;
    if (scanDetail) scanDetail.textContent = detail;
    if (progress) { progress.hidden = false; progress.textContent = `${text}. ${detail}`; }
    if (cancelScan) cancelScan.hidden = !active;
    if (!active) window.clearInterval(scanTimer);
  };
  const pollScan = async () => {
    try { const response = await fetch("/api/scans/active"); if (response.ok) showScan(await response.json()); } catch (_) {}
  };
  document.querySelectorAll("[data-scan-trigger]").forEach((button) => button.addEventListener("click", () => {
    if (button.dataset.scanMode) { scanDialog?.showModal(); } else { scanDialog?.showModal(); }
  }));
  document.querySelectorAll("[data-start-scan]").forEach((button) => button.addEventListener("click", async () => {
    try {
      const response = await fetch("/api/scans", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: button.dataset.startScan }) });
      if (!response.ok) throw new Error();
      showScan(await response.json()); scanTimer = window.setInterval(pollScan, 1500);
    } catch (_) { if (progress) { progress.hidden = false; progress.textContent = "Nie udało się uruchomić skanu. Spróbuj ponownie."; } }
  }));
  cancelScan?.addEventListener("click", async () => {
    const response = await fetch("/api/scans/active", { method: "DELETE" });
    if (response.ok && progress) progress.textContent = "Zatrzymywanie skanu — wyniki częściowe nie zostaną zapisane.";
  });
  if (document.querySelector("[data-offers-page]")) pollScan();

  document.querySelectorAll("[data-evaluate-stale]").forEach((button) => button.addEventListener("click", async () => {
    const state = document.querySelector("[data-evaluation-progress]");
    const cancelEvaluation = document.querySelector("[data-cancel-evaluation]");
    button.disabled = true;
    if (state) state.textContent = "Tworzę przebieg oceny z wybraną wersją profilu…";
    try {
      const response = await fetch("/api/evaluations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ profile_id: button.dataset.profileId }) });
      const body = await response.json(); if (!response.ok) throw new Error(body.detail);
      if (state) state.textContent = `Ocena działa w tle: ${body.run_id}.`;
      if (cancelEvaluation) cancelEvaluation.hidden = false;
    } catch (error) { if (state) state.textContent = error.message || "Nie udało się uruchomić oceny."; button.disabled = false; }
  }));
  document.querySelector("[data-cancel-evaluation]")?.addEventListener("click", async (event) => {
    const response = await fetch("/api/evaluations/active", { method: "DELETE" });
    if (response.ok) {
      event.currentTarget.hidden = true;
      const state = document.querySelector("[data-evaluation-progress]");
      if (state) state.textContent = "Ocena została anulowana; ukończone wyniki pozostają w historii.";
    }
  });

})();
