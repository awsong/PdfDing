// Shared (admin-uploaded) PDF: DB-backed point comments overlay.
// - In editable mode (non-admin viewing the shared PDF):
//     users can add pins, edit/delete their own (autosave with 1.5s debounce).
// - In read-only mode (admin viewing the shared PDF):
//     all pins from all users are displayed, colored per user, click shows
//     author + content, no modification UI.

(function () {
  const AUTOSAVE_DELAY_MS = 1500;
  const STATUS_FADE_MS = 1500;

  let listUrl = '';
  let pdfId = '';
  let csrfToken = '';
  let currentUserId = null;
  let isAdmin = false;
  let canEditUI = true; // whether any editing UI is shown at all

  // Distinct, readable colors for per-user pin coloring.
  const USER_COLORS = [
    '#ea580c', '#2563eb', '#059669', '#7c3aed', '#db2777',
    '#0891b2', '#ca8a04', '#dc2626', '#4f46e5', '#65a30d',
    '#9333ea', '#0d9488', '#c026d3', '#e11d48', '#16a34a',
  ];
  const userColorCache = new Map();
  function colorForUser(id) {
    if (userColorCache.has(id)) return userColorCache.get(id);
    // simple deterministic hash
    let h = 0;
    const s = String(id);
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    const color = USER_COLORS[Math.abs(h) % USER_COLORS.length];
    userColorCache.set(id, color);
    return color;
  }

  const commentState = new Map(); // id -> { data, dirty, timer, savePromise }
  const pinEls = new Map(); // id -> pin element
  const openPopovers = new Map(); // id -> popover element

  let placingMode = false;

  function setStatus(state) {
    const el = document.getElementById('sharedCommentStatus');
    if (!el) return;
    el.classList.remove('saving', 'saved', 'error');
    if (state === 'saving') {
      el.textContent = 'Saving…';
      el.classList.add('saving');
    } else if (state === 'saved') {
      el.textContent = 'Saved';
      el.classList.add('saved');
      setTimeout(() => {
        if (el.classList.contains('saved')) {
          el.classList.remove('saved');
          el.textContent = '';
        }
      }, STATUS_FADE_MS);
    } else if (state === 'error') {
      el.textContent = 'Save failed';
      el.classList.add('error');
    } else {
      el.textContent = '';
    }
  }

  function detailUrl(id) {
    return `/pdf/shared-comments/${pdfId}/${id}/`;
  }

  async function api(method, url, body) {
    const opts = {
      method,
      headers: {
        'X-CSRFToken': csrfToken,
        'Content-Type': 'application/json',
      },
      credentials: 'same-origin',
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return await fetch(url, opts);
  }

  async function fetchAll() {
    try {
      const resp = await fetch(listUrl, { credentials: 'same-origin' });
      if (!resp.ok) return;
      const data = await resp.json();
      for (const c of data.comments || []) {
        commentState.set(c.id, { data: c, dirty: false, timer: null, savePromise: null });
      }
      renderAllPins();
    } catch (e) {
      console.error('shared_comments: fetch failed', e);
    }
  }

  function getPageEl(pageNumber) {
    return document.querySelector(`#viewer .page[data-page-number="${pageNumber}"]`);
  }

  function renderAllPins() {
    for (const [id, st] of commentState) {
      renderPin(id, st.data);
    }
  }

  function renderPin(id, c) {
    const pageEl = getPageEl(c.page);
    if (!pageEl) return;

    let pin = pinEls.get(id);
    if (!pin) {
      pin = document.createElement('div');
      pin.className = 'shared-comment-pin';
      pin.setAttribute('data-comment-id', id);
      pin.title = c.user_email;
      pin.innerHTML = `
        <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg">
          <path d="M12 2C7.58 2 4 5.58 4 10c0 5.25 7 12 8 12s8-6.75 8-12c0-4.42-3.58-8-8-8zm0 11a3 3 0 1 1 0-6 3 3 0 0 1 0 6z"/>
        </svg>`;
      pin.addEventListener('click', (ev) => {
        ev.stopPropagation();
        togglePopover(id);
      });
      pinEls.set(id, pin);
    }
    pin.style.left = (c.x * 100).toFixed(3) + '%';
    pin.style.top = (c.y * 100).toFixed(3) + '%';
    pin.style.color = colorForUser(c.user_id);
    if (pin.parentElement !== pageEl) {
      pageEl.appendChild(pin);
    }
  }

  function removePin(id) {
    const pin = pinEls.get(id);
    if (pin && pin.parentElement) pin.parentElement.removeChild(pin);
    pinEls.delete(id);
    closePopover(id);
  }

  function togglePopover(id) {
    if (openPopovers.has(id)) closePopover(id);
    else openPopover(id);
  }

  function openPopover(id) {
    const st = commentState.get(id);
    if (!st) return;
    const pin = pinEls.get(id);
    if (!pin) return;

    const pop = document.createElement('div');
    pop.className = 'shared-comment-popover';
    pop.addEventListener('click', (e) => e.stopPropagation());
    pop.style.borderLeft = `4px solid ${colorForUser(st.data.user_id)}`;

    const header = document.createElement('div');
    header.className = 'shared-comment-header';
    header.textContent = st.data.user_email;
    pop.appendChild(header);

    // Allow editing only when UI is editable AND the current user authored the comment.
    const allowEdit = canEditUI && st.data.is_mine;

    if (allowEdit) {
      const ta = document.createElement('textarea');
      ta.value = st.data.text || '';
      ta.rows = 4;
      ta.placeholder = 'Write a comment…';
      pop.appendChild(ta);
      ta.addEventListener('input', () => {
        st.data.text = ta.value;
        st.dirty = true;
        scheduleSave(id);
      });
      if (!st.data.text) ta.focus();
    } else {
      const body = document.createElement('div');
      body.className = 'shared-comment-body';
      const text = (st.data.text || '').trim();
      body.textContent = text || '(empty)';
      if (!text) body.classList.add('shared-comment-body-empty');
      pop.appendChild(body);
    }

    const footer = document.createElement('div');
    footer.className = 'shared-comment-footer';
    if (allowEdit) {
      const del = document.createElement('button');
      del.type = 'button';
      del.textContent = 'Delete';
      del.className = 'shared-comment-delete';
      del.addEventListener('click', () => deleteComment(id));
      footer.appendChild(del);
    }
    const close = document.createElement('button');
    close.type = 'button';
    close.textContent = 'Close';
    close.addEventListener('click', () => closePopover(id, true));
    footer.appendChild(close);
    pop.appendChild(footer);

    pin.appendChild(pop);
    openPopovers.set(id, pop);
  }

  function closePopover(id, viaUserAction) {
    const pop = openPopovers.get(id);
    if (pop && pop.parentElement) pop.parentElement.removeChild(pop);
    openPopovers.delete(id);

    if (viaUserAction && canEditUI) {
      const st = commentState.get(id);
      // Auto-delete empty comments authored by current user.
      if (st && st.data.is_mine && (!st.data.text || st.data.text.trim() === '')) {
        deleteComment(id, true);
      } else if (st && st.dirty) {
        flushSave(id);
      }
    }
  }

  function scheduleSave(id) {
    const st = commentState.get(id);
    if (!st) return;
    if (st.timer) clearTimeout(st.timer);
    st.timer = setTimeout(() => {
      st.timer = null;
      flushSave(id);
    }, AUTOSAVE_DELAY_MS);
  }

  async function flushSave(id) {
    const st = commentState.get(id);
    if (!st || !st.dirty) return;
    if (st.savePromise) return;

    setStatus('saving');
    const payload = { text: st.data.text };
    st.dirty = false;
    st.savePromise = api('PATCH', detailUrl(id), payload)
      .then(async (resp) => {
        if (!resp.ok) {
          setStatus('error');
          st.dirty = true;
        } else {
          const data = await resp.json();
          st.data = data;
          setStatus('saved');
        }
      })
      .catch(() => {
        setStatus('error');
        st.dirty = true;
      })
      .finally(() => {
        st.savePromise = null;
        if (st.dirty) flushSave(id);
      });
    return st.savePromise;
  }

  async function deleteComment(id, silent) {
    try {
      const resp = await api('DELETE', detailUrl(id));
      if (!resp.ok && resp.status !== 204) {
        if (!silent) setStatus('error');
        return;
      }
    } catch {
      if (!silent) setStatus('error');
      return;
    }
    commentState.delete(id);
    removePin(id);
  }

  async function createCommentAt(page, x, y) {
    setStatus('saving');
    try {
      const resp = await api('POST', listUrl, { page, x, y, text: '' });
      if (!resp.ok) {
        setStatus('error');
        return;
      }
      const data = await resp.json();
      commentState.set(data.id, { data, dirty: false, timer: null, savePromise: null });
      renderPin(data.id, data);
      setStatus('saved');
      openPopover(data.id);
    } catch {
      setStatus('error');
    }
  }

  function setPlacingMode(on) {
    placingMode = on;
    document.body.classList.toggle('shared-comment-placing', on);
    const btn = document.getElementById('sharedCommentToggle');
    if (btn) btn.classList.toggle('active', on);
  }

  function handlePageClick(ev) {
    if (!placingMode) return;
    const pageEl = ev.target.closest('#viewer .page');
    if (!pageEl) return;
    const pageNumber = parseInt(pageEl.getAttribute('data-page-number'), 10);
    if (!pageNumber) return;
    const rect = pageEl.getBoundingClientRect();
    const x = (ev.clientX - rect.left) / rect.width;
    const y = (ev.clientY - rect.top) / rect.height;
    if (x < 0 || x > 1 || y < 0 || y > 1) return;
    setPlacingMode(false);
    createCommentAt(pageNumber, x, y);
  }

  function flushAllOnUnload() {
    if (!canEditUI) return;
    for (const [id, st] of commentState) {
      if (st.dirty) {
        try {
          fetch(detailUrl(id), {
            method: 'PATCH',
            headers: {
              'X-CSRFToken': csrfToken,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({ text: st.data.text }),
            credentials: 'same-origin',
            keepalive: true,
          });
        } catch {}
      }
    }
  }

  window.init_shared_comments = function (opts) {
    pdfId = opts.pdf_id;
    listUrl = opts.list_url;
    csrfToken = opts.csrf_token;
    currentUserId = opts.user_id;
    isAdmin = !!opts.is_admin;
    canEditUI = opts.can_edit !== false;

    const eb = PDFViewerApplication.eventBus;
    eb.on('pagesinit', () => fetchAll());
    eb.on('pagerendered', (ev) => {
      for (const [id, st] of commentState) {
        if (st.data.page === ev.pageNumber) renderPin(id, st.data);
      }
    });

    document.addEventListener('click', (ev) => {
      if (placingMode) {
        handlePageClick(ev);
      } else if (!ev.target.closest('.shared-comment-pin')) {
        for (const id of Array.from(openPopovers.keys())) closePopover(id, true);
      }
    });

    const toggle = document.getElementById('sharedCommentToggle');
    if (toggle && canEditUI) {
      toggle.addEventListener('click', (ev) => {
        ev.stopPropagation();
        setPlacingMode(!placingMode);
      });
    }

    if (canEditUI) window.addEventListener('beforeunload', flushAllOnUnload);
  };
})();
