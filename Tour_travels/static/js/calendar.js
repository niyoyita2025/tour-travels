// Live booking calendar. Refreshes availability from the server every 20 seconds.
//
// This script only updates what the page shows. The server re-checks every seat
// when a booking is saved, so a stale calendar can never cause a double booking.
(function () {
  var root = document.getElementById('calendar');
  if (!root) return;

  var url = root.getAttribute('data-availability-url');
  var month = root.getAttribute('data-month');
  var updated = document.getElementById('cal-updated');
  var REFRESH_MS = 20000;

  function setStatusClass(el, status) {
    Array.prototype.slice.call(el.classList).forEach(function (cls) {
      if (cls.indexOf('day-') === 0) el.classList.remove(cls);
    });
    el.classList.add('day-' + status);
  }

  function dayText(day) {
    if (day.status === 'available' || day.status === 'limited') {
      return day.remaining + (day.remaining === 1 ? ' seat left' : ' seats left');
    }
    if (day.status === 'full') return 'Fully booked';
    return day.label;
  }

  function apply(data) {
    document.querySelectorAll('[data-date]').forEach(function (el) {
      var day = data.days[el.getAttribute('data-date')];
      if (!day || el.classList.contains('is-outside')) return;
      setStatusClass(el, day.status);
      var meta = el.querySelector('.meta');
      if (meta) meta.textContent = dayText(day);
      var selectable = day.status === 'available' || day.status === 'limited';
      if (selectable) {
        el.setAttribute('href', el.getAttribute('data-href'));
        el.removeAttribute('aria-disabled');
        el.classList.remove('is-disabled');
      } else {
        el.removeAttribute('href');
        el.setAttribute('aria-disabled', 'true');
        el.classList.add('is-disabled');
      }
    });

    document.querySelectorAll('[data-slot-id]').forEach(function (li) {
      var slot = data.slots[li.getAttribute('data-slot-id')];
      if (!slot) return;
      var remaining = li.querySelector('[data-slot-remaining]');
      if (remaining) remaining.textContent = slot.remaining;
      var button = li.querySelector('[data-slot-book]');
      if (button && !slot.bookable) {
        button.style.display = 'none';
        var note = li.querySelector('[data-slot-note]');
        if (note) note.textContent = 'No longer available';
        li.classList.add('is-disabled');
      }
    });

    if (updated) {
      updated.textContent = 'Updated ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
  }

  function refresh() {
    fetch(url + '?month=' + encodeURIComponent(month), {
      headers: { 'Accept': 'application/json' },
      credentials: 'same-origin',
    })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) { if (data) apply(data); })
      .catch(function () { /* keep the last known state; try again next time */ });
  }

  setInterval(refresh, REFRESH_MS);
})();
