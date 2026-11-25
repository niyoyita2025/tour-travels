// Email code entry (sign-up verification and password reset).
// Progressive enhancement: every form still works without this script.
(function () {
  'use strict';

  function digitsOnly(text) {
    return (text || '').replace(/\D/g, '');
  }

  function formatTime(seconds) {
    var m = Math.floor(seconds / 60);
    var s = seconds % 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  // Six code boxes: type, paste or autofill a code; focus moves on; the form submits when all six are filled.
  function setupCodeBoxes(form) {
    var boxes = Array.prototype.slice.call(form.querySelectorAll('.otp-box'));
    if (!boxes.length) return;

    function refreshFilled() {
      boxes.forEach(function (box) {
        box.classList.toggle('is-filled', box.value !== '');
      });
    }

    function complete() {
      return boxes.every(function (box) { return box.value !== ''; });
    }

    function submitIfComplete() {
      if (complete() && !form.hasAttribute('data-submitted')) {
        form.setAttribute('data-submitted', '1');
        if (form.requestSubmit) { form.requestSubmit(); } else { form.submit(); }
      }
    }

    // Writes digits starting at `start` and returns the index of the last box written.
    function spread(start, digits) {
      var last = start;
      for (var i = 0; i < digits.length && start + i < boxes.length; i++) {
        boxes[start + i].value = digits.charAt(i);
        last = start + i;
      }
      return last;
    }

    boxes.forEach(function (box, index) {
      box.addEventListener('focus', function () { box.select(); });

      box.addEventListener('input', function () {
        var digits = digitsOnly(box.value);
        if (digits.length > 1) {
          // More than one character arrived at once: a paste or a one-time-code autofill.
          var last = spread(index, digits);
          boxes[Math.min(last + 1, boxes.length - 1)].focus();
        } else {
          box.value = digits;
          if (digits && index < boxes.length - 1) boxes[index + 1].focus();
        }
        refreshFilled();
        submitIfComplete();
      });

      box.addEventListener('keydown', function (event) {
        if (event.key === 'Backspace' && box.value === '' && index > 0) {
          event.preventDefault();
          boxes[index - 1].value = '';
          boxes[index - 1].focus();
          refreshFilled();
        } else if (event.key === 'ArrowLeft' && index > 0) {
          event.preventDefault();
          boxes[index - 1].focus();
        } else if (event.key === 'ArrowRight' && index < boxes.length - 1) {
          event.preventDefault();
          boxes[index + 1].focus();
        }
      });

      box.addEventListener('paste', function (event) {
        var text = event.clipboardData ? event.clipboardData.getData('text') : '';
        var digits = digitsOnly(text);
        if (!digits) return;
        event.preventDefault();
        var last = spread(index, digits);
        boxes[Math.min(last + 1, boxes.length - 1)].focus();
        refreshFilled();
        submitIfComplete();
      });
    });

    refreshFilled();
  }

  // Live countdowns: code expiry (m:ss) and the wait before a new code can be requested.
  function runCountdowns() {
    var expiry = document.querySelector('[data-countdown-expiry]');
    var resendButton = document.querySelector('[data-resend]');
    var resendLabel = document.querySelector('[data-countdown-resend]');
    var expiryEnd = expiry ? Date.now() + Number(expiry.getAttribute('data-seconds')) * 1000 : 0;
    var resendEnd = resendButton ? Date.now() + Number(resendButton.getAttribute('data-seconds')) * 1000 : 0;
    if (!expiryEnd && !resendEnd) return;

    function tick() {
      var now = Date.now();
      if (expiry) {
        var left = Math.ceil((expiryEnd - now) / 1000);
        if (left > 0) {
          expiry.textContent = formatTime(left);
        } else {
          expiry.parentNode.textContent = 'This code has expired. Request a new one below.';
          expiry = null;
        }
      }
      if (resendButton) {
        var wait = Math.ceil((resendEnd - now) / 1000);
        if (wait > 0) {
          if (resendLabel) resendLabel.textContent = String(wait);
        } else {
          resendButton.disabled = false;
          resendButton.textContent = 'Resend code';
          resendButton = null;
        }
      }
      if (expiry || resendButton) {
        window.setTimeout(tick, 1000);
      }
    }
    tick();
  }

  // Show a busy state on any form marked data-busy-text, and stop double submissions.
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-busy-text')) return;
    if (form.getAttribute('data-busy') === '1') {
      event.preventDefault();
      return;
    }
    form.setAttribute('data-busy', '1');
    form.setAttribute('aria-busy', 'true');
    var button = form.querySelector('button[type="submit"]');
    if (button) {
      button.disabled = true;
      button.classList.add('is-busy');
      button.textContent = form.getAttribute('data-busy-text');
    }
  }, true);

  // Returning to a page with the back button must not leave a form stuck in the busy state.
  window.addEventListener('pageshow', function (event) {
    if (!event.persisted) return;
    document.querySelectorAll('form[data-busy="1"]').forEach(function (form) {
      form.removeAttribute('data-busy');
      form.removeAttribute('aria-busy');
      form.removeAttribute('data-submitted');
      var button = form.querySelector('button[type="submit"]');
      if (button) { button.disabled = false; button.classList.remove('is-busy'); }
    });
  });

  document.querySelectorAll('.otp-form').forEach(setupCodeBoxes);
  runCountdowns();
})();
