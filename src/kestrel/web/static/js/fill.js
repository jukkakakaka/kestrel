/* 记一笔成交的弹窗。
 *
 * 台账页的「记一笔成交」和建议卡的「我已经买了」用的是同一个弹窗：
 * 建议卡只是先填好建议里的价格和数量，用户仍然可以改成真实成交。
 *
 * 市场、名称、手续费、止损/目标都由 /api/journal/fill/suggest 现算，
 * 用户什么都不用先知道。
 */
(function () {
  'use strict';

  var dialog = document.getElementById('fill-dialog');
  if (!dialog) return;

  var $ = function (id) { return document.getElementById(id); };
  var I18N = window.I18N || {};

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

  var state = { market: '', feesTouched: false, timer: null, preset: null };

  function levelsLocked() {
    return !!(stopEl.value || targetEl.value);
  }

  function suggest() {
    var code = (codeEl.value || '').trim();
    if (!code) {
      resolvedEl.textContent = I18N.codeHelp || '';
      return;
    }
    var params = new URLSearchParams({
      code: code,
      price: priceEl.value || '',
      qty: qtyEl.value || '',
      side: sideEl.value || 'buy'
    });
    fetch('/api/journal/fill/suggest?' + params.toString())
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res.ok) return;
        state.market = res.market || '';
        if (!res.market) {
          // 名称在库里查不到：不猜市场，直接告诉用户去加自选或换成代码。
          resolvedEl.textContent = I18N.unknownSymbol || '';
          return;
        }
        resolvedEl.textContent = res.known
          ? res.market + ' · ' + res.code + ' · ' + res.name
          : res.market + ' · ' + res.code + ' · ' + (I18N.notCached || '');

        if (res.fees !== null && res.fees !== undefined && !state.feesTouched) {
          feesEl.value = Number(res.fees).toFixed(2);
        }
        feesHintEl.textContent = res.broker
          ? (I18N.feesByBroker || '{broker}').replace('{broker}', res.broker)
          : '';

        var lv = res.levels || {};
        if (lv.ok) {
          if (!levelsLocked() && sideEl.value === 'buy') {
            stopEl.value = Number(lv.stop).toFixed(3);
            targetEl.value = Number(lv.target).toFixed(3);
          }
          levelsEl.className = 'notice';
          levelsEl.textContent = lv.basis +
            '（' + lv.atr_pct + '% / ' + lv.risk_pct + '%）';
          levelsNoteEl.textContent = I18N.levelsNote || '';
        } else if (stopEl.value && targetEl.value) {
          // 从建议卡进来时止损/目标已经填好了，这时再警告「算不出建议止损」
          // 只会制造噪音——用户已经有一个计划了。
          levelsEl.className = 'notice hidden';
          levelsNoteEl.textContent = '';
        } else {
          levelsEl.className = 'notice warn';
          levelsEl.textContent = lv.reason || I18N.levelsUnavailable || '';
          levelsNoteEl.textContent = I18N.levelsOptional || '';
        }
        if (!levelsEl.classList.contains('hidden')) levelsEl.classList.remove('hidden');
      })
      .catch(function () { /* 建议失败不影响记账 */ });
  }

  function debounced() {
    clearTimeout(state.timer);
    state.timer = setTimeout(suggest, 350);
  }

  function open(preset) {
    state.preset = preset || null;
    state.feesTouched = false;
    msgEl.textContent = '';
    dateEl.value = new Date().toISOString().slice(0, 10);
    codeEl.value = '';
    qtyEl.value = '';
    priceEl.value = '';
    feesEl.value = '';
    stopEl.value = '';
    targetEl.value = '';
    noteEl.value = '';
    sideEl.value = (preset && preset.side) || 'buy';

    if (preset) {
      codeEl.value = preset.code || '';
      qtyEl.value = preset.qty || '';
      priceEl.value = preset.price || '';
      // 建议里的止损/目标已经是这一笔的计划，先填上，允许改。
      stopEl.value = preset.stop || '';
      targetEl.value = preset.target || '';
      if (preset.note) noteEl.value = preset.note;
    }

    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    codeEl.focus();
    if (preset && preset.code) suggest();
  }

  window.openFillDialog = open;

  ['input', 'change'].forEach(function (evt) {
    codeEl.addEventListener(evt, debounced);
    qtyEl.addEventListener(evt, debounced);
    priceEl.addEventListener(evt, debounced);
  });
  sideEl.addEventListener('change', suggest);
  feesEl.addEventListener('input', function () { state.feesTouched = true; });

  $('fd-cancel').addEventListener('click', function () {
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
  });

  $('fd-save').addEventListener('click', function () {
    var payload = {
      market: state.market || '',
      code: (codeEl.value || '').trim(),
      side: sideEl.value,
      qty: qtyEl.value,
      price: priceEl.value,
      fees: feesEl.value || 0,
      filled_at: dateEl.value || null,
      stop: stopEl.value || null,
      target: targetEl.value || null,
      note: noteEl.value
    };
    // 从建议卡进来的成交要挂回那条建议：否则建议仍显示「待处理」，
    // 执行质量页也统计不到这一笔。
    if (state.preset && state.preset.recId) payload.rec_id = state.preset.recId;
    if (!payload.code || !payload.qty || !payload.price) {
      msgEl.textContent = I18N.requireFill || '';
      return;
    }
    msgEl.textContent = I18N.saving || '';
    fetch('/api/journal/fill', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res.ok) {
          msgEl.textContent = res.error || I18N.saveFailedPlain || '';
          return;
        }
        var done = state.preset && state.preset.onSaved;
        if (typeof done === 'function') { done(); return; }
        location.reload();
      })
      .catch(function (e) { msgEl.textContent = String(e); });
  });
})();
