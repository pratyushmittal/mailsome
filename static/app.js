// Django owns page data and forms. These helpers only enhance the rendered HTML.
(() => {
  // These endpoints return escaped Django templates only; email bodies render in frames.
  // Fetch once per page; no extra reader state, and normal links work without JavaScript.
  function loadFragment(target) {
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

  function loadFragments() {
    document.querySelectorAll('[data-fragment]').forEach(loadFragment);
  }

  // Grow an email frame to its content, and let keys pressed inside it reach the app.
  function fitFrame(frame, onFrameKey) {
    const page = frame.contentDocument?.documentElement;
    // Browsers hide documents they treat as cross-origin; such frames keep their CSS height.
    if (!page) return;
    const fit = () => { frame.style.height = page.scrollHeight + 'px'; };
    fit();
    // Window resizes reflow the email after it loads.
    new ResizeObserver(fit).observe(page);
    // Clicking a link focuses the frame. Adding the same listener twice is a no-op.
    frame.contentDocument.addEventListener('keydown', onFrameKey);
  }

  // A key pressed inside an email acts as if pressed on its frame element.
  function frameKeyEvent(event, frame) {
    const {key, code, shiftKey, altKey, ctrlKey, metaKey, repeat, isComposing} = event;
    return {
      key, code, shiftKey, altKey, ctrlKey, metaKey, repeat, isComposing, target: frame,
      get defaultPrevented() { return event.defaultPrevented; },
      preventDefault: () => event.preventDefault(),
    };
  }

  // The email may finish loading before this script runs, so fit now and on load.
  function setupFrames(handleKey) {
    document.querySelectorAll('.email-frame').forEach(frame => {
      const onFrameKey = event => handleKey(frameKeyEvent(event, frame));
      fitFrame(frame, onFrameKey);
      frame.addEventListener('load', () => fitFrame(frame, onFrameKey));
    });
  }

  function showSearch(show) {
    const search = document.getElementById('mail-search'), toggle = document.querySelector('.search-toggle');
    search.hidden = !show;
    toggle?.setAttribute('aria-expanded', String(show));
    if (show) search.querySelector('input[type="search"]')?.focus();
    else toggle?.focus();
  }

  // A submitted search closes through its link, which reloads the unfiltered list.
  function closeSearch() {
    if (new URL(location.href).searchParams.has('q')) document.querySelector('[data-close-search]')?.click();
    else showSearch(false);
  }

  function setupSearch() {
    const search = document.getElementById('mail-search'), toggle = document.querySelector('.search-toggle');
    // Pages without the search form have nothing to enhance.
    if (!search) return;
    // Without JavaScript the search form stays visible and works as a regular GET.
    search.hidden = !search.querySelector('input[type="search"]').value;
    toggle?.setAttribute('aria-expanded', String(!search.hidden));
    toggle?.addEventListener('click', () => showSearch(search.hidden));
  }

  function highlight(row, scroll = false) {
    document.querySelector('.mail.highlighted')?.classList.remove('highlighted');
    if (!row) return; // An empty or changed list has no row to highlight.
    row.classList.add('highlighted');
    row.focus({preventScroll: true});
    if (scroll) row.scrollIntoView({block: 'nearest'});
  }

  // Each list URL remembers its selection, scroll, and row order for this browser tab.
  function storageKey(url) {
    return 'mailsome:list:' + url.pathname + url.search;
  }

  function savePosition() {
    // Storage can be disabled; normal links and fragment navigation must still work.
    try {
      sessionStorage.setItem(storageKey(location), JSON.stringify({
        row: document.querySelector('.mail.highlighted')?.id,
        scroll: window.scrollY,
        // Row order lets the reader continue to the next mail after archiving.
        rows: [...document.querySelectorAll('.mail')].map(row => [row.id, row.getAttribute('href')]),
      }));
    } catch { /* No persistent browser storage is required. */ }
  }

  function setupRows() {
    document.querySelectorAll('.mail').forEach(row => {
      row.addEventListener('focus', () => {
        document.querySelector('.mail.highlighted')?.classList.remove('highlighted');
        row.classList.add('highlighted');
      });
      row.addEventListener('click', () => { highlight(row); savePosition(); });
    });
  }

  // Reader backlinks include a row fragment. Restore exact scroll only for this same list/row.
  function restorePosition() {
    // Lists opened directly, not from a reader backlink, start at the top.
    if (!location.hash.startsWith('#mail-')) return;
    try {
      const row = document.getElementById(decodeURIComponent(location.hash.slice(1)));
      const saved = JSON.parse(sessionStorage.getItem(storageKey(location)) || 'null');
      // The row may have been archived since the backlink was created.
      if (!row?.matches('.mail')) return;
      highlight(row, true);
      if (saved?.row === row.id && Number.isFinite(saved.scroll)) window.scrollTo({top: saved.scroll});
    } catch { /* A malformed fragment or unavailable storage must not break the page. */ }
  }

  // The next mail of the list this reader was opened from.
  function nextMail() {
    try {
      const back = new URL(document.querySelector('[data-back]').getAttribute('href'), location.href);
      const rows = JSON.parse(sessionStorage.getItem(storageKey(back)) || 'null')?.rows || [];
      const index = rows.findIndex(([id]) => id === back.hash.slice(1));
      // The last row, or a reader opened outside a saved list, has no next mail.
      return index >= 0 ? rows[index + 1]?.[1] || null : null;
    } catch { return null; } // Unavailable storage keeps the ordinary return to the list.
  }

  function setupReader() {
    const archive = document.querySelector('[data-archive]');
    const next = archive && nextMail();
    if (next) {
      // Loading it now caches its conversation on the server, so it opens instantly.
      fetch(next, {priority: 'low'}).catch(() => {});
      // Archiving continues to it instead of returning to the list.
      archive.addEventListener('submit', () => { archive.querySelector('input[name="next"]').value = next; });
    }

    // Reply inline; without JavaScript the link opens the standalone reply page.
    const dialog = document.querySelector('[data-reply-dialog]');
    // Pages other than the reader have no reply.
    if (!dialog) return;
    document.querySelector('[data-reply]').addEventListener('click', event => {
      event.preventDefault();
      dialog.showModal();
      dialog.querySelector('textarea').focus();
    });
    // Closing keeps the typed reply for the next r.
    dialog.querySelector('[data-cancel]').addEventListener('click', event => { event.preventDefault(); dialog.close(); });
  }

  // Mark one feed email read and archive it in Gmail; a failed request retries on its next focus.
  function markDone(item) {
    // Archived mail, or a request in flight, needs no write.
    if (item.classList.contains('done')) return;
    const unread = item.classList.contains('unread');
    item.classList.add('done');
    item.classList.remove('unread');
    fetch(item.dataset.doneUrl, {
      method: 'POST',
      headers: {'X-CSRFToken': document.querySelector('[name="csrfmiddlewaretoken"]').value},
    })
      .then(response => { if (!response.ok) throw new Error('Not marked done'); })
      .catch(() => {
        item.classList.remove('done');
        if (unread) item.classList.add('unread');
      });
  }

  // The focused feed email is marked done after a second, so fast scrolling marks nothing.
  function focusFeedMail(item) {
    const previous = document.querySelector('.feed-mail.focused');
    // Scrolling reports an email again as it settles; keep its running timer.
    if (previous === item) return;
    previous?.classList.remove('focused');
    clearTimeout(previous?.doneTimer);
    item.classList.add('focused');
    item.doneTimer = setTimeout(() => markDone(item), 1000);
  }

  // The feed email crossing a reading line a third down the screen is in focus.
  function setupFeed() {
    const reading = new IntersectionObserver(entries => entries.forEach(entry => {
      // Emails leaving the reading line lose focus when the next one arrives.
      if (entry.isIntersecting) focusFeedMail(entry.target);
    }), {rootMargin: '-30% 0px -69% 0px'});
    document.querySelectorAll('.feed-mail').forEach(item => reading.observe(item));
  }

  // One click sends one email; a second submit would send a duplicate.
  function setupSendForms() {
    document.querySelectorAll('[data-send]').forEach(form => form.addEventListener('submit', () => {
      form.querySelector('button[type="submit"]').disabled = true;
    }));
  }

  function tabIds() {
    return [...document.querySelectorAll('.tabs .tab[data-tab-id]')].map(tab => tab.dataset.tabId);
  }

  // Move one label; Others is not in this list.
  function submitOrder(ids, from, to) {
    ids.splice(to, 0, ...ids.splice(from, 1));
    const form = document.getElementById('tab-order-form');
    form.querySelectorAll('[name="order"]').forEach(input => input.remove());
    ids.forEach(id => {
      const input = document.createElement('input');
      input.type = 'hidden'; input.name = 'order'; input.value = id;
      form.append(input);
    });
    form.requestSubmit();
  }

  function setupTabDragging() {
    let dragged = null;
    document.querySelectorAll('.tab[data-tab-id]').forEach(tab => {
      tab.addEventListener('dragstart', event => {
        dragged = tab.dataset.tabId;
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', dragged);
      });
      tab.addEventListener('dragover', event => {
        if (dragged) event.preventDefault(); // Ignore files/links dragged in from outside the tab strip.
      });
      tab.addEventListener('drop', event => {
        // Only label drags reorder tabs.
        if (!dragged) return;
        event.preventDefault();
        const ids = tabIds(), from = ids.indexOf(dragged), to = ids.indexOf(tab.dataset.tabId);
        if (from >= 0 && from !== to) submitOrder(ids, from, to);
      });
      tab.addEventListener('dragend', () => { dragged = null; });
    });
  }

  // Recent reader keys, for multi-key shortcuts such as grr.
  function keySequence() {
    let keys = '', last = 0;
    return {
      push(key) { keys = (Date.now() - last < 900 ? keys : '') + key; last = Date.now(); return keys; },
      set(value) { keys = value; },
    };
  }

  function click(event, selector) {
    event.preventDefault();
    document.querySelector(selector)?.click();
    return true;
  }

  // Each key handler returns true when it owns the key, so later handlers skip it.

  // This explicit chord works in editors too; focus an existing context field without losing its draft.
  function contextKey(event, sequence) {
    if (!(event.altKey && event.shiftKey && event.code === 'KeyC' && !event.repeat)) return false;
    sequence.set('');
    const context = document.getElementById('id_user_context');
    // Other pages open the context editor; Settings and the editor already show the field.
    if (!context) return click(event, '[data-user-context]');
    event.preventDefault();
    context.focus();
    return true;
  }

  // Forms keep native keys. Escape leaves an editor through its ordinary Cancel link.
  function editorKey(event) {
    if (!document.querySelector('[data-editor]')) return false;
    if (event.key === 'Escape') click(event, '[data-cancel]');
    return true;
  }

  // Escape closes a focused search, then returns from the reader, then closes an open search.
  function escapeKey(event, sequence) {
    if (event.key !== 'Escape') return false;
    sequence.set('');
    const search = document.getElementById('mail-search'), back = document.querySelector('[data-back]');
    if (search && !search.hidden && (event.target.closest('#mail-search') || !back)) {
      event.preventDefault();
      closeSearch();
    } else if (back) click(event, '[data-back]');
    else if (event.target.closest('.tabs')) {
      event.preventDefault();
      document.querySelector('.search-toggle')?.focus();
    }
    return true;
  }

  // Typing in fields keeps native keys and interrupts any key sequence.
  function fieldKey(event, sequence) {
    if (!event.target.closest('input, textarea, select, form') && !event.target.isContentEditable) return false;
    sequence.set('');
    return true;
  }

  // Reordering also has native move buttons on each label's edit page.
  function tabOrderKey(event) {
    if (!(event.altKey && ['ArrowLeft', 'ArrowRight'].includes(event.key) && event.target.matches('.tab[data-tab-id]'))) return false;
    event.preventDefault();
    const ids = tabIds(), from = ids.indexOf(event.target.dataset.tabId), to = from + (event.key === 'ArrowRight' ? 1 : -1);
    if (to >= 0 && to < ids.length) submitOrder(ids, from, to);
    return true;
  }

  // Remaining shortcuts are single, unmodified presses.
  function modifiedKey(event) {
    return event.altKey || event.repeat;
  }

  function searchKey(event) {
    if (event.key !== '/' || !document.getElementById('mail-search')) return false;
    event.preventDefault();
    showSearch(true);
    return true;
  }

  function composeKey(event) {
    if (event.key !== 'c' || !document.querySelector('[data-compose]')) return false;
    return click(event, '[data-compose]');
  }

  // The item j/k moves to; with nothing selected, both start at the first item.
  function neighbour(items, current, key) {
    return items[Math.max(0, Math.min(items.length - 1, items.indexOf(current) + (key === 'j' ? 1 : -1)))];
  }

  // In a feed, j/k scroll the next/previous email to the reading line.
  function feedKey(event) {
    const items = [...document.querySelectorAll('.feed-mail')];
    // Ordinary lists move their row selection instead.
    if (!items.length || event.shiftKey || !['j', 'k'].includes(event.key)) return false;
    event.preventDefault();
    const current = document.querySelector('.feed-mail.focused');
    const next = neighbour(items, current, event.key);
    // Moving on with j/k marks the email left done without waiting.
    if (current && next !== current) markDone(current);
    focusFeedMail(next);
    next.scrollIntoView({block: 'start', behavior: 'smooth'});
    return true;
  }

  function listKey(event) {
    if (document.querySelector('[data-reader]') || event.shiftKey || !['j', 'k'].includes(event.key)) return false;
    const rows = [...document.querySelectorAll('.mail')];
    // An empty list has nothing to select.
    if (!rows.length) return true;
    event.preventDefault();
    highlight(neighbour(rows, document.querySelector('.mail.highlighted'), event.key), true);
    return true;
  }

  // Focused links already implement Enter. Only page-level Enter needs a shortcut.
  function enterKey(event) {
    if (document.querySelector('[data-reader]') || event.key !== 'Enter' || event.target.closest('a, button, summary')) return false;
    const row = document.querySelector('.mail.highlighted');
    if (row) { event.preventDefault(); row.click(); }
    return true;
  }

  function readerKey(event, sequence) {
    if (!document.querySelector('[data-reader]')) return false;
    if (event.key === 'O') return click(event, '[data-reply-gmail]');
    if (event.shiftKey) return false;
    const keys = sequence.push(event.key);
    if (keys === 'grr') { sequence.set(''); return click(event, '[data-sender-all]'); }
    // The first r in grr belongs to sender history, not the reply action.
    if (keys === 'gr') return true;
    if (keys !== 'g') sequence.set(event.key === 'g' ? 'g' : '');
    if (event.key === 'd') {
      event.preventDefault();
      document.querySelector('[data-archive]')?.requestSubmit();
      return true;
    }
    const action = {r: '[data-reply]', m: '[data-sender-labels]', n: '[data-sender-note]', u: '[data-unsubscribe]'}[event.key];
    return action ? click(event, action) : false;
  }

  function tabKey(event) {
    const tabs = [...document.querySelectorAll('.tabs .tab')];
    let next;
    if (event.key === 'Tab' && (!event.target.closest('a, button, summary') || event.target.closest('.tab, .mail'))) {
      const index = tabs.findIndex(tab => tab.classList.contains('active'));
      next = tabs[(index + (event.shiftKey ? -1 : 1) + tabs.length) % tabs.length];
    } else if (!event.shiftKey && /^[1-9]$/.test(event.key)) next = tabs[Number(event.key) - 1];
    if (!next) return false;
    event.preventDefault();
    next.click();
    return true;
  }

  const keyHandlers = [contextKey, editorKey, escapeKey, fieldKey, tabOrderKey, modifiedKey, searchKey, composeKey, feedKey, listKey, enterKey, readerKey, tabKey];

  function onKey(event, sequence) {
    if (event.defaultPrevented || event.isComposing || event.ctrlKey || event.metaKey || !(event.target instanceof Element)) return;
    // An open reply keeps native keys: typing, Tab, and Escape to close it with the draft kept.
    if (document.querySelector('dialog[open]')) return;
    keyHandlers.some(handle => handle(event, sequence));
  }

  function startSyncPolling() {
    const status = document.querySelector('[data-sync-status]');
    // Pages without a connected account show no sync status.
    if (!status) return;
    const interval = Number(status.dataset.interval);
    async function poll() {
      try {
        const response = await fetch('/api/sync', {headers: {'X-Mailsome-Request': '1'}, signal: AbortSignal.timeout(5000)});
        if (response.ok) {
          const {synced_at, classifications_due} = await response.json();
          status.querySelector('[data-last-synced]').textContent = synced_at == null ? 'Not yet' : new Intl.DateTimeFormat('en-IN', {
            timeZone: 'Asia/Kolkata', dateStyle: 'medium', timeStyle: 'medium',
          }).format(new Date(synced_at)) + ' IST';
          status.querySelector('[data-classifications-due]').textContent = classifications_due;
        }
      } catch {
        // Retain the last known successful timestamp until the next periodic poll.
      }
      setTimeout(poll, interval);
    }
    setTimeout(poll, interval);
  }

  function main() {
    loadFragments();
    setupSearch();
    setupRows();
    restorePosition();
    setupReader();
    setupSendForms();
    setupFeed();
    setupTabDragging();
    const sequence = keySequence();
    const handleKey = event => onKey(event, sequence);
    setupFrames(handleKey);
    document.addEventListener('keydown', handleKey);
    startSyncPolling();
  }

  // The script is deferred, so the document is already parsed.
  main();
})();
