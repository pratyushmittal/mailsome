document.addEventListener('alpine:init', () => {
  Alpine.data('inboxApp', () => ({
    account: null,
    messages: [],
    tabs: [],
    labels: [],
    search: '',
    searchQuery: '',
    searchOpen: false,
    draggingTab: null,
    dropTarget: null,
    savingOrder: false,
    orderMessage: '',
    personDraft: '',
    ai: {enabled: false, has_key: false, reasoning: 'medium'},
    apiKey: '',
    aiSaving: false,
    aiError: '',
    aiUsage: null,
    usageLoading: false,
    usageError: '',
    labelProgress: null,
    labelTimer: null,
    reasons: [],
    activeTab: null,
    tabDraft: null,
    savingTab: false,
    tabError: '',
    listRequest: null,
    senderContext: null,
    senderView: null,
    senderEditor: null,
    noteDraft: '',
    senderLabels: [],
    actionBusy: false,
    actionError: '',
    keySequence: '',
    keyTime: 0,
    selected: null,
    listScroll: 0,
    body: '',
    bodyLoading: false,
    loading: true,
    syncing: false,
    progress: null,
    progressError: '',
    now: Date.now(),
    error: '',

    async request(path, options = {}) {
      const response = await fetch(path, {
        ...options,
        headers: {'X-Mailsome-Request': '1', 'Content-Type': 'application/json'},
      });
      const result = await response.json();
      // API failures should keep the cached inbox visible and offer a retry.
      if (!response.ok) throw new Error(result.error || 'Request failed. Please retry.');
      return result;
    },

    update(result) {
      this.account = result.account;
      this.messages = result.messages;
      // Refresh may remove the message currently being read from the inbox cache.
      if (this.selected && !this.selected.remote && !this.messages.some(mail => mail.id === this.selected.id)) {
        this.selected = null;
        this.body = '';
      }
    },

    async init() {
      try {
        const [inbox, filters] = await Promise.all([this.request('/api/inbox'), this.request('/api/tabs')]);
        this.update(inbox);
        this.tabs = filters.tabs;
        this.ai = await this.request('/api/ai-settings');
      } catch (error) {
        this.error = error.message;
      } finally {
        this.loading = false;
      }
      this.labelTimer = setInterval(() => this.pollLabels(), 2000);
      // Disconnected accounts have no credentials with which to refresh.
      if (this.account) await this.refresh();
    },

    destroy() {
      clearInterval(this.labelTimer);
      this.listRequest?.abort();
    },

    async pollLabels() {
      try {
        const progress = await this.request('/api/label-progress');
        const changed = JSON.stringify(progress) !== JSON.stringify(this.labelProgress);
        this.labelProgress = progress;
        // Label writes change local tab membership without requiring another Gmail sync.
        if (changed && !this.loading && !this.syncing && !this.selected && !this.senderView) await this.selectTab(this.activeTab, true);
      } catch (error) {
        this.labelProgress = {status: 'failed', error: 'Cannot retrieve labeling progress. Refresh to retry.'};
      }
    },

    async loadUsage(before = null) {
      // Opening Settings or clicking Load older repeatedly must not duplicate requests.
      if (this.usageLoading) return;
      this.usageLoading = true;
      this.usageError = '';
      try {
        const result = await this.request('/api/ai-usage' + (before === null ? '' : '?before=' + before));
        // Append only explicitly requested older pages; opening Settings refreshes the latest page.
        if (before !== null && this.aiUsage) result.requests = [...this.aiUsage.requests, ...result.requests];
        this.aiUsage = result;
      } catch (error) {
        this.usageError = error.message;
      } finally {
        this.usageLoading = false;
      }
    },

    costLabel(value) {
      // Null usage is unknown, not zero; tiny classification costs should remain visible.
      if (value === null || value === undefined) return 'Unknown';
      // Avoid displaying a small positive charge as a free request.
      if (value > 0 && value < 0.000001) return '<$0.000001';
      return new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 6}).format(value);
    },

    async saveAI() {
      this.aiSaving = true;
      this.aiError = '';
      try {
        this.ai = await this.request('/api/ai-settings', {
          method: 'PUT', body: JSON.stringify({enabled: this.ai.enabled, reasoning: this.ai.reasoning, api_key: this.apiKey}),
        });
        this.apiKey = '';
      } catch (error) {
        this.aiError = error.message;
      } finally {
        this.aiSaving = false;
      }
    },

    get progressLabel() {
      const completed = (this.progress?.completed ?? 0).toLocaleString();
      const total = (this.progress?.total ?? 0).toLocaleString();
      return {
        connecting: 'Connecting to Gmail…',
        listing: `Finding recent inbox messages · ${completed} found`,
        headers: `Checking message headers · ${completed} of ${total}`,
        history: `Checking mailbox changes · ${completed} pages read`,
        changes: `Updating changed messages · ${completed} of ${total}`,
        saving: 'Saving your recent inbox…',
        complete: 'Sync complete. Loading your inbox…',
      }[this.progress?.stage] || 'Waiting for the sync to start…';
    },

    get progressAge() {
      return Math.max(0, Math.floor((this.now - (this.progress?.updated_at ?? this.now)) / 1000));
    },

    get elapsed() {
      const seconds = Math.max(0, Math.floor((this.now - (this.progress?.started_at ?? this.now)) / 1000));
      return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    },

    async pollProgress(signal) {
      // Each refresh owns its polling loop, so late responses can't alter a later refresh.
      while (!signal.aborted) {
        try {
          const progress = await this.request('/api/sync-progress', {
            signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]),
          });
          // The POST can complete while this progress request is still in flight.
          if (!signal.aborted) {
            this.progress = progress;
            this.progressError = '';
          }
        } catch (error) {
          // A polling failure isn't proof that the Gmail sync itself has failed.
          if (!signal.aborted) this.progressError = 'Cannot retrieve live progress. The refresh may still be running.';
        }
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    },

    async refresh() {
      // A double click must not queue duplicate sync requests from this tab.
      if (this.syncing) return;
      this.syncing = true;
      this.error = '';
      this.progress = null;
      this.progressError = '';
      this.now = Date.now();
      const polling = new AbortController();
      const clock = setInterval(() => { this.now = Date.now(); }, 1000);
      this.pollProgress(polling.signal);
      try {
        const result = await this.request('/api/refresh', {method: 'POST'});
        this.account = result.account;
        // Refresh returns Others; a selected label or explicit search still needs its own view.
        if (this.activeTab === null && !this.searchQuery && !this.senderView) {
          this.listRequest?.abort();
          this.loading = false;
          this.update(result);
        } else await this.selectTab(this.activeTab, true);
      } catch (error) {
        this.error = error.message;
      } finally {
        polling.abort();
        clearInterval(clock);
        this.syncing = false;
      }
    },

    async selectTab(id, keepReader = false) {
      // Sender-wide results are separately paged and must not be replaced by the recent inbox.
      if (keepReader && this.senderView) return this.loadAllSender(true);
      this.senderView = null;
      this.senderEditor = null;
      this.keySequence = '';
      this.listRequest?.abort();
      const request = new AbortController();
      this.listRequest = request;
      this.activeTab = id;
      // Choosing a tab exits search; refreshes keep the submitted query and current reader.
      if (!keepReader) {
        this.selected = null;
        this.searchOpen = false;
        this.search = '';
        this.searchQuery = '';
      }
      this.messages = [];
      this.loading = true;
      this.error = '';
      try {
        const query = new URLSearchParams({q: this.searchQuery});
        // Search is independent of tab membership; only normal tab views send a tab ID.
        if (id !== null && !this.searchQuery) query.set('tab', id);
        const result = await this.request('/api/inbox?' + query, {signal: request.signal});
        // A slower tab must not overwrite a newer selection.
        if (!request.signal.aborted) this.update(result);
      } catch (error) {
        // Changing tabs aborts the previous request deliberately, not as a user-visible error.
        if (!request.signal.aborted) this.error = error.message;
      } finally {
        // The latest selection owns the list's loading state.
        if (!request.signal.aborted) this.loading = false;
      }
    },

    handleShortcut(event) {
      // Leave browser shortcuts, IME composition, and already-handled keys alone.
      if (event.defaultPrevented || event.isComposing || event.ctrlKey || event.metaKey || event.altKey || !(event.target instanceof Element)) return;
      // Editors and Settings keep normal typing and keyboard navigation, including form buttons.
      if (this.tabDraft || document.querySelector('.settings[open]')) return;
      if (event.key === 'Escape') {
        this.keySequence = '';
        if (this.senderEditor) {
          event.preventDefault();
          this.senderEditor = null;
          this.$nextTick(() => this.$refs.senderActions.focus());
          return;
        }
        if (this.searchOpen) {
          event.preventDefault();
          this.closeSearch();
        } else if (event.target.closest('.tabs')) {
          // Give keyboard users a way out of the cycling tabs and into toolbar controls.
          event.preventDefault();
          this.$refs.searchToggle.focus();
        }
        return;
      }
      // Inputs include checkboxes; contenteditable descendants also need their native keys.
      if (event.target.closest('input, textarea, select, form') || event.target.isContentEditable) {
        this.keySequence = '';
        return;
      }
      // Open sender editors retain native navigation; holding a key must not repeat an action.
      if (this.senderEditor || event.repeat) return;
      if (this.selected && !event.shiftKey) {
        const sequence = (Date.now() - this.keyTime < 900 ? this.keySequence : '') + event.key;
        this.keySequence = ['g', 'gr'].includes(sequence) ? sequence : (event.key === 'g' ? 'g' : '');
        this.keyTime = Date.now();
        if (sequence === 'grr') {
          event.preventDefault();
          this.viewSender();
          return;
        }
        if (['d', 'm', 'n', 'u'].includes(event.key)) {
          event.preventDefault();
          if (event.key === 'd') this.archiveMessage();
          else this.editSender({m: 'labels', n: 'note', u: 'unsubscribe'}[event.key]);
          return;
        }
      }
      if (event.key === '/') {
        event.preventDefault();
        this.searchOpen = true;
        this.$nextTick(() => this.$refs.searchInput.focus());
        return;
      }
      // Match visual order, including Others last, even after a drag or a tab removal.
      const ids = [...this.tabs.map(tab => tab.id), null];
      let index = -1;
      if (event.key === 'Tab') {
        // Toolbar buttons and links retain native Tab navigation rather than trapping focus.
        if (event.target.closest('a, button, summary') && !event.target.closest('.tab, .mail, .conversation')) return;
        index = (ids.indexOf(this.activeTab) + (event.shiftKey ? -1 : 1) + ids.length) % ids.length;
      } else if (!event.shiftKey && /^[1-9]$/.test(event.key)) {
        index = Number(event.key) - 1;
      }
      // Non-shortcut keys and positions beyond the current number of tabs are harmless.
      if (index < 0 || index >= ids.length) return;
      event.preventDefault();
      this.selectTab(ids[index]);
      this.$nextTick(() => {
        const button = document.getElementById(ids[index] === null ? 'others-tab' : 'label-tab-' + ids[index]);
        // An in-flight tab reload can remove the target before Alpine updates focus.
        if (!button) return;
        button.focus({preventScroll: true});
        button.scrollIntoView({block: 'nearest', inline: 'nearest'});
      });
    },

    toggleSearch() {
      // Closing the search restores the previously selected tab, not a hidden filter.
      if (this.searchOpen) return this.closeSearch();
      this.searchOpen = true;
      this.$nextTick(() => this.$refs.searchInput.focus());
    },

    submitSearch() {
      this.senderView = null;
      this.searchQuery = this.search.trim();
      this.selected = null;
      return this.selectTab(this.activeTab, true);
    },

    closeSearch() {
      this.selectTab(this.activeTab);
      this.$refs.searchToggle.focus();
    },

    startDrag(event, id) {
      // A pending save owns the order; don't start a second reorder against stale positions.
      if (this.savingOrder || this.savingTab) {
        event.preventDefault();
        return;
      }
      this.draggingTab = id;
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', String(id));
    },

    dropTab(target) {
      const id = this.draggingTab;
      this.draggingTab = null;
      this.dropTarget = null;
      // Ignore external drags, including dragged text from a message.
      if (id === null) return;
      return this.moveTab(id, this.tabs.findIndex(tab => tab.id === target));
    },

    async moveTab(id, index) {
      const from = this.tabs.findIndex(tab => tab.id === id);
      // Keyboard moves at an edge and repeated drops should be harmless no-ops.
      if (this.savingOrder || this.savingTab || from < 0 || index < 0 || index >= this.tabs.length || from === index) return;
      this.savingOrder = true;
      this.orderMessage = '';
      const previous = this.tabs;
      const ordered = [...this.tabs];
      // Move the existing object so the active tab, its color, and reader retain their identity.
      ordered.splice(index, 0, ordered.splice(from, 1)[0]);
      this.tabs = ordered;
      try {
        this.tabs = (await this.request('/api/tabs/order', {method: 'PUT', body: JSON.stringify(ordered.map(tab => tab.id))})).tabs;
        this.orderMessage = `${ordered[index].name} moved to position ${index + 1}.`;
      } catch (error) {
        this.error = error.message;
        // A different browser may have added or removed tabs; restore the server's current order.
        try {
          this.tabs = (await this.request('/api/tabs')).tabs;
        } catch {
          this.tabs = previous;
        }
        this.orderMessage = 'Tab order could not be saved.';
      } finally {
        this.savingOrder = false;
        this.$nextTick(() => {
          const button = document.getElementById('label-tab-' + id);
          // The tab may have been removed in another browser while the save was in flight.
          if (button) {
            button.focus({preventScroll: true});
            button.scrollIntoView({block: 'nearest', inline: 'nearest'});
          }
        });
      }
    },

    async editTab(tab = null) {
      this.tabDraft = tab ? {...tab, people: [...tab.people]} : {id: null, label_id: null, name: '', description: '', people: [], auto_classify: false};
      this.personDraft = '';
      this.tabError = '';
      this.$nextTick(() => document.getElementById('tab-name').focus());
      try {
        const result = await this.request('/api/labels');
        this.labels = result.labels;
        this.ai.can_label = result.can_label;
        this.tabs = (await this.request('/api/tabs')).tabs;
        // A Gmail rename keeps the same tab identity and updates its display name.
        if (this.tabDraft?.label_id) this.tabDraft.name = this.labels.find(label => label.id === this.tabDraft.label_id)?.name || this.tabDraft.name;
      } catch (error) {
        this.tabError = error.message;
      }
    },

    addPerson() {
      const email = this.personDraft.trim().toLowerCase();
      if (!email) return; // Empty chip input is normal when saving the form.
      if (!/^[^\s@<>]+@[^\s@<>]+$/.test(email)) {
        this.tabError = 'Enter a sender email address.';
        return;
      }
      this.tabDraft.people = [...new Set([...this.tabDraft.people, email])];
      this.personDraft = '';
      this.tabError = '';
    },

    async saveTab() {
      this.addPerson();
      if (this.personDraft.trim()) return; // Fix an invalid unfinished address before saving.
      this.savingTab = true;
      this.tabError = '';
      try {
        const saved = await this.request('/api/tabs' + (this.tabDraft.id === null ? '' : '/' + this.tabDraft.id), {
          method: this.tabDraft.id === null ? 'POST' : 'PUT',
          body: JSON.stringify(this.tabDraft),
        });
        this.tabs = (await this.request('/api/tabs')).tabs;
        this.tabDraft = null;
        await this.selectTab(saved.id);
      } catch (error) {
        this.tabError = error.message;
      } finally {
        this.savingTab = false;
      }
    },

    async deleteTab() {
      // Removing a saved view doesn't touch mail, but should still be an intentional choice.
      if (!window.confirm('Remove this tab and its rules? The Gmail label and your mail will be kept.')) return;
      this.savingTab = true;
      try {
        await this.request('/api/tabs/' + this.tabDraft.id, {method: 'DELETE'});
        this.tabs = (await this.request('/api/tabs')).tabs;
        this.tabDraft = null;
        await this.selectTab(null);
      } catch (error) {
        this.tabError = error.message;
      } finally {
        this.savingTab = false;
      }
    },

    async loadSenderSettings() {
      const context = this.senderContext;
      // Missing From headers cannot own a note or deterministic sender rule.
      if (!context?.email) return;
      try {
        const result = await this.request('/api/sender-settings?' + new URLSearchParams({sender: context.email}));
        Object.assign(context, result, {preferencesLoaded: true});
      } catch (error) {
        if (this.senderContext === context) this.actionError = error.message;
      }
    },

    editSender(editor) {
      // Actions wait for metadata/preferences and never overlap an in-flight save.
      if (!this.selected || this.bodyLoading || this.actionBusy || !this.senderContext?.preferencesLoaded) return;
      if (editor === 'unsubscribe' && !this.selected.unsubscribe) return;
      this.senderEditor = editor;
      this.actionError = '';
      this.noteDraft = this.senderContext.note;
      this.senderLabels = [...this.senderContext.labels];
      this.$nextTick(() => {
        const control = this.$refs.senderActions.querySelector('[data-editor="' + editor + '"] input, [data-editor="' + editor + '"] textarea, [data-editor="' + editor + '"] a');
        if (control) control.focus(); // Empty label menus may have no checkbox yet.
      });
    },

    async saveSender() {
      // Double submissions must not overwrite a newer edit.
      if (this.actionBusy) return;
      const context = this.senderContext;
      this.actionBusy = true;
      this.actionError = '';
      try {
        const values = this.senderEditor === 'note' ? {note: this.noteDraft} : {labels: this.senderLabels};
        const result = await this.request('/api/sender-settings?' + new URLSearchParams({sender: context.email}), {method: 'PUT', body: JSON.stringify(values)});
        Object.assign(context, result);
        this.tabs = (await this.request('/api/tabs')).tabs;
        if (this.senderContext === context) this.senderEditor = null;
      } catch (error) {
        if (this.senderContext === context) this.actionError = error.message;
      } finally {
        this.actionBusy = false;
      }
    },

    async archiveMessage() {
      // A held shortcut or double-click must not queue another archive.
      if (!this.selected || this.actionBusy || this.bodyLoading) return;
      const mail = this.selected;
      this.actionBusy = true;
      this.actionError = '';
      try {
        await this.request('/api/messages/' + encodeURIComponent(mail.id) + '/actions/archive', {method: 'POST'});
        // All-sender results still contain archived mail; the inbox must no longer show it.
        if (!this.senderView) this.messages = this.messages.filter(item => item.id !== mail.id);
        if (this.selected?.id === mail.id) this.closeMessage();
      } catch (error) {
        this.actionError = error.message;
      } finally {
        this.actionBusy = false;
      }
    },

    async markUnsubscribed() {
      // Label only after explicit confirmation, never merely because the external page was opened.
      if (!this.selected?.unsubscribe || this.actionBusy) return;
      const mail = this.selected;
      this.actionBusy = true;
      this.actionError = '';
      try {
        const result = await this.request('/api/messages/' + encodeURIComponent(mail.id) + '/actions/unsubscribed', {method: 'POST'});
        this.tabs = (await this.request('/api/tabs')).tabs;
        if (this.selected?.id === mail.id) {
          this.selected.labels = result.labels;
          this.senderEditor = null;
          await this.loadSenderSettings();
        }
      } catch (error) {
        this.actionError = error.message;
      } finally {
        this.actionBusy = false;
      }
    },

    viewSender() {
      // This explicit view includes archived/older messages, without adding them to SQLite.
      if (!this.selected?.sender_email) return;
      this.senderView = {email: this.selected.sender_email, next: null};
      this.selected = null;
      this.senderEditor = null;
      this.searchOpen = false;
      this.search = '';
      this.searchQuery = '';
      this.keySequence = '';
      return this.loadAllSender(true);
    },

    async loadAllSender(reset = false) {
      const view = this.senderView;
      if (!view || (!reset && this.loading)) return; // Only one pagination request owns this view.
      this.listRequest?.abort();
      const request = new AbortController();
      this.listRequest = request;
      this.loading = true;
      this.error = '';
      if (reset) this.messages = []; // Refresh replaces the page; Load more appends it.
      try {
        const query = new URLSearchParams({sender: view.email, view: 'messages', all: '1'});
        if (!reset && view.next) query.set('page', view.next);
        const result = await this.request('/api/sender-history?' + query, {signal: request.signal});
        if (!request.signal.aborted && this.senderView === view) {
          this.messages = [...new Map([...this.messages, ...result.messages].map(mail => [mail.id, mail])).values()];
          view.next = result.next_page;
        }
      } catch (error) {
        if (!request.signal.aborted) this.error = error.message;
      } finally {
        if (!request.signal.aborted) this.loading = false;
      }
    },

    get previousConversations() {
      return (this.senderContext?.messages || []).filter(mail => mail.id !== this.selected?.id).slice(0, 5);
    },

    async loadSenderHistory() {
      const context = this.senderContext;
      // No usable sender or an in-flight page means there is nothing new to request yet.
      if (!context?.email || context.loading) return;
      context.loading = true;
      context.error = '';
      try {
        const query = new URLSearchParams({sender: context.email, view: 'messages'});
        // Subsequent pages are fetched only when the user asks for more conversations.
        if (context.next) query.set('page', context.next);
        const result = await this.request('/api/sender-history?' + query);
        // Update this sender's own object, never another sender selected during the request.
        // Deduplicate messages because Gmail can change between pagination requests.
        context.messages = [...new Map([...(context.messages || []), ...result.messages].map(mail => [mail.id, mail])).values()];
        context.next = result.next_page;
      } catch (error) {
        context.error = error.message;
      } finally {
        context.loading = false;
      }
    },

    closeMessage() {
      this.selected = null;
      this.senderEditor = null;
      this.keySequence = '';
      this.body = '';
      this.$nextTick(() => window.scrollTo({top: this.listScroll}));
    },

    async openMessage(mail, remote = false) {
      // Preserve the inbox position when expanding a row into the reader.
      if (!this.selected) this.listScroll = window.scrollY;
      window.scrollTo({top: 0});
      this.selected = {...mail, remote, unsubscribe: ''};
      this.senderEditor = null;
      this.actionError = '';
      this.keySequence = '';
      this.body = '';
      this.reasons = [];
      this.bodyLoading = true;
      this.error = '';
      const senderChanged = this.senderContext?.email !== mail.sender_email;
      // Reuse loaded context; null messages means this sender's first page hasn't loaded yet.
      if (senderChanged) {
        this.senderContext = {email: mail.sender_email, messages: mail.sender_email ? null : [], next: null, loading: false, error: '', note: '', preferencesLoaded: false};
      }
      try {
        const result = await this.request((remote ? '/api/history/messages/' : '/api/messages/') + encodeURIComponent(mail.id));
        // A slower response must not replace a different message selected meanwhile.
        if (this.selected?.id === mail.id) {
          this.body = result.body;
          this.reasons = result.reasons || [];
          this.selected.labels = result.labels || mail.labels;
          this.selected.unsubscribe = result.unsubscribe || '';
        }
      } catch (error) {
        // Ignore a failure from a reader the user has already left.
        if (this.selected?.id === mail.id) this.error = error.message;
      } finally {
        // Only the active reader owns its loading indicator.
        if (this.selected?.id === mail.id) {
          this.bodyLoading = false;
          // Prioritize the message body before sidebar requests acquire the Gmail lock.
          if (senderChanged) this.loadSenderHistory();
          this.loadSenderSettings();
          // Resolve unpinned Gmail label names once, without blocking the message body.
          if (!this.labels.length) this.request('/api/labels').then(result => { this.labels = result.labels; }).catch(() => {});
        }
      }
    },
  }));
  Alpine.data('oauthSetup', () => ({
    uploading: false,
    error: '',

    async upload() {
      // Ignore repeated submits while the first upload is still in flight.
      if (this.uploading) return;
      const file = document.getElementById('credentials').files[0];
      // Native required validation normally catches this; keep programmatic submits safe too.
      if (!file) {
        this.error = 'Choose your downloaded Google client JSON file.';
        return;
      }
      // Reject large files before reading or sending them; the server also enforces this limit.
      if (file.size > 64 * 1024) {
        this.error = 'Choose the Google client JSON file (maximum 64 KB).';
        return;
      }
      this.uploading = true;
      this.error = '';
      try {
        const response = await fetch('/api/oauth-credentials', {
          method: 'POST',
          headers: {'X-Mailsome-Request': '1', 'Content-Type': 'application/json'},
          body: file,
        });
        const result = await response.json();
        // Invalid files stay on this page so the owner can correct them and retry.
        if (!response.ok) throw new Error(result.error || 'Upload failed. Please retry.');
        window.location.assign('/auth/connect');
      } catch (error) {
        this.error = error.message;
        this.uploading = false;
      }
    },
  }));
});
