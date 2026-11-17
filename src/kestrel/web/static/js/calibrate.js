/* 动态资金校准 & 手头持仓校准/补录弹窗逻辑（支持金额隐私标记联动）。 */
(function () {
  'use strict';

  var dlg = document.getElementById('calibrate-dialog');
  if (!dlg) return;

  var $ = function (id) { return document.getElementById(id); };
  var i18n = function (k, d) { return (window.I18N && window.I18N[k]) || d || ''; };
  var dirty = false;

  function fmtCny(v) {
    return '¥' + Number(v || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });
  }

  function setMoneyText(el, val) {
    if (!el) return;
    el.setAttribute('data-priv', 'money');
    el.textContent = fmtCny(val);
  }

  function updateTopBar(snap) {
    if (!snap) return;
    setMoneyText($('top-eq'), snap.total_equity_cny);
    setMoneyText($('top-cash'), snap.available_cash_cny);
    setMoneyText($('cp-ctx-eq'), snap.total_equity_cny);
    setMoneyText($('cp-ctx-cash'), snap.available_cash_cny);

    setMoneyText($('cal-v-init'), snap.initial_capital_cny);
    setMoneyText($('cal-v-cash'), snap.available_cash_cny);
    setMoneyText($('cal-v-mv'), snap.market_value_cny);
    setMoneyText($('cal-v-eq'), snap.total_equity_cny);
  }

  function renderPositions(list) {
    var box = $('cal-pos-list');
    if (!box) return;
    box.textContent = '';
    if (!list || !list.length) {
      var p = document.createElement('p');
      p.className = 'tiny muted';
      p.textContent = i18n('calEmptyPos', '当前暂无开仓持仓，可下方直接补录已有股票底仓。');
      box.appendChild(p);
      return;
    }
    list.forEach(function (pos) {
      var row = document.createElement('div');
      row.className = 'cal-pos-card';

      var head = document.createElement('div');
      head.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;gap:8px;';
      var title = document.createElement('strong');
      title.style.fontSize = '13px';
      title.textContent = pos.market + ' ' + pos.code + ' · ' + pos.name;
      head.appendChild(title);
      row.appendChild(head);

      var grid = document.createElement('div');
      grid.className = 'grid g4';
      grid.style.gap = '8px';

      function makeInput(label, val, isPrivate) {
        var wrap = document.createElement('div');
        wrap.className = 'field';
        var lb = document.createElement('label');
        lb.className = 'tiny';
        lb.textContent = label;
        var inp = document.createElement('input');
        inp.type = 'number';
        inp.step = '0.001';
        inp.value = val !== null && val !== undefined ? String(val) : '';
        if (isPrivate) inp.setAttribute('data-priv-input', '1');
        wrap.appendChild(lb);
        wrap.appendChild(inp);
        grid.appendChild(wrap);
        return inp;
      }

      var isEn = document.documentElement.lang.indexOf('en') === 0;
      var qtyIn = makeInput(isEn ? 'Qty' : '股数', pos.qty, true);
      var entIn = makeInput(isEn ? 'Cost' : '成本价', pos.entry, false);
      var stpIn = makeInput(isEn ? 'Stop' : '止损价', pos.stop, false);
      var tgtIn = makeInput(isEn ? 'Target' : '目标价', pos.target, false);
      row.appendChild(grid);

      var foot = document.createElement('div');
      foot.style.cssText = 'display:flex;justify-content:flex-end;gap:6px;margin-top:10px;';
      var delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'btn ghost sm';
      delBtn.textContent = i18n('calRemoveBtn', '清零移除');
      var saveBtn = document.createElement('button');
      saveBtn.type = 'button';
      saveBtn.className = 'btn sm';
      saveBtn.textContent = i18n('calSaveBtn', '保存修改');

      function postUpdate(qVal) {
        fetch('/api/portfolio/calibrate-position', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            trade_id: pos.id,
            qty: qVal,
            entry_price: entIn.value || pos.entry,
            stop_price: stpIn.value || null,
            target_price: tgtIn.value || null,
            affect_cash: false
          })
        })
          .then(function (r) { return r.json(); })
          .then(function (res) {
            var msg = $('cal-pos-msg');
            if (!res.ok) {
              if (msg) msg.textContent = res.error || i18n('saveFailedPlain');
              return;
            }
            dirty = true;
            updateTopBar(res.snapshot);
            renderPositions(res.snapshot && res.snapshot.positions);
            if (msg) msg.textContent = res.message || i18n('calUpdated');
          });
      }

      saveBtn.addEventListener('click', function () { postUpdate(qtyIn.value || 0); });
      delBtn.addEventListener('click', function () { postUpdate(0); });
      foot.appendChild(delBtn);
      foot.appendChild(saveBtn);
      row.appendChild(foot);
      box.appendChild(row);
    });
  }

  function loadPortfolio() {
    fetch('/api/portfolio')
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res.ok) return;
        updateTopBar(res);
        renderPositions(res.positions || []);
      });
  }

  function openCalibrate(tabName) {
    dirty = false;
    var cm = $('cal-cash-msg');
    var pm = $('cal-pos-msg');
    if (cm) cm.textContent = '';
    if (pm) pm.textContent = '';
    switchTab(tabName || 'cash');
    loadPortfolio();
    if (typeof dlg.showModal === 'function') dlg.showModal();
    else dlg.setAttribute('open', '');
    if (window.KestrelAnnotate) window.KestrelAnnotate(dlg);
  }

  function switchTab(name) {
    dlg.querySelectorAll('[data-cal-tab]').forEach(function (b) {
      b.setAttribute('aria-selected', b.getAttribute('data-cal-tab') === name ? 'true' : 'false');
    });
    var pc = $('cal-pane-cash');
    var pp = $('cal-pane-pos');
    if (pc) pc.classList.toggle('hidden', name !== 'cash');
    if (pp) pp.classList.toggle('hidden', name !== 'pos');
  }

  window.openCalibrateDialog = openCalibrate;

  dlg.querySelectorAll('[data-cal-tab]').forEach(function (btn) {
    btn.addEventListener('click', function () { switchTab(btn.getAttribute('data-cal-tab')); });
  });

  dlg.addEventListener('close', function () {
    if (dirty) location.reload();
  });

  var topBtn = $('open-calibrate');
  if (topBtn) topBtn.addEventListener('click', function () { openCalibrate('cash'); });
  document.addEventListener('click', function (e) {
    var trigger = e.target.closest && e.target.closest('[data-open-calibrate]');
    if (!trigger) return;
    e.preventDefault();
    openCalibrate(trigger.getAttribute('data-open-calibrate') || 'cash');
  });

  var cashBtn = $('cal-cash-submit');
  if (cashBtn) {
    cashBtn.addEventListener('click', function () {
      var mode = $('cal-cash-mode').value;
      var amt = $('cal-cash-amount').value;
      var note = $('cal-cash-note').value;
      var msg = $('cal-cash-msg');
      if (!amt) return;
      var body = { note: note };
      if (mode === 'target_available') body.target_available_cny = amt;
      else if (mode === 'target_equity') body.target_equity_cny = amt;
      else if (mode === 'deposit') { body.kind = 'deposit'; body.delta_cny = amt; }
      else if (mode === 'withdraw') { body.kind = 'withdraw'; body.delta_cny = amt; }
      else if (mode === 'initial_capital') body.initial_capital_cny = amt;

      fetch('/api/portfolio/calibrate-cash', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (!res.ok) {
            if (msg) msg.textContent = res.error || i18n('saveFailedPlain');
            return;
          }
          dirty = true;
          updateTopBar(res.snapshot);
          if (msg) msg.textContent = i18n('calUpdated', '已同步更新资金账本');
          $('cal-cash-amount').value = '';
        });
    });
  }

  var posAddBtn = $('cal-pos-add');
  if (posAddBtn) {
    posAddBtn.addEventListener('click', function () {
      var msg = $('cal-pos-msg');
      var code = ($('cal-p-code').value || '').trim();
      var qty = $('cal-p-qty').value;
      var entry = $('cal-p-entry').value;
      if (!code || !qty || !entry) {
        if (msg) msg.textContent = i18n('requireFill');
        return;
      }
      fetch('/api/portfolio/calibrate-position', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          market: $('cal-p-market').value || '',
          code: code,
          qty: qty,
          entry_price: entry,
          stop_price: $('cal-p-stop').value || null,
          target_price: $('cal-p-target').value || null,
          affect_cash: $('cal-p-affect-cash').checked
        })
      })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (!res.ok) {
            if (msg) msg.textContent = res.error || i18n('saveFailedPlain');
            return;
          }
          dirty = true;
          updateTopBar(res.snapshot);
          renderPositions(res.snapshot && res.snapshot.positions);
          if (msg) msg.textContent = res.message || i18n('calUpdated');
          $('cal-p-code').value = '';
          $('cal-p-qty').value = '';
          $('cal-p-entry').value = '';
          $('cal-p-stop').value = '';
          $('cal-p-target').value = '';
        });
    });
  }
})();
