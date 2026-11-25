// Language selector: a globe button that shows the current language code (EN, RW, FR or SW) and opens a menu.
// Translation itself is done by Google Translate in the visitor's browser. Choosing a language records it in the
// `googtrans` cookie, which Google's translation script reads when the page loads, so the page reloads translated.
(function () {
  'use strict';

  var COOKIE = 'googtrans';
  var LANGUAGES = { en: 'English', rw: 'Kinyarwanda', fr: 'French', sw: 'Swahili' };

  function currentLanguage() {
    var match = document.cookie.match(/(?:^|;\s*)googtrans=([^;]*)/);
    if (match) {
      var target = decodeURIComponent(match[1]).split('/')[2];
      if (LANGUAGES[target]) return target;
    }
    return 'en';
  }

  function translationReady() {
    return !!(window.google && google.translate && google.translate.TranslateElement);
  }

  function setLanguage(code) {
    var past = 'Thu, 01 Jan 1970 00:00:00 GMT';
    if (code === 'en') {
      // English is the page's own language, so remove the translation cookie.
      document.cookie = COOKIE + '=; expires=' + past + '; path=/';
      if (location.hostname) {
        document.cookie = COOKIE + '=; expires=' + past + '; path=/; domain=' + location.hostname;
      }
    } else {
      document.cookie = COOKIE + '=/en/' + code + '; path=/';
    }
    location.reload();
  }

  function setup(picker) {
    var button = picker.querySelector('[data-lang-button]');
    var menu = picker.querySelector('[data-lang-menu]');
    var note = picker.querySelector('[data-lang-note]');
    var codeLabel = picker.querySelector('[data-lang-code]');
    var nameLabel = picker.querySelector('[data-lang-name]');
    var items = Array.prototype.slice.call(picker.querySelectorAll('[data-lang-option]'));
    var current = currentLanguage();

    function showCurrent() {
      codeLabel.textContent = current.toUpperCase();
      nameLabel.textContent = 'Language: ' + LANGUAGES[current];
      items.forEach(function (item) {
        if (item.getAttribute('data-lang-option') === current) {
          item.setAttribute('aria-current', 'true');
        } else {
          item.removeAttribute('aria-current');
        }
      });
    }

    function open(focusItem) {
      menu.hidden = false;
      button.setAttribute('aria-expanded', 'true');
      var selected = items.filter(function (item) { return item.getAttribute('aria-current') === 'true'; })[0];
      (focusItem === 'last' ? items[items.length - 1] : selected || items[0]).focus();
    }

    function close(returnFocus) {
      menu.hidden = true;
      button.setAttribute('aria-expanded', 'false');
      if (returnFocus) button.focus();
    }

    button.addEventListener('click', function () {
      if (menu.hidden) { open(); } else { close(false); }
    });
    button.addEventListener('keydown', function (event) {
      if (event.key === 'ArrowDown') { event.preventDefault(); open(); }
      if (event.key === 'ArrowUp') { event.preventDefault(); open('last'); }
    });

    items.forEach(function (item, index) {
      item.addEventListener('click', function () {
        var code = item.getAttribute('data-lang-option');
        if (code === current) { close(true); return; }
        if (code !== 'en' && !translationReady()) {
          note.hidden = false;  // translation has not loaded: say so, rather than doing nothing
          return;
        }
        setLanguage(code);
      });
      item.addEventListener('keydown', function (event) {
        if (event.key === 'ArrowDown') { event.preventDefault(); items[(index + 1) % items.length].focus(); }
        if (event.key === 'ArrowUp') { event.preventDefault(); items[(index - 1 + items.length) % items.length].focus(); }
        if (event.key === 'Escape') { event.preventDefault(); close(true); }
        if (event.key === 'Tab') { close(false); }
      });
    });

    document.addEventListener('click', function (event) {
      if (!menu.hidden && !picker.contains(event.target)) close(false);
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !menu.hidden) close(true);
    });

    showCurrent();
  }

  document.querySelectorAll('[data-lang-picker]').forEach(setup);
})();
