// Site behaviour. Progressive enhancement: pages work without JavaScript.
(function () {
  // Mobile navigation and dashboard sidebar toggles
  function bindToggle(buttonId, panelId, linkSelector) {
    var button = document.getElementById(buttonId);
    var panel = document.getElementById(panelId);
    if (!button || !panel) return;

    button.addEventListener('click', function () {
      var open = panel.classList.toggle('open');
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    panel.querySelectorAll(linkSelector).forEach(function (a) {
      a.addEventListener('click', function () {
        panel.classList.remove('open');
        button.setAttribute('aria-expanded', 'false');
      });
    });
  }
  bindToggle('menuBtn', 'navLinks', 'a');
  bindToggle('dashToggle', 'dashSidebar', 'a');

  // Fixed header: add a shadow once the page has scrolled, so it stands out from the content under it.
  var nav = document.getElementById('siteNav');
  if (nav) {
    var onScroll = function () { nav.classList.toggle('is-scrolled', window.scrollY > 8); };
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }

  // Dark / light mode. The page starts in the saved or device theme (see the script in base.html).
  var KEY = 'er-theme';
  var root = document.documentElement;
  var button = document.getElementById('themeToggle');

  function describe(theme) {
    if (!button) return;
    var dark = theme === 'dark';
    var icon = button.querySelector('span');
    if (icon) icon.textContent = dark ? '☀️' : '🌙';
    button.setAttribute('aria-label', dark ? 'Switch to light mode' : 'Switch to dark mode');
    button.setAttribute('aria-pressed', dark ? 'true' : 'false');
  }

  describe(root.getAttribute('data-theme') || 'light');
  if (button) {
    button.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem(KEY, next); } catch (e) { /* storage blocked: the choice lasts this visit */ }
      describe(next);
    });
  }
})();
