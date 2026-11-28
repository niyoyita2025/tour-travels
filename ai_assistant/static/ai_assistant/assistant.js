// Travel assistant chat for visitors. It only shows text the server returned, and sends the visitor's question.
(function () {
  'use strict';

  var box = document.getElementById('assistant');
  if (!box) return;

  var toggle = document.getElementById('assistantToggle');
  var panel = document.getElementById('assistantPanel');
  var closeButton = document.getElementById('assistantClose');
  var log = document.getElementById('assistantLog');
  var suggest = document.getElementById('assistantSuggest');
  var form = document.getElementById('assistantForm');
  var input = document.getElementById('assistantInput');
  var askUrl = box.getAttribute('data-ask-url');
  var csrf = box.getAttribute('data-csrf');
  var greeted = false;

  function message(text, fromBot) {
    var item = document.createElement('div');
    item.className = 'assistant-msg ' + (fromBot ? 'is-bot' : 'is-user');
    item.textContent = text;
    log.appendChild(item);
    log.scrollTop = log.scrollHeight;
    return item;
  }

  function addLinks(links) {
    if (!links || !links.length) return;
    var holder = document.createElement('div');
    holder.className = 'assistant-links';
    links.forEach(function (link) {
      // Only paths on this website are linked.
      if (typeof link.url !== 'string' || link.url.charAt(0) !== '/' || link.url.charAt(1) === '/') return;
      var a = document.createElement('a');
      a.href = link.url;
      a.textContent = link.label;
      holder.appendChild(a);
    });
    log.appendChild(holder);
    log.scrollTop = log.scrollHeight;
  }

  function setSuggestions(list) {
    suggest.textContent = '';
    (list || []).forEach(function (text) {
      var button = document.createElement('button');
      button.type = 'button';
      button.textContent = text;
      button.addEventListener('click', function () { ask(text); });
      suggest.appendChild(button);
    });
  }

  function ask(question) {
    var text = (question || '').trim();
    if (!text) return;
    message(text, false);
    input.value = '';
    var waiting = message('Looking that up…', true);
    fetch(askUrl, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRFToken': csrf },
      body: JSON.stringify({ message: text })
    })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          return { ok: response.ok, data: data };
        });
      })
      .then(function (result) {
        waiting.remove();
        if (!result.ok) {
          message(result.data.error || 'Sorry, the assistant is not available right now. Please contact us.', true);
          return;
        }
        message(result.data.answer, true);
        addLinks(result.data.links);
        setSuggestions(result.data.suggestions);
      })
      .catch(function () {
        waiting.remove();
        message('Sorry, something went wrong. Please check your connection and try again.', true);
      });
  }

  function open() {
    panel.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    if (!greeted) {
      greeted = true;
      message(box.getAttribute('data-greeting'), true);
      setSuggestions(['What tours do you offer?', 'Which destinations can I visit?', 'How much does a tour cost?',
        'Which dates are open?', 'How do I book?']);
    }
    input.focus();
  }

  function close() {
    panel.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    toggle.focus();
  }

  toggle.addEventListener('click', function () { if (panel.hidden) { open(); } else { close(); } });
  closeButton.addEventListener('click', close);
  form.addEventListener('submit', function (event) { event.preventDefault(); ask(input.value); });
  document.addEventListener('keydown', function (event) { if (event.key === 'Escape' && !panel.hidden) close(); });
})();
