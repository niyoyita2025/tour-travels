// Show / hide password. Adds an eye button inside every password field on the page.
// Progressive enhancement: without JavaScript the fields still work, they just have no button.
(function () {
  'use strict';

  var EYE_OPEN = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
    '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
  var EYE_SLASHED = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
    '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>' +
    '<path d="M3 3l18 18"/></svg>';

  function enhance(input) {
    if (input.closest('.pw-field')) return;

    var wrap = document.createElement('span');
    wrap.className = 'pw-field';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'pw-toggle';
    button.setAttribute('aria-controls', input.id || '');
    button.setAttribute('aria-pressed', 'false');
    wrap.appendChild(button);

    function render(visible) {
      button.innerHTML = visible ? EYE_SLASHED : EYE_OPEN;
      var label = visible ? 'Hide password' : 'Show password';
      button.setAttribute('aria-label', label);
      button.setAttribute('title', label);
      button.setAttribute('aria-pressed', visible ? 'true' : 'false');
    }

    render(false);
    button.addEventListener('click', function () {
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      render(show);
      input.focus({ preventScroll: true });  // keep typing where the person was
    });
  }

  document.querySelectorAll('input[type="password"]').forEach(enhance);
})();
