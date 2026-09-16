// Django owns page data and forms. These helpers only enhance the rendered HTML.
document.addEventListener('DOMContentLoaded', () => {
  // These endpoints return escaped Django templates or nh3-sanitized email fragments only.
  // Fetch once per page; no extra reader state, and normal links work without JavaScript.
  for (const target of document.querySelectorAll('[data-fragment]')) {
    fetch(target.dataset.fragment, {signal: AbortSignal.timeout(30000)})
      .then(async response => {
        // Keep the fallback and its retry link on provider failures, not an error page fragment.
        if (!response.ok || !response.headers.get('Content-Type')?.startsWith('text/html')) throw new Error('Content unavailable');
        target.innerHTML = await response.text();
      })
      .catch(() => {
        const notice = document.createElement('p');
        notice.textContent = 'Could not load this section. Use its link to retry.';
        target.prepend(notice);
      })
      .finally(() => target.setAttribute('aria-busy', 'false'));
  }

  const search = document.getElementById('mail-search');
  const searchToggle = document.querySelector('.search-toggle');
  let sequence = '';
  let lastKey = 0;
  let draggedTab = null;

  function showSearch(show) {
    search.hidden = !show;
    searchToggle?.setAttribute('aria-expanded', String(show));
    if (show) search.querySelector('input[type="search"]')?.focus();
    else searchToggle?.focus();
  }

  // Without JavaScript the search form stays visible and works as a regular GET.
  if (search) {
    search.hidden = !search.querySelector('input[type="search"]').value;
    searchToggle?.setAttribute('aria-expanded', String(!search.hidden));
    searchToggle?.addEventListener('click', () => showSearch(search.hidden));
  }

  function highlight(row, scroll = false) {
    document.querySelector('.mail.highlighted')?.classList.remove('highlighted');
    if (!row) return; // An empty or changed list has no row to highlight.
    row.classList.add('highlighted');
    row.focus({preventScroll: true});
    if (scroll) row.scrollIntoView({block: 'nearest'});
  }

  function savePosition() {
    // Storage can be disabled; normal links and fragment navigation must still work.
    try {
      sessionStorage.setItem('mailsome:list:' + location.pathname + location.search, JSON.stringify({
        row: document.querySelector('.mail.highlighted')?.id,
        scroll: window.scrollY,
      }));
    } catch { /* No persistent browser storage is required. */ }
  }

  document.querySelectorAll('.mail').forEach(row => {
    row.addEventListener('focus', () => {
      document.querySelector('.mail.highlighted')?.classList.remove('highlighted');
      row.classList.add('highlighted');
    });
    row.addEventListener('click', () => { highlight(row); savePosition(); });
  });

  // Reader backlinks include a row fragment. Restore exact scroll only for this same list/row.
  if (location.hash.startsWith('#mail-')) {
    try {
      const row = document.getElementById(decodeURIComponent(location.hash.slice(1)));
      const saved = JSON.parse(sessionStorage.getItem('mailsome:list:' + location.pathname + location.search) || 'null');
      if (row?.matches('.mail')) {
        highlight(row, true);
        if (saved?.row === row.id && Number.isFinite(saved.scroll)) window.scrollTo({top: saved.scroll});
      }
    } catch { /* A malformed fragment or unavailable storage must not break the page. */ }
  }

  function submitOrder(ids) {
    const form = document.getElementById('tab-order-form');
    form.querySelectorAll('[name="order"]').forEach(input => input.remove());
    ids.forEach(id => {
      const input = document.createElement('input');
      input.type = 'hidden'; input.name = 'order'; input.value = id;
      form.append(input);
    });
    form.requestSubmit();
  }

  document.querySelectorAll('.tab[data-tab-id]').forEach(tab => {
    tab.addEventListener('dragstart', event => {
      draggedTab = tab.dataset.tabId;
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', draggedTab);
    });
    tab.addEventListener('dragover', event => {
      if (draggedTab) event.preventDefault(); // Ignore files/links dragged in from outside the tab strip.
    });
    tab.addEventListener('drop', event => {
      if (!draggedTab) return;
      event.preventDefault();
      const ids = [...document.querySelectorAll('.tab[data-tab-id]')].map(item => item.dataset.tabId);
      const from = ids.indexOf(draggedTab), to = ids.indexOf(tab.dataset.tabId);
      if (from < 0 || from === to) return;
      ids.splice(to, 0, ...ids.splice(from, 1)); // Move only the dragged label; Others is not in this list.
      submitOrder(ids);
    });
    tab.addEventListener('dragend', () => { draggedTab = null; });
  });

  document.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.isComposing || event.ctrlKey || event.metaKey || !(event.target instanceof Element)) return;
    const target = event.target;
    // This explicit chord works in editors too; focus an existing context field without losing its draft.
    if (event.altKey && event.shiftKey && event.code === 'KeyC' && !event.repeat) {
      event.preventDefault(); sequence = '';
      const context = document.getElementById('id_user_context');
      if (context) context.focus();
      else document.querySelector('[data-user-context]')?.click();
      return;
    }
    // Forms keep native keys. Escape leaves an editor through its ordinary Cancel link.
    if (document.querySelector('[data-editor]')) {
      if (event.key === 'Escape') { event.preventDefault(); document.querySelector('[data-cancel]')?.click(); }
      return;
    }
    if (event.key === 'Escape') {
      sequence = '';
      if (target.closest('#mail-search') && !search.hidden) {
        event.preventDefault();
        if (new URL(location.href).searchParams.has('q')) search.querySelector('[data-close-search]')?.click();
        else showSearch(false);
      } else if (document.querySelector('[data-back]')) {
        event.preventDefault(); document.querySelector('[data-back]').click();
      } else if (search && !search.hidden) {
        event.preventDefault();
        if (new URL(location.href).searchParams.has('q')) search.querySelector('[data-close-search]')?.click();
        else showSearch(false);
      } else if (target.closest('.tabs')) {
        event.preventDefault(); searchToggle?.focus();
      }
      return;
    }
    if (target.closest('input, textarea, select, form') || target.isContentEditable) { sequence = ''; return; }
    const tabs = [...document.querySelectorAll('.tabs .tab')];
    // Reordering also has native move buttons on each label's edit page.
    if (event.altKey && ['ArrowLeft', 'ArrowRight'].includes(event.key) && target.matches('.tab[data-tab-id]')) {
      event.preventDefault();
      const ids = tabs.filter(tab => tab.dataset.tabId).map(tab => tab.dataset.tabId);
      const index = ids.indexOf(target.dataset.tabId), to = index + (event.key === 'ArrowRight' ? 1 : -1);
      if (to >= 0 && to < ids.length) { ids.splice(to, 0, ...ids.splice(index, 1)); submitOrder(ids); }
      return;
    }
    if (event.altKey || event.repeat) return;
    if (event.key === '/' && search) { event.preventDefault(); showSearch(true); return; }
    const reader = document.querySelector('[data-reader]');
    if (!reader && !event.shiftKey && ['j', 'k'].includes(event.key)) {
      const rows = [...document.querySelectorAll('.mail')];
      if (!rows.length) return;
      event.preventDefault();
      const index = rows.indexOf(document.querySelector('.mail.highlighted'));
      const next = index < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, index + (event.key === 'j' ? 1 : -1)));
      highlight(rows[next], true);
      return;
    }
    // Focused links already implement Enter. Only page-level Enter needs a shortcut.
    if (!reader && event.key === 'Enter' && !target.closest('a, button, summary')) {
      const row = document.querySelector('.mail.highlighted');
      if (row) { event.preventDefault(); row.click(); }
      return;
    }
    if (reader && !event.shiftKey) {
      sequence = (Date.now() - lastKey < 900 ? sequence : '') + event.key;
      lastKey = Date.now();
      if (sequence === 'grr') { event.preventDefault(); document.querySelector('[data-sender-all]')?.click(); sequence = ''; return; }
      // The first r in grr belongs to sender history, not the Gmail reply action.
      if (sequence === 'gr') return;
      if (!['g', 'gr'].includes(sequence)) sequence = event.key === 'g' ? 'g' : '';
      if (event.key === 'd') { event.preventDefault(); document.querySelector('[data-archive]')?.requestSubmit(); return; }
      const action = {r: '[data-reply-gmail]', m: '[data-sender-labels]', n: '[data-sender-note]', u: '[data-unsubscribe]'}[event.key];
      if (action) { event.preventDefault(); document.querySelector(action)?.click(); return; }
    }
    let nextTab;
    if (event.key === 'Tab' && (!target.closest('a, button, summary') || target.closest('.tab, .mail'))) {
      const index = tabs.findIndex(tab => tab.classList.contains('active'));
      nextTab = tabs[(index + (event.shiftKey ? -1 : 1) + tabs.length) % tabs.length];
    } else if (!event.shiftKey && /^[1-9]$/.test(event.key)) nextTab = tabs[Number(event.key) - 1];
    if (nextTab) { event.preventDefault(); nextTab.click(); }
  });

  const progress = document.querySelector('[data-progress]');
  // Connected pages keep a normal reload link even when JavaScript is unavailable.
  if (progress) {
    function schedulePoll() {
      // Readers and editors stop polling as soon as their active work finishes.
      if (progress.dataset.active === 'true') setTimeout(poll, 1000);
      else if (progress.dataset.pollIdle === 'true' && !document.querySelector('[data-reader], [data-editor]')) setTimeout(poll, 15000);
    }

    async function poll() {
      try {
        const response = await fetch('/api/progress', {headers: {'X-Mailsome-Request': '1'}, signal: AbortSignal.timeout(5000)});
        // Failed requests must not replace the last known progress or page revision.
        if (!response.ok) throw new Error('Progress unavailable');
        const status = await response.json();
        for (const [kind, selector] of [['sync', '[data-sync-status]'], ['labeling', '[data-label-status]']]) {
          const work = status[kind];
          const line = progress.querySelector(selector);
          line.textContent = work.error || `${kind === 'sync' ? 'Sync' : 'Labeling'}: ${work.status} · ${work.stage || ''} ${work.completed ?? 0}${work.total == null ? '' : ' / ' + work.total}`;
          line.hidden = !['queued', 'running', 'failed'].includes(work.status);
        }
        progress.querySelector('[data-progress-error]').hidden = true;
        progress.dataset.active = String([status.sync.status, status.labeling.status].some(value => ['queued', 'running'].includes(value)));
        // Keep the rendered revision: a notification never replaces mail or navigates away from typing.
        progress.querySelector('[data-mail-updated]').hidden = String(status.revision ?? '') === progress.dataset.revision;
      } catch {
        const error = progress.querySelector('[data-progress-error]');
        error.textContent = 'Cannot retrieve progress. Work may still be running; reload to check.';
        error.hidden = false;
      }
      schedulePoll();
    }
    schedulePoll();
  }
});
