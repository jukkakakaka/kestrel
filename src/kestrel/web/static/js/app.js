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
})();
