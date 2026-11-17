/* 可开关右侧炒股 Agent 侧边栏（Kestrel Copilot）：强 API 门禁、严格话题限定、上下文自动注入。 */
(function () {
  'use strict';

  var drawer = document.getElementById('copilot-drawer');
  if (!drawer) return;

  var $ = function (id) { return document.getElementById(id); };
  var i18n = function (k, d) { return (window.I18N && window.I18N[k]) || d || ''; };
  var STORE_KEY = 'kestrel.copilot.open';

  var toggleBtn = $('toggle-copilot');
  var closeBtn = $('cp-close');
  var cfgToggleBtn = $('cp-toggle-cfg');
  var cancelCfgBtn = $('cp-cancel-cfg');
  var saveCfgBtn = $('cp-save-cfg');
  var clearBtn = $('cp-clear');
  var setupPane = $('cp-setup');
  var chatWrap = $('cp-chat-wrap');
  var msgsBox = $('cp-messages');
  var inputEl = $('cp-input');
  var sendBtn = $('cp-send');
  var sendTextEl = sendBtn ? sendBtn.querySelector('.cp-send-text') : null;
  var modelLabel = $('cp-model-label');
  var cfgMsg = $('cp-cfg-msg');

  var historyLoaded = false;

  function setOpen(open) {
    var isOpen = !!open;
    document.body.classList.toggle('copilot-open', isOpen);
    if (toggleBtn) {
      toggleBtn.classList.toggle('active', isOpen);
      toggleBtn.setAttribute('aria-pressed', isOpen ? 'true' : 'false');
    }
    try { localStorage.setItem(STORE_KEY, isOpen ? '1' : '0'); } catch (e) {}
    if (isOpen && !historyLoaded && !chatWrap.classList.contains('hidden')) {
      loadHistory();
    }
    window.dispatchEvent(new Event('resize'));
  }

  function appendMsg(role, content, rejected) {
    if (!msgsBox) return;
    var div = document.createElement('div');
    div.className = 'copilot-msg ' + (role === 'user' ? 'user' : 'assistant') + (rejected ? ' rejected' : '');
    div.textContent = content;
    msgsBox.appendChild(div);
    msgsBox.scrollTop = msgsBox.scrollHeight;
    if (role !== 'user' && !rejected && window.KestrelAnnotate) {
      window.KestrelAnnotate(div);
    }
  }

  function loadHistory() {
    historyLoaded = true;
    fetch('/api/copilot/history')
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res.ok || !res.messages || !res.messages.length) return;
        msgsBox.textContent = '';
        res.messages.forEach(function (m) {
          appendMsg(m.role, m.content, !!m.rejected);
        });
      })
      .catch(function () {});
  }

  function showSetup(show) {
    setupPane.classList.toggle('hidden', !show);
    chatWrap.classList.toggle('hidden', show);
  }

  function setSendBusy(busy) {
    if (!sendBtn) return;
    sendBtn.disabled = !!busy;
    var label = sendBtn.getAttribute('data-label') || '发送';
    if (sendTextEl) {
      sendTextEl.textContent = busy ? i18n('cpSending', '…') : label;
    } else {
      sendBtn.textContent = busy ? i18n('cpSending', '…') : label;
    }
  }

  if (sendBtn) {
    sendBtn.setAttribute('data-label', sendTextEl ? sendTextEl.textContent : sendBtn.textContent);
  }

  function sendQuestion(text, symbolHint) {
    var msg = String(text !== undefined ? text : inputEl.value || '').trim();
    if (!msg) return;
    if (text === undefined) inputEl.value = '';
    appendMsg('user', msg, false);
    setSendBusy(true);

    fetch('/api/copilot/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: msg,
        page_path: location.pathname,
        symbol_hint: symbolHint || ''
      })
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res.ok) {
          if (res.code === 'not_configured') {
            showSetup(true);
            if (cfgMsg) cfgMsg.textContent = res.error || '';
          } else {
            appendMsg('assistant', res.error || i18n('unknownError', 'Error'), true);
          }
          return;
        }
        appendMsg('assistant', res.reply, !!res.rejected);
      })
      .catch(function (e) {
        appendMsg('assistant', i18n('networkError', 'Network error: ') + e, true);
      })
      .finally(function () {
        setSendBusy(false);
      });
  }

  window.openCopilot = function (promptText, symbolHint) {
    setOpen(true);
    if (promptText && !chatWrap.classList.contains('hidden')) {
      sendQuestion(promptText, symbolHint);
    } else if (promptText && inputEl) {
      inputEl.value = promptText;
    }
  };

  if (toggleBtn) toggleBtn.addEventListener('click', function () {
    setOpen(!document.body.classList.contains('copilot-open'));
  });
  if (closeBtn) closeBtn.addEventListener('click', function () { setOpen(false); });
  if (cfgToggleBtn) cfgToggleBtn.addEventListener('click', function () {
    showSetup(setupPane.classList.contains('hidden'));
  });
  if (cancelCfgBtn) cancelCfgBtn.addEventListener('click', function () { showSetup(false); });

  drawer.querySelectorAll('.cp-preset').forEach(function (btn) {
    btn.addEventListener('click', function () {
      $('cp-base-url').value = btn.getAttribute('data-url') || '';
      $('cp-model-name').value = btn.getAttribute('data-model') || '';
    });
  });

  if (saveCfgBtn) {
    saveCfgBtn.addEventListener('click', function () {
      var url = ($('cp-base-url').value || '').trim();
      var model = ($('cp-model-name').value || '').trim();
      var key = ($('cp-api-key').value || '').trim();
      if (cfgMsg) cfgMsg.textContent = i18n('saving', '…');
      var body = { base_url: url, model: model };
      if (key !== '') body.api_key = key;

      fetch('/api/copilot/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (!res.ok) {
            if (cfgMsg) cfgMsg.textContent = res.error || i18n('saveFailedPlain', 'Failed');
            return;
          }
          if (cfgMsg) cfgMsg.textContent = '';
          if (modelLabel && res.status) modelLabel.textContent = res.status.model;
          showSetup(false);
          loadHistory();
        })
        .catch(function (e) { if (cfgMsg) cfgMsg.textContent = String(e); });
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener('click', function () {
      fetch('/api/copilot/clear', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
        .then(function () {
          if (msgsBox) msgsBox.textContent = '';
          historyLoaded = false;
        });
    });
  }

  drawer.querySelectorAll('.cp-ask').forEach(function (chip) {
    chip.addEventListener('click', function () {
      sendQuestion(chip.getAttribute('data-q'));
    });
  });

  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('[data-ask-copilot]');
    if (!btn) return;
    e.preventDefault();
    window.openCopilot(
      btn.getAttribute('data-ask-copilot') || '',
      btn.getAttribute('data-symbol') || ''
    );
  });

  if (sendBtn) sendBtn.addEventListener('click', function () { sendQuestion(); });
  if (inputEl) {
    inputEl.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendQuestion();
      }
    });
  }

  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      setOpen(!document.body.classList.contains('copilot-open'));
    }
  });

  try {
    if (localStorage.getItem(STORE_KEY) === '1') setOpen(true);
  } catch (e) {}
})();
