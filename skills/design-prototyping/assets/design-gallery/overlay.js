/**
 * PolySimulator Design Gallery — Pin-comment overlay
 * IIFE, no dependencies, no build step required.
 * All IDs/classes prefixed with dsc-
 */
(function () {
  'use strict';

  // ─── State ─────────────────────────────────────────────────────────────────
  var commentMode = false;
  var threads     = [];       // Array of thread objects for this page
  var openPopover = null;     // { el, threadId|null }

  var PAGE = location.pathname;

  // ─── DOM bootstrap ──────────────────────────────────────────────────────────
  function ready(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  // ─── Utility ────────────────────────────────────────────────────────────────
  function esc(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function relTime(isoStr) {
    var diff = Math.floor((Date.now() - new Date(isoStr)) / 1000);
    if (diff < 60)    return 'just now';
    if (diff < 3600)  return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return Math.floor(diff / 86400) + 'd ago';
  }

  function getSavedAuthor() {
    try { return localStorage.getItem('ds_comment_author') || ''; } catch (_) { return ''; }
  }
  function saveAuthor(v) {
    try { localStorage.setItem('ds_comment_author', v); } catch (_) {}
  }

  function getSavedOwnerKey() {
    try { return localStorage.getItem('ds_owner_key') || ''; } catch (_) { return ''; }
  }
  function saveOwnerKey(v) {
    try { localStorage.setItem('ds_owner_key', v); } catch (_) {}
  }

  // ─── API calls ──────────────────────────────────────────────────────────────
  function apiGet(url, cb) {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', url);
    xhr.onload = function () {
      try { cb(null, JSON.parse(xhr.responseText)); } catch (e) { cb(e); }
    };
    xhr.onerror = function () { cb(new Error('Network error')); };
    xhr.send();
  }

  function apiPost(url, body, cb) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onload = function () {
      try { cb(null, JSON.parse(xhr.responseText)); } catch (e) { cb(e); }
    };
    xhr.onerror = function () { cb(new Error('Network error')); };
    xhr.send(JSON.stringify(body));
  }

  function apiDelete(url, cb) {
    var xhr = new XMLHttpRequest();
    xhr.open('DELETE', url);
    xhr.onload = function () {
      try { cb(null, JSON.parse(xhr.responseText)); } catch (e) { cb(e); }
    };
    xhr.onerror = function () { cb(new Error('Network error')); };
    xhr.send();
  }

  // ─── Load threads ────────────────────────────────────────────────────────────
  function loadThreads() {
    apiGet('/__c/api/threads?page=' + encodeURIComponent(PAGE), function (err, data) {
      if (err) return;
      threads = data.threads || [];
      renderAllPins();
      updateCount();
    });
  }

  // ─── Count badge ─────────────────────────────────────────────────────────────
  function updateCount() {
    var open = threads.filter(function (t) { return !t.resolved; }).length;
    var el = document.getElementById('dsc-comment-count');
    if (el) el.textContent = open;
  }

  // ─── Pin layer ───────────────────────────────────────────────────────────────
  function getPinLayer() {
    var el = document.getElementById('dsc-pin-layer');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'dsc-pin-layer';
    document.body.appendChild(el);
    return el;
  }

  function renderAllPins() {
    var layer = getPinLayer();
    // Remove existing pins
    var old = layer.querySelectorAll('.dsc-pin');
    for (var i = 0; i < old.length; i++) old[i].remove();

    // Sort: open first, then by index
    var sorted = threads.slice().sort(function (a, b) {
      if (a.resolved !== b.resolved) return a.resolved ? 1 : -1;
      return 0;
    });

    sorted.forEach(function (thread, idx) {
      renderPin(thread, idx + 1);
    });
  }

  function pinCoords(thread) {
    // xPct / yPct stored as fraction of scrollWidth/scrollHeight
    var docW = Math.max(document.body.scrollWidth,  document.documentElement.scrollWidth);
    var docH = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
    return {
      x: thread.xPct * docW,
      y: thread.yPct * docH,
    };
  }

  function renderPin(thread, num) {
    var layer = getPinLayer();
    var pin = document.createElement('div');
    pin.className = 'dsc-pin' + (thread.resolved ? ' dsc-resolved' : '');
    pin.dataset.threadId = thread.id;
    pin.textContent = num;

    var coords = pinCoords(thread);
    pin.style.left = coords.x + 'px';
    pin.style.top  = coords.y + 'px';

    pin.addEventListener('click', function (e) {
      e.stopPropagation();
      // In comment mode clicking a pin just opens the thread (no new pin)
      openThreadPopover(thread, pin);
    });

    layer.appendChild(pin);
  }

  // ─── Close any open popover ──────────────────────────────────────────────────
  function closePopover() {
    if (openPopover) {
      openPopover.el.remove();
      openPopover = null;
    }
  }

  // ─── Position a popover near a reference element or coordinate ───────────────
  function positionPopover(popEl, refX, refY) {
    // Place popover to the right and below, clamped to viewport
    var vpW = window.innerWidth;
    var vpH = window.innerHeight;
    var pw  = 308;  // slightly wider than CSS width to account for border
    var gap = 14;

    var screenX = refX - window.scrollX;
    var screenY = refY - window.scrollY;

    var left = screenX + gap;
    var top  = screenY - 40;

    // Clamp
    if (left + pw > vpW - 10)  left = screenX - pw - gap;
    if (left < 10)             left = 10;
    if (top + 340 > vpH - 10)  top  = vpH - 350;
    if (top < 10)              top  = 10;

    // Convert back to absolute (document-relative)
    popEl.style.left = (left + window.scrollX) + 'px';
    popEl.style.top  = (top  + window.scrollY) + 'px';
  }

  // ─── Thread popover ──────────────────────────────────────────────────────────
  function openThreadPopover(thread, pinEl) {
    closePopover();

    var pop = document.createElement('div');
    pop.className = 'dsc-popover';
    pop.dataset.popThread = thread.id;

    pop.innerHTML = buildThreadPopoverHTML(thread);

    document.body.appendChild(pop);

    var coords = pinCoords(thread);
    positionPopover(pop, coords.x, coords.y);

    // Wire close button
    pop.querySelector('.dsc-popover-close').addEventListener('click', function () {
      closePopover();
    });

    // Wire reply
    var replyBtn = pop.querySelector('.dsc-reply-btn');
    if (replyBtn) {
      replyBtn.addEventListener('click', function () {
        var authorEl = pop.querySelector('.dsc-reply-author');
        var textEl   = pop.querySelector('.dsc-reply-text');
        var author   = authorEl.value.trim();
        var text     = textEl.value.trim();
        if (!author || !text) return;
        saveAuthor(author);
        var replyBody = { author: author, text: text };
        var ownerKey = getSavedOwnerKey();
        if (ownerKey) replyBody.ownerKey = ownerKey;
        apiPost('/__c/api/threads/' + thread.id + '/reply', replyBody, function (err, updated) {
          if (err) return;
          var t = findThread(thread.id);
          if (t) { t.comments = updated.comments; }
          openThreadPopover(updated, pinEl);
        });
      });
    }

    // Wire resolve/reopen
    var resolveBtn = pop.querySelector('.dsc-resolve-btn');
    if (resolveBtn) {
      resolveBtn.addEventListener('click', function () {
        apiPost('/__c/api/threads/' + thread.id + '/resolve', { resolved: !thread.resolved }, function (err, updated) {
          if (err) return;
          mergeThread(updated);
          renderAllPins();
          updateCount();
          openThreadPopover(updated, pinEl);
        });
      });
    }

    // Wire delete
    var deleteBtn = pop.querySelector('.dsc-delete-btn');
    if (deleteBtn) {
      deleteBtn.addEventListener('click', function () {
        if (!confirm('Delete this thread?')) return;
        apiDelete('/__c/api/threads/' + thread.id, function (err) {
          if (err) return;
          threads = threads.filter(function (t) { return t.id !== thread.id; });
          closePopover();
          renderAllPins();
          updateCount();
        });
      });
    }

    openPopover = { el: pop, threadId: thread.id };
  }

  function buildThreadPopoverHTML(thread) {
    var commentsHTML = thread.comments.map(function (c) {
      var roleBadge = c.role === 'owner'
        ? '<span class="dsc-owner-badge">owner</span>'
        : '';
      return '<div class="dsc-comment-item">'
        + '<div class="dsc-comment-meta">'
        + '<span class="dsc-comment-author">' + esc(c.author) + '</span>'
        + roleBadge
        + '<span class="dsc-comment-time">' + relTime(c.createdAt) + '</span>'
        + '</div>'
        + '<div class="dsc-comment-text">' + esc(c.text) + '</div>'
        + '</div>';
    }).join('');

    var resolvedBadge = thread.resolved
      ? '<div class="dsc-resolved-badge">Resolved</div>'
      : '';

    var resolveLabel = thread.resolved ? 'Reopen' : 'Resolve';

    var savedAuthor = esc(getSavedAuthor());

    return '<div class="dsc-popover-header">'
      + '<span class="dsc-popover-title">Thread</span>'
      + '<button class="dsc-popover-close" aria-label="Close">'
      + '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg"><line x1="1" y1="1" x2="11" y2="11" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><line x1="11" y1="1" x2="1" y2="11" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'
      + '</button>'
      + '</div>'
      + resolvedBadge
      + '<div class="dsc-thread-list">' + commentsHTML + '</div>'
      + '<div class="dsc-form">'
      + '<input class="dsc-input dsc-reply-author" placeholder="Your name" value="' + savedAuthor + '" maxlength="80" />'
      + '<textarea class="dsc-textarea dsc-reply-text" placeholder="Reply…" maxlength="4000"></textarea>'
      + '<div class="dsc-btn-row">'
      + '<button class="dsc-btn dsc-btn-resolve dsc-resolve-btn">' + resolveLabel + '</button>'
      + '<button class="dsc-btn dsc-btn-danger dsc-delete-btn">Delete</button>'
      + '<button class="dsc-btn dsc-btn-primary dsc-reply-btn">Reply</button>'
      + '</div>'
      + '</div>';
  }

  // ─── Composer popover (new pin) ───────────────────────────────────────────────
  function openComposerPopover(xPct, yPct, absX, absY) {
    closePopover();

    var pop = document.createElement('div');
    pop.className = 'dsc-popover';
    pop.dataset.popCompose = '1';

    var savedAuthor = esc(getSavedAuthor());

    pop.innerHTML = '<div class="dsc-popover-header">'
      + '<span class="dsc-popover-title">New comment</span>'
      + '<button class="dsc-popover-close" aria-label="Close">'
      + '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg"><line x1="1" y1="1" x2="11" y2="11" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><line x1="11" y1="1" x2="1" y2="11" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'
      + '</button>'
      + '</div>'
      + '<div class="dsc-form">'
      + '<input class="dsc-input dsc-new-author" placeholder="Your name" value="' + savedAuthor + '" maxlength="80" />'
      + '<textarea class="dsc-textarea dsc-new-text" placeholder="Leave a comment…" maxlength="4000"></textarea>'
      + '<div class="dsc-btn-row">'
      + '<button class="dsc-btn dsc-cancel-btn">Cancel</button>'
      + '<button class="dsc-btn dsc-btn-primary dsc-post-btn">Post</button>'
      + '</div>'
      + '</div>';

    document.body.appendChild(pop);
    positionPopover(pop, absX, absY);

    // Focus
    setTimeout(function () {
      var authorEl = pop.querySelector('.dsc-new-author');
      if (authorEl) {
        if (authorEl.value) {
          pop.querySelector('.dsc-new-text').focus();
        } else {
          authorEl.focus();
        }
      }
    }, 50);

    // Wire close
    pop.querySelector('.dsc-popover-close').addEventListener('click', function () {
      closePopover();
    });

    // Wire cancel
    pop.querySelector('.dsc-cancel-btn').addEventListener('click', function () {
      closePopover();
    });

    // Wire post
    pop.querySelector('.dsc-post-btn').addEventListener('click', function () {
      var author = pop.querySelector('.dsc-new-author').value.trim();
      var text   = pop.querySelector('.dsc-new-text').value.trim();
      if (!author || !text) return;
      saveAuthor(author);
      var postBody = {
        page:   PAGE,
        xPct:   xPct,
        yPct:   yPct,
        author: author,
        text:   text,
      };
      var ownerKey = getSavedOwnerKey();
      if (ownerKey) postBody.ownerKey = ownerKey;
      apiPost('/__c/api/threads', postBody, function (err, thread) {
        if (err) return;
        threads.push(thread);
        closePopover();
        renderAllPins();
        updateCount();
        // Open the new thread popover
        var pinEl = document.querySelector('[data-thread-id="' + thread.id + '"]');
        openThreadPopover(thread, pinEl);
      });
    });

    openPopover = { el: pop, threadId: null };
  }

  // ─── Click handler (page clicks when in comment mode) ─────────────────────────
  function onPageClick(e) {
    if (!commentMode) return;

    // Ignore clicks on toolbar, popovers, pins
    var target = e.target;
    if (target.closest && (
      target.closest('#dsc-toolbar') ||
      target.closest('.dsc-popover') ||
      target.closest('.dsc-pin')
    )) return;

    // Compute % of document
    var docW = Math.max(document.body.scrollWidth,  document.documentElement.scrollWidth);
    var docH = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
    var absX = e.pageX;
    var absY = e.pageY;
    var xPct = absX / docW;
    var yPct = absY / docH;

    openComposerPopover(xPct, yPct, absX, absY);
  }

  // ─── Keyboard ────────────────────────────────────────────────────────────────
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      if (openPopover) {
        closePopover();
      } else if (commentMode) {
        setCommentMode(false);
      }
    }
  });

  // ─── Comment mode toggle ──────────────────────────────────────────────────────
  function setCommentMode(on) {
    commentMode = on;
    document.body.classList.toggle('dsc-comment-mode', on);
    var btn = document.getElementById('dsc-toggle-btn');
    if (btn) {
      btn.classList.toggle('dsc-active', on);
      btn.querySelector('.dsc-toggle-label').textContent = on ? 'Done' : 'Comment';
    }
  }

  // ─── Helper: find/merge thread in local array ─────────────────────────────────
  function findThread(id) {
    for (var i = 0; i < threads.length; i++) {
      if (threads[i].id === id) return threads[i];
    }
    return null;
  }

  function mergeThread(updated) {
    for (var i = 0; i < threads.length; i++) {
      if (threads[i].id === updated.id) { threads[i] = updated; return; }
    }
    threads.push(updated);
  }

  // ─── Settings panel ────────────────────────────────────────────────────────────
  var settingsOpen = false;

  function openSettingsPanel(anchorEl) {
    var existing = document.getElementById('dsc-settings-panel');
    if (existing) { existing.remove(); settingsOpen = false; return; }
    settingsOpen = true;

    var panel = document.createElement('div');
    panel.id = 'dsc-settings-panel';

    var savedAuthor   = esc(getSavedAuthor());
    var savedOwnerKey = esc(getSavedOwnerKey());

    panel.innerHTML = '<div class="dsc-settings-title">Settings</div>'
      + '<label class="dsc-settings-label">Display name</label>'
      + '<input class="dsc-input dsc-settings-name" value="' + savedAuthor + '" placeholder="Your name" maxlength="80" />'
      + '<label class="dsc-settings-label" style="margin-top:8px">Owner key</label>'
      + '<input class="dsc-input dsc-settings-key" type="password" value="' + savedOwnerKey + '" placeholder="Leave blank if not the operator" maxlength="200" />'
      + '<div class="dsc-settings-hint">Owner key marks your comments as authoritative (the operator\'s key).</div>'
      + '<div class="dsc-btn-row" style="margin-top:10px">'
      + '<button class="dsc-btn dsc-btn-primary dsc-settings-save">Save</button>'
      + '</div>';

    document.body.appendChild(panel);

    // Position above the toolbar anchor
    var rect = anchorEl.getBoundingClientRect();
    panel.style.bottom = (window.innerHeight - rect.top + 8) + 'px';
    panel.style.right  = (window.innerWidth  - rect.right)   + 'px';

    panel.querySelector('.dsc-settings-save').addEventListener('click', function () {
      var nameVal = panel.querySelector('.dsc-settings-name').value.trim();
      var keyVal  = panel.querySelector('.dsc-settings-key').value;
      saveAuthor(nameVal);
      saveOwnerKey(keyVal);
      panel.remove();
      settingsOpen = false;
    });

    // Close on outside click
    setTimeout(function () {
      document.addEventListener('click', function closeSettings(e) {
        if (!e.target.closest || (!e.target.closest('#dsc-settings-panel') && !e.target.closest('#dsc-settings-btn'))) {
          var p = document.getElementById('dsc-settings-panel');
          if (p) { p.remove(); settingsOpen = false; }
          document.removeEventListener('click', closeSettings);
        }
      });
    }, 0);
  }

  // ─── Build toolbar ────────────────────────────────────────────────────────────
  function buildToolbar() {
    var bar = document.createElement('div');
    bar.id = 'dsc-toolbar';

    var btn = document.createElement('button');
    btn.id = 'dsc-toggle-btn';
    btn.setAttribute('aria-label', 'Toggle comment mode');
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="5" cy="5" r="3.5" stroke="currentColor" stroke-width="1.5"/><circle cx="5" cy="5" r="1" fill="currentColor"/><line x1="7.5" y1="7.5" x2="12.5" y2="12.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>'
      + '<span class="dsc-toggle-label">Comment</span>';

    btn.addEventListener('click', function () {
      setCommentMode(!commentMode);
      if (!commentMode) closePopover();
    });

    var countBadge = document.createElement('span');
    countBadge.id = 'dsc-comment-count';
    countBadge.textContent = '0';

    // Settings button (gear icon)
    var settingsBtn = document.createElement('button');
    settingsBtn.id = 'dsc-settings-btn';
    settingsBtn.setAttribute('aria-label', 'Settings');
    settingsBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="7" cy="7" r="2" stroke="currentColor" stroke-width="1.4"/><path d="M7 1v1.2M7 11.8V13M1 7h1.2M11.8 7H13M2.93 2.93l.85.85M10.22 10.22l.85.85M2.93 11.07l.85-.85M10.22 3.78l.85-.85" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';

    settingsBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      openSettingsPanel(settingsBtn);
    });

    bar.appendChild(btn);
    bar.appendChild(countBadge);
    bar.appendChild(settingsBtn);
    document.body.appendChild(bar);
  }

  // ─── Wire document click ──────────────────────────────────────────────────────
  document.addEventListener('click', function (e) {
    // Close popover when clicking outside (not in comment mode)
    if (!commentMode && openPopover) {
      if (!e.target.closest || (
        !e.target.closest('.dsc-popover') &&
        !e.target.closest('.dsc-pin') &&
        !e.target.closest('#dsc-toolbar')
      )) {
        closePopover();
      }
    }
    // In comment mode, route to pin dropper
    if (commentMode) {
      onPageClick(e);
    }
  }, true);

  // ─── Init ────────────────────────────────────────────────────────────────────
  ready(function () {
    buildToolbar();
    loadThreads();
  });

}());
