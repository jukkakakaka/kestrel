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

  var addBtn = document.getElementById('add-btn');
  var addInput = document.getElementById('add-code');
  if (addBtn && addInput) {
    var submit = function () {
      var code = addInput.value.trim();
      if (!code) return;
      addBtn.disabled = true;
      fetch('/api/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: code })
      })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (res.ok) { location.reload(); }
          else { alert(res.error || 'failed'); addBtn.disabled = false; }
        })
        .catch(function () { addBtn.disabled = false; });
    };
    addBtn.addEventListener('click', submit);
    addInput.addEventListener('keydown', function (e) { if (e.key === 'Enter') submit(); });
  }

  document.querySelectorAll('[data-remove]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var path = btn.getAttribute('data-remove');
      if (!confirm(path + ' ?')) return;
      fetch('/api/watchlist/' + path, { method: 'DELETE' })
        .then(function () { location.reload(); });
    });
  });

  function paint(quotes) {
    quotes.forEach(function (q) {
      var row = document.querySelector('tr[data-key="' + q.market + ':' + q.code + '"]');
      if (!row) return;
      var price = row.querySelector('[data-field="price"]');
      var change = row.querySelector('[data-field="change"]');
      var src = row.querySelector('[data-field="source"]');
      if (price) price.textContent = q.price;
      if (src) src.textContent = q.source;
      if (change) {
        if (q.change_pct === null || q.change_pct === undefined) {
          change.textContent = '—';
        } else {
          var pct = Number(q.change_pct);
          change.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
          change.className = 'num mono ' + (pct >= 0 ? 'up' : 'down');
        }
      }
    });
  }

  var refreshBtn = document.getElementById('refresh-btn');
  var staleBox = document.getElementById('stale');
  if (refreshBtn) {
    var load = function () {
      refreshBtn.disabled = true;
      fetch('/api/quotes')
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (res.quotes) paint(res.quotes);
          if (staleBox) staleBox.classList.toggle('hidden', !res.stale);
        })
        .finally(function () { refreshBtn.disabled = false; });
    };
    refreshBtn.addEventListener('click', load);
    load();
  }

  var scanBtn = document.getElementById('scan-btn');
  var scanStatus = document.getElementById('scan-status');
  if (scanBtn) {
    scanBtn.addEventListener('click', function () {
      scanBtn.disabled = true;
      scanBtn.textContent = scanBtn.getAttribute('data-running') || '…';
      if (scanStatus) {
        scanStatus.classList.remove('hidden');
        scanStatus.textContent = scanBtn.getAttribute('data-running') || '…';
      }
      fetch('/api/scan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (res.ok) { location.reload(); }
          else {
            if (scanStatus) scanStatus.textContent = res.error || 'failed';
            scanBtn.disabled = false;
          }
        })
        .catch(function () { scanBtn.disabled = false; });
    });
  }

  var tabs = document.querySelectorAll('.tab[data-tab]');
  if (tabs.length) {
    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        tabs.forEach(function (t) { t.classList.remove('active'); });
        tab.classList.add('active');
        var name = tab.getAttribute('data-tab');
        document.querySelectorAll('[data-pane]').forEach(function (pane) {
          pane.classList.toggle('hidden', pane.getAttribute('data-pane') !== name);
        });
        if (history.replaceState) history.replaceState(null, '', '#' + name);
      });
    });
    var hash = location.hash.replace('#', '');
    if (hash) {
      var target = document.querySelector('.tab[data-tab="' + hash + '"]');
      if (target) target.click();
    }
  }

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
    var el = e.target.closest('[data-term]');
    if (!el) return;
    glossary().then(function (data) {
      var item = data.items && data.items[el.getAttribute('data-term')];
      if (!item) return;
      hidePop();
      pop = document.createElement('div');
      pop.className = 'gloss-pop';
      var title = document.createElement('b');
      title.textContent = item.term;
      var body = document.createElement('span');
      body.textContent = item.body;
      pop.appendChild(title);
      pop.appendChild(body);
      document.body.appendChild(pop);
      var rect = el.getBoundingClientRect();
      var top = rect.bottom + window.scrollY + 8;
      var left = Math.min(rect.left + window.scrollX, window.innerWidth - 320);
      pop.style.top = top + 'px';
      pop.style.left = Math.max(12, left) + 'px';
    });
  });

  document.addEventListener('mouseout', function (e) {
    if (e.target.closest && e.target.closest('[data-term]')) hidePop();
  });
  window.addEventListener('scroll', hidePop, { passive: true });
})();
