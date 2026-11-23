(function () {
  'use strict';

  /* ---------------- 现代原生 Dialog 工具（遵守 closedby="any" 与 Safari 回退） ---------------- */

  var supportsClosedBy = typeof HTMLDialogElement !== 'undefined' && 'closedBy' in HTMLDialogElement.prototype;

  function wireDialog(dlg) {
    if (!dlg || dlg._kestrelWired) return dlg;
    dlg._kestrelWired = true;
    dlg.addEventListener('close', function () {
      dlg.returnValue = '';
    });
    if (!supportsClosedBy) {
      dlg.addEventListener('click', function (e) {
        if (e.target !== dlg) return;
        var rect = dlg.getBoundingClientRect();
        var inside = (
          rect.top <= e.clientY &&
          e.clientY <= rect.top + rect.height &&
          rect.left <= e.clientX &&
          e.clientX <= rect.left + rect.width
        );
        if (!inside && typeof dlg.close === 'function') {
          dlg.close();
        }
      });
    }
    return dlg;
  }

  document.querySelectorAll('dialog').forEach(wireDialog);

  function ensureDialog() {
    var dlg = document.getElementById('app-dialog');
    if (dlg) return wireDialog(dlg);
    dlg = document.createElement('dialog');
    dlg.id = 'app-dialog';
    dlg.className = 'modal app-dialog';
    dlg.setAttribute('closedby', 'any');
    dlg.setAttribute('aria-labelledby', 'app-dialog-title');
    dlg.innerHTML =
      '<form method="dialog" class="modal-head">' +
      '<div class="modal-title-group"><h2 id="app-dialog-title" data-dialog-title></h2></div>' +
      '<button class="wb-icon-btn sm" value="cancel" aria-label="Close">' +
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>' +
      '</button></form>' +
      '<div class="modal-body"><p data-dialog-text></p></div>' +
      '<div class="modal-foot">' +
      '<button class="btn ghost" value="cancel" data-dialog-cancel></button>' +
      '<button class="btn" value="ok" data-dialog-ok></button></div>';
    document.body.appendChild(dlg);
    return wireDialog(dlg);
  }

  function ask(options) {
    var opt = options || {};
    var dlg = ensureDialog();
    var ok = dlg.querySelector('[data-dialog-ok]');
    var cancel = dlg.querySelector('[data-dialog-cancel]');
    dlg.querySelector('[data-dialog-title]').textContent = opt.title || '';
    dlg.querySelector('[data-dialog-text]').textContent = opt.text || '';
    ok.textContent = opt.okText || (window.I18N && window.I18N.okText) || '确定';
    cancel.textContent = opt.cancelText || (window.I18N && window.I18N.cancelText) || '取消';
    cancel.style.display = opt.confirm ? '' : 'none';
    return new Promise(function (resolve) {
      function onClose() {
        var accepted = dlg.returnValue === 'ok';
        dlg.removeEventListener('close', onClose);
        resolve(accepted);
      }
      dlg.addEventListener('close', onClose);
      if (typeof dlg.showModal === 'function') dlg.showModal();
      else dlg.setAttribute('open', '');
    });
  }

  window.askDialog = ask;
  window.showAlert = function (text, title) {
    return ask({ title: title || (window.I18N && window.I18N.noticeTitle) || '提示', text: text });
  };
  window.askConfirm = function (text, title) {
    return ask({
      title: title || (window.I18N && window.I18N.confirmTitle) || '确认',
      text: text,
      confirm: true,
      okText: (window.I18N && window.I18N.okText) || '确定',
      cancelText: (window.I18N && window.I18N.cancelText) || '取消'
    });
  };

  /* ---------------- 无障碍播报与轻量 Toast ---------------- */

  function announce(msg) {
    var sr = document.getElementById('ksl-sr-status');
    if (sr && msg) sr.textContent = msg;
  }

  window.showToast = function (msg) {
    if (!msg) return;
    announce(msg);
    var el = document.getElementById('ksl-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'ksl-toast';
      el.style.cssText = 'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);' +
        'background:var(--ink);color:var(--surface);padding:8px 16px;border-radius:999px;' +
        'font-size:12.5px;font-weight:550;z-index:260;box-shadow:var(--shadow-2);' +
        'pointer-events:none;transition:opacity .2s;';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.opacity = '1';
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.style.opacity = '0'; }, 2400);
  };

  /* ---------------- Cookie 辅助 ---------------- */

  function setCookie(name, value) {
    document.cookie = name + '=' + encodeURIComponent(value) + ';path=/;max-age=31536000;samesite=lax';
  }

  /* ---------------- 左侧可折叠导航栏 & 窄屏抽屉 ---------------- */

  var rootEl = document.documentElement;
  var railEl = document.getElementById('app-rail');
  var railCollapseBtn = document.getElementById('rail-collapse-btn');
  var railCollapseText = document.getElementById('rail-collapse-text');
  var mobileNavBtn = document.getElementById('mobile-nav-btn');
  var railBackdrop = document.getElementById('rail-backdrop');
  var railMobileClose = document.getElementById('rail-mobile-close');

  function isRailVisuallyCompact() {
    if (!railEl || window.innerWidth < 960) return false;
    return railEl.getBoundingClientRect().width <= 80;
  }

  function syncRailButtonLabels() {
    if (!railCollapseBtn) return;
    var compact = isRailVisuallyCompact();
    var collapseLabel = railCollapseBtn.getAttribute('data-collapse-label') || 'Collapse';
    var expandLabel = railCollapseBtn.getAttribute('data-expand-label') || 'Expand';
    var label = compact ? expandLabel : collapseLabel;
    railCollapseBtn.setAttribute('aria-label', label);
    railCollapseBtn.setAttribute('title', label);
    if (railCollapseText) railCollapseText.textContent = label;
  }

  if (railCollapseBtn) {
    railCollapseBtn.addEventListener('click', function () {
      var compact = isRailVisuallyCompact();
      var next = compact ? 'expanded' : 'collapsed';
      rootEl.setAttribute('data-rail', next);
      try { localStorage.setItem('kestrel.rail', next); } catch (e) { /* ignore */ }
      requestAnimationFrame(syncRailButtonLabels);
    });
    window.addEventListener('resize', syncRailButtonLabels, { passive: true });
    requestAnimationFrame(syncRailButtonLabels);
  }

  function setMobileDrawer(open) {
    var isOpen = !!open && window.innerWidth < 960;
    document.body.classList.toggle('rail-drawer-open', isOpen);
    if (mobileNavBtn) {
      mobileNavBtn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    }
    if (!isOpen && mobileNavBtn && document.activeElement && railEl && railEl.contains(document.activeElement)) {
      mobileNavBtn.focus();
    }
  }

  if (mobileNavBtn) {
    mobileNavBtn.addEventListener('click', function () {
      setMobileDrawer(!document.body.classList.contains('rail-drawer-open'));
    });
  }
  if (railBackdrop) {
    railBackdrop.addEventListener('click', function () { setMobileDrawer(false); });
  }
  if (railMobileClose) {
    railMobileClose.addEventListener('click', function () { setMobileDrawer(false); });
  }
  if (railEl) {
    railEl.addEventListener('click', function (e) {
      var link = e.target.closest && e.target.closest('a[href]');
      if (link && window.innerWidth < 960) setMobileDrawer(false);
    });
  }
  window.addEventListener('resize', function () {
    if (window.innerWidth >= 960 && document.body.classList.contains('rail-drawer-open')) {
      setMobileDrawer(false);
    }
  }, { passive: true });

  /* ---------------- 金额隐私模式（#toggle-privacy + Alt+P） ---------------- */

  var privBtn = document.getElementById('toggle-privacy');

  function syncPrivacyUi() {
    var on = rootEl.getAttribute('data-privacy') === 'on';
    if (privBtn) {
      privBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
      var tip = on
        ? (privBtn.getAttribute('data-tip-show') || '')
        : (privBtn.getAttribute('data-tip-hide') || '');
      if (tip) privBtn.setAttribute('title', tip);
    }
  }

  function setPrivacyMode(enabled, persist) {
    var on = !!enabled;
    rootEl.setAttribute('data-privacy', on ? 'on' : 'off');
    syncPrivacyUi();
    if (persist !== false) {
      fetch('/api/ui/privacy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: on })
      })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (res && typeof res.enabled === 'boolean') {
            rootEl.setAttribute('data-privacy', res.enabled ? 'on' : 'off');
            syncPrivacyUi();
          }
        })
        .catch(function () {});
      if (on && window.I18N && window.I18N.privMaskedHint) {
        window.showToast(window.I18N.privMaskedHint);
      }
    }
  }

  window.KestrelSetPrivacy = setPrivacyMode;

  if (privBtn) {
    syncPrivacyUi();
    privBtn.addEventListener('click', function () {
      setPrivacyMode(rootEl.getAttribute('data-privacy') !== 'on', true);
    });
  }

  /* 监听根标签 data-privacy 属性变化（例如设置页切换开关时顶栏眼睛图标实时同步） */
  if (typeof MutationObserver !== 'undefined') {
    var privObserver = new MutationObserver(syncPrivacyUi);
    privObserver.observe(rootEl, { attributes: true, attributeFilter: ['data-privacy'] });
  }

  /* ---------------- 图标化三态主题切换器（#theme-switcher） ---------------- */

  var themeBox = document.getElementById('theme-switcher');
  var colorMeta = document.querySelector('meta[name="color-scheme"]');
  var darkMql = window.matchMedia('(prefers-color-scheme: dark)');

  function applyThemeChoice(val, persist) {
    var choice = (val === 'light' || val === 'dark' || val === 'auto') ? val : 'auto';
    var resolved = choice === 'auto' ? (darkMql.matches ? 'dark' : 'light') : choice;
    rootEl.setAttribute('data-theme', choice);
    rootEl.setAttribute('data-mode', resolved);
    if (colorMeta) {
      colorMeta.content = choice === 'auto' ? 'light dark' : resolved;
    }
    if (themeBox) {
      themeBox.querySelectorAll('[data-theme-value]').forEach(function (btn) {
        var active = btn.getAttribute('data-theme-value') === choice;
        btn.classList.toggle('active', active);
        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
    }
    if (persist !== false) {
      setCookie('theme', choice);
      try { localStorage.setItem('kestrel.theme', choice); } catch (e) { /* ignore */ }
    }
  }

  if (themeBox) {
    themeBox.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('[data-theme-value]');
      if (!btn) return;
      applyThemeChoice(btn.getAttribute('data-theme-value'), true);
    });
  }

  window.addEventListener('storage', function (e) {
    if (e.key === 'kestrel.theme' && e.newValue) {
      applyThemeChoice(e.newValue, false);
    }
  });

  var legacyThemeSel = document.getElementById('theme');
  if (legacyThemeSel) {
    legacyThemeSel.addEventListener('change', function () {
      applyThemeChoice(legacyThemeSel.value, true);
    });
  }

  /* ---------------- 紧凑双语切换胶囊（#locale-toggle） ---------------- */

  function switchLocale(nextLocale) {
    var target = (nextLocale === 'en') ? 'en' : 'zh-CN';
    setCookie('locale', target);
    setCookie('kestrel_locale', target);
    fetch('/api/locale', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ locale: target })
    })
      .catch(function () {})
      .finally(function () {
        var url = new URL(window.location.href);
        if (url.searchParams.has('locale')) {
          url.searchParams.set('locale', target);
          window.location.assign(url.toString());
        } else {
          window.location.reload();
        }
      });
  }

  var localeBtn = document.getElementById('locale-toggle');
  if (localeBtn) {
    localeBtn.addEventListener('click', function () {
      var cur = String(rootEl.getAttribute('lang') || localeBtn.getAttribute('data-locale') || 'zh-CN');
      var next = cur.toLowerCase().indexOf('en') === 0 ? 'zh-CN' : 'en';
      switchLocale(next);
    });
  }

  var legacyLocaleSel = document.getElementById('locale');
  if (legacyLocaleSel) {
    legacyLocaleSel.addEventListener('change', function () {
      switchLocale(legacyLocaleSel.value);
    });
  }

  /* ---------------- 顶栏快速股票搜索直达（#quick-search-input） ---------------- */

  var quickInput = document.getElementById('quick-search-input');

  function guessMarketFromCode(code) {
    var c = String(code || '').trim().toUpperCase();
    if (/^\d{6}$/.test(c)) return 'CN';
    if (/^\d{4,5}$/.test(c)) return 'HK';
    return 'US';
  }

  function resolveFromDatalist(raw) {
    var q = String(raw || '').trim();
    if (!q) return null;
    var list = document.getElementById('fd-code-known');
    if (!list) return null;
    var upper = q.toUpperCase();
    var opts = list.querySelectorAll('option');
    for (var i = 0; i < opts.length; i++) {
      var val = String(opts[i].value || '').trim();
      var name = String(opts[i].textContent || '').trim();
      if (val.toUpperCase() === upper) {
        var parts = val.split('/');
        if (parts.length === 2) return { market: parts[0].toUpperCase(), code: parts[1] };
      }
      var segs = val.split('/');
      if (segs.length === 2 && (segs[1].toUpperCase() === upper || name === q)) {
        return { market: segs[0].toUpperCase(), code: segs[1] };
      }
    }
    return null;
  }

  function jumpToStock(raw) {
    var q = String(raw || '').trim();
    if (!q) return;
    var slashMatch = q.match(/^(CN|HK|US)\s*[/:.]\s*([A-Za-z0-9._-]+)$/i);
    if (slashMatch) {
      window.location.assign('/stock/' + slashMatch[1].toUpperCase() + '/' + encodeURIComponent(slashMatch[2].toUpperCase()));
      return;
    }
    var known = resolveFromDatalist(q);
    if (known) {
      window.location.assign('/stock/' + known.market + '/' + encodeURIComponent(known.code));
      return;
    }
    fetch('/api/journal/fill/suggest?code=' + encodeURIComponent(q))
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res && res.ok && res.market && res.code) {
          window.location.assign('/stock/' + res.market.toUpperCase() + '/' + encodeURIComponent(res.code));
        } else {
          var clean = q.toUpperCase();
          window.location.assign('/stock/' + guessMarketFromCode(clean) + '/' + encodeURIComponent(clean));
        }
      })
      .catch(function () {
        var clean = q.toUpperCase();
        window.location.assign('/stock/' + guessMarketFromCode(clean) + '/' + encodeURIComponent(clean));
      });
  }

  if (quickInput) {
    quickInput.addEventListener('change', function () {
      var v = (quickInput.value || '').trim();
      if (/^(CN|HK|US)\/[A-Za-z0-9._-]+$/i.test(v)) {
        jumpToStock(v);
      }
    });
    quickInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        jumpToStock(quickInput.value);
      } else if (e.key === 'Escape') {
        quickInput.blur();
      }
    });
  }

  /* ---------------- 全局快捷键（Alt+P 隐私打码 / 斜杠聚焦搜索 / Esc 关闭移动端抽屉） ---------------- */

  document.addEventListener('keydown', function (e) {
    if (e.altKey && !e.ctrlKey && !e.metaKey && (e.code === 'KeyP' || String(e.key || '').toLowerCase() === 'p')) {
      e.preventDefault();
      setPrivacyMode(rootEl.getAttribute('data-privacy') !== 'on', true);
      return;
    }
    if (e.key === 'Escape' && document.body.classList.contains('rail-drawer-open')) {
      setMobileDrawer(false);
      return;
    }
    if (e.key === '/' && !e.altKey && !e.ctrlKey && !e.metaKey && quickInput) {
      var tag = e.target && e.target.tagName ? e.target.tagName.toLowerCase() : '';
      var editable = tag === 'input' || tag === 'textarea' || tag === 'select' || (e.target && e.target.isContentEditable);
      if (!editable && !document.querySelector('dialog[open]')) {
        e.preventDefault();
        quickInput.focus();
        quickInput.select();
      }
    }
  });

  /* ---------------- 自选股添加与移除 ---------------- */

  var addBtn = document.getElementById('add-btn');
  var addInput = document.getElementById('add-code');
  var flash = document.getElementById('flash');

  function say(msg, kind, sticky) {
    if (!flash) return;
    flash.className = 'notice ' + (kind || '');
    flash.textContent = msg;
    flash.classList.remove('hidden');
    if (!sticky) {
      setTimeout(function () { flash.classList.add('hidden'); }, 6000);
    }
  }

  if (addBtn && addInput) {
    var submit = function () {
      var code = addInput.value.trim();
      if (!code) return;
      addBtn.disabled = true;
      addBtn.classList.add('busy');
      var originalHtml = addBtn.innerHTML;
      addBtn.innerHTML = '<span class="spin"></span>';
      fetch('/api/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: code })
      })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
        .then(function (res) {
          if (res.body.ok) {
            location.reload();
          } else {
            say((res.body && res.body.error) || window.I18N.addFailed, 'err', true);
            addBtn.disabled = false;
            addBtn.classList.remove('busy');
            addBtn.innerHTML = originalHtml;
          }
        })
        .catch(function (e) {
          say(window.I18N.networkError + e, 'err', true);
          addBtn.disabled = false;
          addBtn.classList.remove('busy');
          addBtn.innerHTML = originalHtml;
        });
    };
    addBtn.addEventListener('click', submit);
    addInput.addEventListener('keydown', function (e) { if (e.key === 'Enter') submit(); });
  }

  document.querySelectorAll('[data-remove]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var path = btn.getAttribute('data-remove');
      askConfirm(window.I18N.confirmRemove.replace('{code}', path)).then(function (yes) {
        if (!yes) return;
        fetch('/api/watchlist/' + path, { method: 'DELETE' })
          .then(function () { location.reload(); });
      });
    });
  });

  /* ---------------- 显示货币 ---------------- */

  var currencySel = document.getElementById('display-currency');
  if (currencySel) {
    currencySel.addEventListener('change', function () {
      fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: { 'ui.display_currency': currencySel.value } })
      }).then(function () { location.reload(); });
    });
  }

  /* ---------------- 行情自动刷新（保留格式化货币） ---------------- */

  var refreshBtn = document.getElementById('refresh-btn');
  var updatedAt = document.getElementById('updated-at');

  function paint(quotes) {
    quotes.forEach(function (q) {
      var row = document.querySelector('tr[data-key="' + q.market + ':' + q.code + '"]');
      if (!row) return;

      var price = row.querySelector('[data-field="price"]');
      var pct = row.querySelector('[data-field="pct"]');
      var chg = row.querySelector('[data-field="chg"]');

      if (price && (q.display_price || q.price)) {
        price.textContent = q.display_price || q.price;
      }
      if (pct) {
        if (q.change_pct === null || q.change_pct === undefined) {
          pct.textContent = '—';
          pct.className = 'num mono';
        } else {
          var v = Number(q.change_pct);
          pct.textContent = (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
          pct.className = 'num mono ' + (v >= 0 ? 'up' : 'down');
        }
      }
      if (chg && q.prev_close && q.price) {
        var delta = Number(q.price) - Number(q.prev_close);
        chg.textContent = (delta >= 0 ? '+' : '') + delta.toFixed(2);
        chg.className = 'num mono ' + (delta >= 0 ? 'up' : 'down');
      }
    });
  }

  if (refreshBtn && document.getElementById('quote-table')) {
    var timer = null;
    var LAST_KEY = 'kestrel.lastQuoteAt';

    function stamp() {
      var now = new Date();
      if (updatedAt) {
        updatedAt.textContent = (window.I18N ? window.I18N.updated : 'Updated') + ' ' +
          now.toTimeString().slice(0, 8);
      }
      try { localStorage.setItem(LAST_KEY, String(now.getTime())); } catch (e) { /* ignore */ }
    }

    function pull(manual) {
      var savedHtml = '';
      if (manual) {
        savedHtml = refreshBtn.innerHTML;
        refreshBtn.disabled = true;
        refreshBtn.classList.add('busy');
        refreshBtn.innerHTML = '<span class="spin"></span><span class="label">' +
          (window.I18N ? window.I18N.refreshing : 'Refreshing') + '</span>';
      }
      return fetch('/api/quotes')
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (res.quotes) paint(res.quotes);
          stamp();
          if (res.stale && manual) {
            say(window.I18N ? window.I18N.stale : 'Some quotes are cached.', 'warn');
          }
        })
        .catch(function (e) {
          if (manual) say((window.I18N ? window.I18N.refreshFailed : 'Refresh failed: ') + e, 'err', true);
        })
        .finally(function () {
          if (manual) {
            refreshBtn.disabled = false;
            refreshBtn.classList.remove('busy');
            refreshBtn.innerHTML = savedHtml;
          }
        });
    }

    refreshBtn.addEventListener('click', function () { pull(true); });

    var interval = (window.KESTREL_REFRESH_SECONDS || 60) * 1000;
    if (interval > 0) {
      timer = setInterval(function () {
        if (document.visibilityState === 'visible') pull(false);
      }, interval);
      document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') pull(false);
      });
      window.addEventListener('beforeunload', function () { clearInterval(timer); });
    }
  }

  /* ---------------- 标签页与分段切换（兼容 .tabs 与 .seg-group [data-tab]） ---------------- */

  var tabButtons = document.querySelectorAll('.tabs [data-tab], .seg-group button[data-tab]');
  if (tabButtons.length) {
    var activate = function (name) {
      tabButtons.forEach(function (b) {
        var active = b.getAttribute('data-tab') === name;
        b.classList.toggle('active', active);
        b.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      document.querySelectorAll('[data-pane]').forEach(function (pane) {
        pane.classList.toggle('hidden', pane.getAttribute('data-pane') !== name);
      });
      if (history.replaceState) history.replaceState(null, '', '#' + name);
    };
    tabButtons.forEach(function (tab) {
      tab.addEventListener('click', function () { activate(tab.getAttribute('data-tab')); });
    });
    var hash = location.hash.replace('#', '');
    var wanted = hash || (new URLSearchParams(location.search).get('tab') || '');
    if (wanted && document.querySelector('[data-tab="' + wanted + '"]')) {
      activate(wanted);
    }
  }

  /* ---------------- 全局自然词汇学习系统（随时悬停 / 点击看解释 + 一键开关零打扰） ---------------- */

  var glossaryCache = null;
  var glossaryLoading = null;

  function loadGlossary() {
    if (glossaryCache) return Promise.resolve(glossaryCache);
    if (!glossaryLoading) {
      glossaryLoading = fetch('/api/glossary')
        .then(function (r) { return r.json(); })
        .then(function (data) {
          glossaryCache = data || { enabled: false, items: {}, matchers: [] };
          return glossaryCache;
        })
        .catch(function () { return { enabled: false, items: {}, matchers: [] }; });
    }
    return glossaryLoading;
  }

  function escapeReg(str) {
    return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function buildMatcherRegex(matchers) {
    if (!matchers || !matchers.length) return null;
    var phraseMap = {};
    var parts = [];
    matchers.forEach(function (m) {
      var p = String(m.phrase || '').trim();
      if (!p) return;
      var lower = p.toLowerCase();
      if (!phraseMap[lower]) {
        phraseMap[lower] = m.key;
        var esc = escapeReg(p);
        // 纯英文词加边界检查，避免误伤单词内部子串
        if (/^[a-z0-9\s.+\-]+$/i.test(p)) {
          parts.push('(?<![A-Za-z0-9])' + esc + '(?![A-Za-z0-9])');
        } else {
          parts.push(esc);
        }
      }
    });
    if (!parts.length) return null;
    try {
      return { re: new RegExp('(' + parts.join('|') + ')', 'gi'), map: phraseMap };
    } catch (e) {
      return null;
    }
  }

  function clearAutoTerms() {
    hidePop(true);
    document.querySelectorAll('span.gloss-auto').forEach(function (span) {
      var parent = span.parentNode;
      if (!parent) return;
      parent.replaceChild(document.createTextNode(span.textContent), span);
      parent.normalize();
    });
  }

  function autoAnnotateTerms(root) {
    if (document.body.dataset.learning !== '1' || document.body.dataset.glossary !== '1') return Promise.resolve();
    var target = root || document.querySelector('main') || document.body;
    if (!target) return Promise.resolve();

    return loadGlossary().then(function (data) {
      if (!data || !data.enabled || !data.matchers || !data.matchers.length) return;
      var compiled = buildMatcherRegex(data.matchers);
      if (!compiled) return;

      var seenByBox = new WeakMap();
      var walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT, {
        acceptNode: function (node) {
          if (!node.nodeValue || !node.nodeValue.trim() || node.nodeValue.trim().length < 2) {
            return NodeFilter.FILTER_REJECT;
          }
          var p = node.parentElement;
          if (!p) return NodeFilter.FILTER_REJECT;
          if (p.closest('a, button, input, textarea, select, option, code, pre, script, style, svg, h1, .no-gloss, .gloss, .gloss-auto, .gloss-pop, [data-term], [data-priv]')) {
            return NodeFilter.FILTER_REJECT;
          }
          return NodeFilter.FILTER_ACCEPT;
        }
      });

      var nodes = [];
      var cur;
      while ((cur = walker.nextNode())) nodes.push(cur);

      nodes.forEach(function (textNode) {
        var parentEl = textNode.parentElement;
        if (!parentEl) return;
        var box = parentEl.closest('.card, .advice, .copilot-msg, .modal-body, section, article') || target;
        var seen = seenByBox.get(box);
        if (!seen) {
          seen = new Set();
          box.querySelectorAll('[data-term]').forEach(function (el) {
            seen.add(el.getAttribute('data-term'));
          });
          seenByBox.set(box, seen);
        }

        var text = textNode.nodeValue;
        compiled.re.lastIndex = 0;
        var frag = null;
        var lastIdx = 0;
        var m;
        while ((m = compiled.re.exec(text)) !== null) {
          var matched = m[0];
          var termKey = compiled.map[matched.toLowerCase()];
          if (!termKey || seen.has(termKey)) continue;
          seen.add(termKey);

          if (!frag) frag = document.createDocumentFragment();
          if (m.index > lastIdx) {
            frag.appendChild(document.createTextNode(text.slice(lastIdx, m.index)));
          }
          var span = document.createElement('span');
          span.className = 'gloss-auto';
          span.setAttribute('data-term', termKey);
          span.setAttribute('tabindex', '0');
          span.textContent = matched;
          frag.appendChild(span);
          lastIdx = compiled.re.lastIndex;
        }
        if (frag && textNode.parentNode) {
          if (lastIdx < text.length) {
            frag.appendChild(document.createTextNode(text.slice(lastIdx)));
          }
          textNode.parentNode.replaceChild(frag, textNode);
        }
      });
    });
  }

  window.KestrelAnnotate = autoAnnotateTerms;

  /* 悬停 / 点击气泡（带延迟关闭，鼠标移到气泡上可点按钮） */
  var pop = null;
  var hideTimer = null;
  var activeTrigger = null;

  function hidePop(immediate) {
    clearTimeout(hideTimer);
    if (!immediate) {
      hideTimer = setTimeout(function () { hidePop(true); }, 200);
      return;
    }
    if (pop && pop.parentNode) pop.parentNode.removeChild(pop);
    pop = null;
    activeTrigger = null;
  }

  function showPop(el) {
    if (document.body.dataset.learning !== '1' || document.body.dataset.glossary !== '1') return;
    var term = el.getAttribute('data-term');
    if (!term) return;
    clearTimeout(hideTimer);
    if (activeTrigger === el && pop) return;

    loadGlossary().then(function (data) {
      var item = data.items && data.items[term];
      if (!item) { hidePop(true); return; }
      hidePop(true);
      activeTrigger = el;

      pop = document.createElement('div');
      pop.className = 'gloss-pop';
      pop.setAttribute('role', 'tooltip');

      var head = document.createElement('div');
      head.className = 'gloss-pop-head';
      var title = document.createElement('b');
      title.textContent = item.term;
      var cat = document.createElement('span');
      cat.className = 'gloss-cat';
      cat.textContent = item.category_label || '';
      head.appendChild(title);
      if (item.category_label) head.appendChild(cat);

      var body = document.createElement('div');
      body.textContent = item.body;

      var actions = document.createElement('div');
      actions.className = 'gloss-pop-actions';

      var askAi = document.createElement('button');
      askAi.type = 'button';
      askAi.className = 'ask-ai';
      askAi.textContent = (window.I18N && window.I18N.askAiTerm) || '问 AI 助手';
      askAi.addEventListener('click', function () {
        var tpl = (window.I18N && window.I18N.cpAskPrompt) || '请结合我当前的账户和持仓，通俗解释「{term}」在实际操作里怎么用？';
        hidePop(true);
        if (window.openCopilot) window.openCopilot(tpl.replace('{term}', item.term));
      });

      var kill = document.createElement('button');
      kill.type = 'button';
      kill.className = 'kill';
      kill.textContent = (window.I18N && window.I18N.hideTerm) || '我已学会，不再标出';
      kill.addEventListener('click', function () {
        fetch('/glossary/' + encodeURIComponent(term) + '/toggle?ajax=1', {
          method: 'POST',
          headers: { Accept: 'application/json' }
        }).then(function () {
          glossaryCache = null;
          glossaryLoading = null;
          hidePop(true);
          document.querySelectorAll('[data-term="' + term + '"]').forEach(function (node) {
            if (node.classList.contains('gloss-auto') && node.parentNode) {
              var p = node.parentNode;
              p.replaceChild(document.createTextNode(node.textContent), node);
              p.normalize();
            } else {
              node.classList.remove('gloss');
              node.removeAttribute('data-term');
            }
          });
        });
      });

      actions.appendChild(askAi);
      actions.appendChild(kill);
      pop.appendChild(head);
      pop.appendChild(body);
      pop.appendChild(actions);

      pop.addEventListener('mouseenter', function () { clearTimeout(hideTimer); });
      pop.addEventListener('mouseleave', function () { hidePop(false); });
      document.body.appendChild(pop);

      var rect = el.getBoundingClientRect();
      var popW = pop.offsetWidth || 320;
      var popH = pop.offsetHeight || 150;
      var left = Math.min(Math.max(12, rect.left), window.innerWidth - popW - 12);
      var top = rect.bottom + 8;
      if (top + popH > window.innerHeight - 12 && rect.top - popH - 8 > 12) {
        top = rect.top - popH - 8;
      }
      pop.style.top = top + 'px';
      pop.style.left = left + 'px';
    });
  }

  document.addEventListener('mouseover', function (e) {
    var el = e.target.closest && e.target.closest('[data-term]');
    if (el) showPop(el);
  });
  document.addEventListener('mouseout', function (e) {
    var el = e.target.closest && e.target.closest('[data-term]');
    if (!el) return;
    if (pop && e.relatedTarget && pop.contains(e.relatedTarget)) return;
    hidePop(false);
  });
  document.addEventListener('click', function (e) {
    var el = e.target.closest && e.target.closest('[data-term]');
    if (el) {
      e.preventDefault();
      showPop(el);
      return;
    }
    if (pop && !pop.contains(e.target)) hidePop(true);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') hidePop(true);
    if (e.key === 'Enter' && document.activeElement && document.activeElement.hasAttribute('data-term')) {
      showPop(document.activeElement);
    }
  });
  window.addEventListener('scroll', function () { hidePop(true); }, { passive: true });

  /* 顶栏「边做边学」实时开关 */
  var learnBtn = document.getElementById('toggle-learning');
  if (learnBtn) {
    learnBtn.addEventListener('click', function () {
      var next = document.body.dataset.learning !== '1';
      fetch('/api/ui/learning', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: next })
      })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (!res.ok) return;
          var on = !!res.enabled;
          document.body.dataset.learning = on ? '1' : '0';
          learnBtn.classList.toggle('active', on);
          learnBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
          glossaryCache = null;
          glossaryLoading = null;
          document.querySelectorAll('.learn-only').forEach(function (el) {
            el.classList.toggle('hidden', !on);
            if (!on && el.tagName === 'DETAILS') el.open = false;
          });
          if (on) {
            autoAnnotateTerms(document.querySelector('main'));
            if (window.showToast && window.I18N.learnOnToast) window.showToast(window.I18N.learnOnToast);
          } else {
            clearAutoTerms();
            if (window.showToast && window.I18N.learnOffToast) window.showToast(window.I18N.learnOffToast);
          }
        });
    });
  }

  /* 初始扫描标注正文 */
  if (document.body.dataset.learning === '1') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function () { autoAnnotateTerms(document.querySelector('main')); });
    } else {
      autoAnnotateTerms(document.querySelector('main'));
    }
  }
})();
