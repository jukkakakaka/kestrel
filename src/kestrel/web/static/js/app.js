(function () {
  'use strict';

  /* 全站对话框。
   *
   * 浏览器原生的 alert() / confirm() 不受主题控制、无法本地化排版，也挡不住
   * 误触。这里提供两个等价的 Promise 版本，所有页面用自己的 CSS 弹窗，
   * 所以深色模式和英文界面都是一致的。
   */
  function ensureDialog() {
    var dlg = document.getElementById('app-dialog');
    if (dlg) return dlg;
    dlg = document.createElement('dialog');
    dlg.id = 'app-dialog';
    dlg.className = 'modal app-dialog';
    dlg.innerHTML =
      '<form method="dialog" class="modal-head"><h2 data-dialog-title></h2>' +
      '<button class="btn ghost sm" value="cancel">✕</button></form>' +
      '<div class="modal-body"><p data-dialog-text></p></div>' +
      '<div class="modal-foot">' +
      '<button class="btn ghost" value="cancel" data-dialog-cancel></button>' +
      '<button class="btn" value="ok" data-dialog-ok></button></div>';
    document.body.appendChild(dlg);
    return dlg;
  }

  function ask(options) {
    var opt = options || {};
    var dlg = ensureDialog();
    var ok = dlg.querySelector('[data-dialog-ok]');
    var cancel = dlg.querySelector('[data-dialog-cancel]');
    dlg.querySelector('[data-dialog-title]').textContent = opt.title || '';
    dlg.querySelector('[data-dialog-text]').textContent = opt.text || '';
    ok.textContent = opt.okText || '确定';
    cancel.textContent = opt.cancelText || '取消';
    cancel.style.display = opt.confirm ? '' : 'none';
    return new Promise(function (resolve) {
      function done(value) { dlg.removeEventListener('close', onClose); resolve(value); }
      function onClose() { done(dlg.returnValue === 'ok'); }
      dlg.addEventListener('close', onClose);
      if (typeof dlg.showModal === 'function') dlg.showModal();
      else dlg.setAttribute('open', '');
    });
  }

  window.askDialog = ask;
  window.showAlert = function (text, title) {
    return ask({ title: title || (window.I18N && window.I18N.notice) || '提示', text: text });
  };
  window.askConfirm = function (text, title) {
    return ask({
      title: title || (window.I18N && window.I18N.confirmTitle) || '确认',
      text: text,
      confirm: true,
      okText: (window.I18N && window.I18N.ok) || '确定',
      cancelText: (window.I18N && window.I18N.cancel) || '取消'
    });
  };

  function setCookie(name, value) {
    document.cookie = name + '=' + encodeURIComponent(value) + ';path=/;max-age=31536000;samesite=lax';
  }

  var localeSel = document.getElementById('locale');
  if (localeSel) {
    localeSel.addEventListener('change', function () {
      setCookie('locale', localeSel.value);
      location.reload();
    });
  }

  var themeSel = document.getElementById('theme');
  if (themeSel) {
    themeSel.addEventListener('change', function () {
      setCookie('theme', themeSel.value);
      location.reload();
    });
  }

  /* ---------------- watchlist ---------------- */

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
      var originalBtn = addBtn.textContent;
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
            addBtn.textContent = originalBtn;
          }
        })
        .catch(function (e) {
          say(window.I18N.networkError + e, 'err', true);
          addBtn.disabled = false;
          addBtn.classList.remove('busy');
          addBtn.textContent = originalBtn;
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

  /* ---------------- quotes refresh ---------------- */

  var refreshBtn = document.getElementById('refresh-btn');
  var updatedAt = document.getElementById('updated-at');

  function paint(quotes) {
    quotes.forEach(function (q) {
      var row = document.querySelector('tr[data-key="' + q.market + ':' + q.code + '"]');
      if (!row) return;

      var price = row.querySelector('[data-field="price"]');
      var pct = row.querySelector('[data-field="pct"]');
      var chg = row.querySelector('[data-field="chg"]');

      if (price && q.price) price.textContent = q.price;
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

  if (refreshBtn) {
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
      if (manual) {
        var label = refreshBtn.querySelector('.label') || refreshBtn;
        var original = label.textContent;
        refreshBtn.disabled = true;
        refreshBtn.classList.add('busy');
        refreshBtn.innerHTML = '<span class="spin"></span><span class="label">' +
          (window.I18N ? window.I18N.refreshing : 'Refreshing') + '</span>';
        refreshBtn.setAttribute('data-original', original);
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
            refreshBtn.innerHTML = '<span class="label">' + refreshBtn.getAttribute('data-original') + '</span>';
          }
        });
    }

    refreshBtn.addEventListener('click', function () { pull(true); });

    // 自动刷新：行情页开着的时候每 60 秒拉一次。
    // 停在一个过期的价格上做决定，比看不到价格更危险。
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

  /* ---------------- tabs ---------------- */

  var tabButtons = document.querySelectorAll('.tabs button[data-tab]');
  if (tabButtons.length) {
    var activate = function (name) {
      tabButtons.forEach(function (b) {
        b.classList.toggle('active', b.getAttribute('data-tab') === name);
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
    // ?tab= 也是入口：页面右上角的「导入成交单」按钮就是一个普通链接，
    // 它不能指望浏览器替它跳标签页。反过来 #tab 仍然有效，旧书签不至于失效。
    var wanted = hash || (new URLSearchParams(location.search).get('tab') || '');
    if (wanted && document.querySelector('.tabs button[data-tab="' + wanted + '"]')) {
      activate(wanted);
    }
  }

  /* ---------------- glossary ---------------- */

  var glossaryPromise = null;
  function glossary() {
    if (!glossaryPromise) {
      glossaryPromise = fetch('/api/glossary').then(function (r) { return r.json(); });
    }
    return glossaryPromise;
  }

  var pop = null;
  function hidePop() {
    if (pop && pop.parentNode) pop.parentNode.removeChild(pop);
    pop = null;
  }

  document.addEventListener('mouseover', function (e) {
    var el = e.target.closest && e.target.closest('[data-term]');
    if (!el) return;
    var term = el.getAttribute('data-term');
    glossary().then(function (data) {
      var item = data.items && data.items[term];
      if (!item) {
        hidePop();
        return;
      }
      hidePop();
      pop = document.createElement('div');
      pop.className = 'gloss-pop';

      var title = document.createElement('b');
      title.textContent = item.term;
      var body = document.createElement('span');
      body.textContent = item.body;
      var kill = document.createElement('span');
      kill.className = 'kill';
      kill.textContent = '不再提示这个词';
      kill.addEventListener('click', function () {
        fetch('/glossary/' + term + '/toggle', { method: 'POST' }).then(function () {
          glossaryPromise = null;
          hidePop();
          document.querySelectorAll('[data-term="' + term + '"]').forEach(function (node) {
            node.classList.remove('gloss');
          });
        });
      });

      pop.appendChild(title);
      pop.appendChild(body);
      pop.appendChild(kill);
      document.body.appendChild(pop);

      var rect = el.getBoundingClientRect();
      var top = rect.bottom + window.scrollY + 8;
      var left = Math.min(rect.left + window.scrollX, window.innerWidth - 340);
      pop.style.top = top + 'px';
      pop.style.left = Math.max(12, left) + 'px';
    });
  });

  document.addEventListener('mouseout', function (e) {
    if (e.target.closest && e.target.closest('[data-term]')) hidePop();
  });
  window.addEventListener('scroll', hidePop, { passive: true });
})();
