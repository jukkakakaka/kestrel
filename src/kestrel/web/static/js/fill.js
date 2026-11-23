/* 统一成交录入弹窗：买入建仓与卖出平仓共用，实时对比「实际 vs 计划」与账户影响（支持隐私打码联动）。 */
(function () {
  'use strict';

  var dialog = document.getElementById('fill-dialog');
  if (!dialog) return;

  var $ = function (id) { return document.getElementById(id); };
  var i18n = function (k, d) { return (window.I18N && window.I18N[k]) || d || ''; };

  var codeEl = $('fd-code');
  var qtyEl = $('fd-qty');
  var priceEl = $('fd-price');
  var feesEl = $('fd-fees');
  var stopEl = $('fd-stop');
  var targetEl = $('fd-target');
  var sideEl = $('fd-side');
  var dateEl = $('fd-date');
  var noteEl = $('fd-note');
  var msgEl = $('fd-msg');
  var resolvedEl = $('fd-resolved');
  var feesHintEl = $('fd-fees-hint');
  var levelsEl = $('fd-levels');
  var levelsNoteEl = $('fd-levels-note');
  var previewEl = $('fd-preview');
  var buyExtrasEl = $('fd-buy-extras');
  var sellExtrasEl = $('fd-sell-extras');
  var exitReasonEl = $('fd-exit-reason');
  var ruleBreakEl = $('fd-rule-break');

  var state = { market: '', code: '', feesTouched: false, timer: null, previewTimer: null, preset: null, lookup: 0, preview: 0, saving: false };

  function syncSideUi() {
    var isSell = sideEl.value === 'sell';
    if (buyExtrasEl) buyExtrasEl.classList.toggle('hidden', isSell);
    if (sellExtrasEl) sellExtrasEl.classList.toggle('hidden', !isSell);
    if (isSell && levelsEl) {
      levelsEl.classList.add('hidden');
      if (levelsNoteEl) levelsNoteEl.textContent = '';
    }
  }

  function levelsLocked() {
    return !!(stopEl.value || targetEl.value);
  }

  function renderPreview(res) {
    if (!previewEl) return;
    if (!res || !res.ok || (!res.summary && (!res.warnings || !res.warnings.length) && !res.qty_error)) {
      previewEl.classList.add('hidden');
      previewEl.textContent = '';
      return;
    }
    previewEl.textContent = '';
    var hasWarn = !res.qty_valid && res.qty_error || (res.warnings && res.warnings.length > 0);
    previewEl.className = 'preview-box' + (hasWarn ? ' warn' : '');

    if (res.summary) {
      var sumLine = document.createElement('div');
      sumLine.style.fontWeight = '600';
      sumLine.textContent = res.summary;
      previewEl.appendChild(sumLine);
    }

    var grid = document.createElement('div');
    grid.className = 'preview-grid';
    function addCell(label, value, privType) {
      var cell = document.createElement('div');
      var k = document.createElement('div');
      k.className = 'tiny muted';
      k.textContent = label;
      var v = document.createElement('div');
      v.className = 'mono';
      v.style.fontWeight = '600';
      if (privType) v.setAttribute('data-priv', privType);
      v.textContent = value;
      cell.appendChild(k);
      cell.appendChild(v);
      grid.appendChild(cell);
    }

    var isEn = document.documentElement.lang.indexOf('en') === 0;
    if (res.breakeven !== null && res.breakeven !== undefined && sideEl.value === 'buy') {
      addCell(isEn ? 'Break-even' : '含费保本价', String(res.breakeven));
    }
    if (res.actual_risk !== undefined) {
      addCell(
        isEn ? 'Actual risk' : '实际风险金',
        Number(res.actual_risk).toFixed(1) + ' ' + (res.currency || ''),
        'money'
      );
    }
    if (res.actual_r !== undefined) {
      addCell(isEn ? 'Actual R:R' : '实际盈亏比', res.actual_r + ':1');
    }
    if (res.plan && res.plan.deviation_pct !== null) {
      addCell(
        isEn ? 'Price drift' : '相对建议偏离',
        (res.plan.deviation_pct >= 0 ? '+' : '') + res.plan.deviation_pct + '%' +
        (res.plan.deviation_r !== null ? ' (' + (res.plan.deviation_r >= 0 ? '+' : '') + res.plan.deviation_r + 'R)' : '')
      );
    }
    if (res.cash_after_cny !== undefined) {
      addCell(
        isEn ? 'Cash after' : '成交后可用现金',
        '¥' + Number(res.cash_after_cny).toLocaleString(undefined, { maximumFractionDigits: 0 }),
        'money'
      );
    }
    if (grid.children.length) previewEl.appendChild(grid);

    if (!res.qty_valid && res.qty_error) {
      var errEl = document.createElement('div');
      errEl.className = 'tiny up';
      errEl.style.marginTop = '6px';
      errEl.textContent = res.qty_error;
      previewEl.appendChild(errEl);
    }
    (res.warnings || []).forEach(function (w) {
      var wEl = document.createElement('div');
      wEl.className = 'tiny';
      wEl.style.marginTop = '4px';
      wEl.textContent = '• ' + w;
      previewEl.appendChild(wEl);
    });
    if (window.KestrelAnnotate) window.KestrelAnnotate(previewEl);
  }

  function runPreview() {
    var code = (codeEl.value || '').trim();
    var price = priceEl.value;
    var qty = qtyEl.value;
    if (!code || !price || !qty) {
      if (previewEl) previewEl.classList.add('hidden');
      return;
    }
    var preview = ++state.preview;
    fetch('/api/journal/fill/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        confirmed: true,
        market: state.market || (state.preset && state.preset.market) || '',
        code: code,
        side: sideEl.value,
        qty: qty,
        price: price,
        fees: feesEl.value !== '' ? feesEl.value : null,
        stop: stopEl.value || null,
        target: targetEl.value || null,
        rec_id: state.preset && state.preset.recId ? state.preset.recId : null,
        trade_id: state.preset && state.preset.tradeId ? state.preset.tradeId : null
      })
    })
      .then(function (r) { return r.json(); })
      .then(function (res) { if (preview === state.preview && dialog.open) renderPreview(res); })
      .catch(function () {});
  }

  function suggest() {
    var code = (codeEl.value || '').trim();
    if (!code) {
      resolvedEl.textContent = i18n('codeHelp');
      return;
    }
    var lookup = ++state.lookup;
    var params = new URLSearchParams({
      code: code,
      market: (state.preset && state.preset.market) || '',
      price: priceEl.value || '',
      qty: qtyEl.value || '',
      side: sideEl.value || 'buy',
      horizon: (state.preset && state.preset.horizon) || ''
    });
    fetch('/api/journal/fill/suggest?' + params.toString())
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res.ok || lookup !== state.lookup || !dialog.open) return;
        state.market = res.market || '';
        state.code = res.code || '';
        if (!res.market) {
          resolvedEl.textContent = i18n('unknownSymbol');
          return;
        }
        var isPrivate = document.documentElement.getAttribute('data-privacy') === 'on';
        var heldText = isPrivate ? '••••' : String(res.held_qty);
        var heldInfo = (sideEl.value === 'sell' && res.held_qty > 0)
          ? ' · ' + (document.documentElement.lang.indexOf('en') === 0 ? 'Held ' : '当前持仓 ') + heldText
          : '';
        resolvedEl.textContent = (res.known
          ? res.market + ' · ' + res.code + ' · ' + res.name
          : res.market + ' · ' + res.code + ' · ' + i18n('notCached')) + heldInfo;

        if (res.fees !== null && res.fees !== undefined) {
          feesEl.placeholder = Number(res.fees).toFixed(2);
          if (!state.feesTouched) feesEl.value = '';
        }
        feesHintEl.textContent = res.broker
          ? i18n('feesByBroker', '{broker}').replace('{broker}', res.broker)
          : '';

        if (sideEl.value === 'buy') {
          var lv = res.levels || {};
          if (lv.ok) {
            levelsEl.className = 'notice';
            levelsEl.textContent = lv.basis + ' · ' + Number(lv.stop).toFixed(3) + ' / ' + Number(lv.target).toFixed(3);
            levelsNoteEl.textContent = i18n('levelsNote');
          } else if (levelsLocked() || (state.preset && state.preset.recId)) {
            levelsEl.className = 'notice hidden';
            levelsNoteEl.textContent = '';
          } else {
            levelsEl.className = 'notice warn';
            levelsEl.textContent = lv.reason || i18n('levelsUnavailable');
            levelsNoteEl.textContent = i18n('levelsOptional');
          }
          if (!levelsEl.classList.contains('hidden')) levelsEl.classList.remove('hidden');
        }
        runPreview();
      })
      .catch(function () {});
  }

  function debounced() {
    state.lookup++;
    state.preview++;
    clearTimeout(state.timer);
    state.timer = setTimeout(suggest, 320);
    clearTimeout(state.previewTimer);
    state.previewTimer = setTimeout(runPreview, 360);
  }

  function open(preset) {
    if (state.saving) return;
    clearTimeout(state.timer);
    clearTimeout(state.previewTimer);
    state.lookup++;
    state.preview++;
    state.requestId = window.crypto.randomUUID ? window.crypto.randomUUID() :
      Array.from(window.crypto.getRandomValues(new Uint32Array(4))).join('-');
    state.preset = preset || null;
    state.market = (preset && preset.market) || '';
    state.code = (preset && preset.code) || '';
    state.feesTouched = false;
    msgEl.textContent = '';
    dateEl.value = '';
    codeEl.readOnly = !!(preset && (preset.recId || preset.tradeId));
    sideEl.disabled = codeEl.readOnly;
    levelsEl.classList.add('hidden');
    levelsNoteEl.textContent = '';
    resolvedEl.textContent = '';
    feesHintEl.textContent = '';
    codeEl.value = '';
    qtyEl.value = '';
    priceEl.value = '';
    feesEl.value = '';
    feesEl.placeholder = 'Auto';
    stopEl.value = '';
    targetEl.value = '';
    noteEl.value = '';
    if (ruleBreakEl) ruleBreakEl.checked = false;
    if (exitReasonEl) exitReasonEl.value = (preset && preset.exitReason) || 'manual';
    if (previewEl) previewEl.classList.add('hidden');
    sideEl.value = (preset && preset.side) || 'buy';

    if (preset) {
      codeEl.value = preset.market && preset.code ? (preset.market + '/' + preset.code) : (preset.code || '');
      if (preset.note) noteEl.value = preset.note;
    }

    syncSideUi();
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    (codeEl.readOnly || (preset && preset.code) ? qtyEl : codeEl).focus();
    if (window.KestrelAnnotate) window.KestrelAnnotate(dialog);
    if (preset && preset.code) suggest();
  }

  window.openFillDialog = open;

  var topBtn = document.getElementById('top-fill-btn');
  if (topBtn) topBtn.addEventListener('click', function () { open(null); });

  var pageFillBtn = document.getElementById('fill-btn');
  if (pageFillBtn) pageFillBtn.addEventListener('click', function () { open(null); });

  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('[data-open-fill]');
    if (!btn) return;
    e.preventDefault();
    open({
      side: btn.getAttribute('data-side') || 'buy',
      market: btn.getAttribute('data-market') || '',
      code: btn.getAttribute('data-code') || '',
      qty: btn.getAttribute('data-qty') || '',
      price: btn.getAttribute('data-price') || '',
      stop: btn.getAttribute('data-stop') || '',
      target: btn.getAttribute('data-target') || '',
      horizon: btn.getAttribute('data-horizon') || '',
      recId: btn.getAttribute('data-rec-id') || '',
      tradeId: btn.getAttribute('data-trade-id') || '',
      exitReason: btn.getAttribute('data-exit-reason') || '',
      note: btn.getAttribute('data-note') || ''
    });
  });

  ['input', 'change'].forEach(function (evt) {
    codeEl.addEventListener(evt, function () {
      state.market = '';
      state.code = '';
      state.lookup++;
      debounced();
    });
    qtyEl.addEventListener(evt, debounced);
    priceEl.addEventListener(evt, debounced);
    stopEl.addEventListener(evt, function () { clearTimeout(state.previewTimer); state.previewTimer = setTimeout(runPreview, 250); });
    targetEl.addEventListener(evt, function () { clearTimeout(state.previewTimer); state.previewTimer = setTimeout(runPreview, 250); });
  });
  sideEl.addEventListener('change', function () {
    syncSideUi();
    suggest();
  });
  feesEl.addEventListener('input', function () {
    state.feesTouched = feesEl.value !== '';
    clearTimeout(state.previewTimer);
    state.previewTimer = setTimeout(runPreview, 250);
  });

  $('fd-cancel').addEventListener('click', function () {
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
  });

  $('fd-save').addEventListener('click', function () {
    if (state.saving) return;
    var payload = {
      request_id: state.requestId,
      confirmed: true,
      market: state.market || (state.preset && state.preset.market) || '',
      code: (codeEl.value || '').trim(),
      side: sideEl.value,
      qty: qtyEl.value,
      price: priceEl.value,
      fees: feesEl.value !== '' ? feesEl.value : null,
      filled_at: dateEl.value || null,
      stop: stopEl.value || null,
      target: targetEl.value || null,
      note: noteEl.value,
      exit_reason: sideEl.value === 'sell' && exitReasonEl ? exitReasonEl.value : '',
      rule_break: sideEl.value === 'sell' && ruleBreakEl ? ruleBreakEl.checked : false
    };
    if (state.preset && state.preset.recId) payload.rec_id = Number(state.preset.recId);
    if (state.preset && state.preset.tradeId) payload.trade_id = Number(state.preset.tradeId);

    if (!payload.code || !payload.qty || !payload.price) {
      msgEl.textContent = i18n('requireFill');
      return;
    }
    state.saving = true;
    $('fd-save').disabled = true;
    msgEl.textContent = i18n('saving');
    fetch('/api/journal/fill', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res.ok) {
          msgEl.textContent = res.error || i18n('saveFailedPlain');
          return;
        }
        var done = state.preset && state.preset.onSaved;
        if (typeof done === 'function') { done(res); return; }
        location.reload();
      })
      .catch(function (e) { msgEl.textContent = String(e); })
      .finally(function () {
        state.saving = false;
        $('fd-save').disabled = false;
      });
  });
})();
