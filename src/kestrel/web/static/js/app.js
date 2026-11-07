(function () {
  'use strict';

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
      var originalBtn = addBtn.textContent;
      addBtn.textContent = '…';
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
            say((res.body && res.body.error) || '添加失败', 'err', true);
            addBtn.disabled = false;
            addBtn.textContent = originalBtn;
          }
        })
        .catch(function (e) {
          say('网络错误：' + e, 'err', true);
          addBtn.disabled = false;
          addBtn.textContent = originalBtn;
        });
    };
    addBtn.addEventListener('click', submit);
    addInput.addEventListener('keydown', function (e) { if (e.key === 'Enter') submit(); });
  }

  document.querySelectorAll('[data-remove]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var path = btn.getAttribute('data-remove');
      if (!confirm('从自选里移除 ' + path + '？')) return;
      fetch('/api/watchlist/' + path, { method: 'DELETE' }).then(function () { location.reload(); });
    });
  });

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
    refreshBtn.addEventListener('click', function () {
      var label = refreshBtn.querySelector('.label') || refreshBtn;
      var original = label.textContent;
      refreshBtn.disabled = true;
      refreshBtn.innerHTML = '<span class="spin"></span><span class="label">刷新中</span>';

      fetch('/api/quotes')
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (res.quotes) paint(res.quotes);
          var now = new Date();
          if (updatedAt) {
            updatedAt.textContent = '更新于 ' + now.toTimeString().slice(0, 8);
          }
          if (res.stale) {
            say('部分行情来自缓存，数据源可能暂时不可用。', 'warn');
          }
        })
        .catch(function (e) {
          say('刷新失败：' + e + '（数据源可能被限流，稍等再试）', 'err', true);
        })
        .finally(function () {
          refreshBtn.disabled = false;
          refreshBtn.innerHTML = '<span class="label">' + original + '</span>';
        });
    });
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
    if (hash && document.querySelector('.tabs button[data-tab="' + hash + '"]')) activate(hash);
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
