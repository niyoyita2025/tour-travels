// Sensor monitoring for administrators: live device values, alert pop-ups, and an optional alert sound.
// Loaded on every admin page. It only reads from the server; it never changes data.
(function () {
  'use strict';

  var FEED_URL = '/manage/iot/alerts/feed.json';
  var LIVE_URL = '/manage/iot/live.json';
  var lastAlertId = null;
  var soundOn = false;
  var audio = null;
  var stack = null;
  var soundButton = document.getElementById('iotSoundBtn');

  function node(tag, className, text) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = text;
    return element;
  }

  function toastStack() {
    if (!stack) {
      stack = node('div', 'iot-toasts');
      document.body.appendChild(stack);
    }
    return stack;
  }

  // Three short tones. Browsers only allow sound after a click, so it starts with the "Turn on alert sounds" button.
  function beep() {
    if (!soundOn) return;
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      var start = audio.currentTime;
      [0, 0.35, 0.7].forEach(function (offset) {
        var oscillator = audio.createOscillator();
        var gain = audio.createGain();
        oscillator.type = 'square';
        oscillator.frequency.value = 880;
        gain.gain.value = 0.08;
        oscillator.connect(gain);
        gain.connect(audio.destination);
        oscillator.start(start + offset);
        oscillator.stop(start + offset + 0.18);
      });
    } catch (error) {
      // Sound is blocked on this device. The on-screen notice still appears.
    }
  }

  function showAlert(alert) {
    var box = node('div', 'iot-toast', null);
    box.setAttribute('role', 'alert');
    box.appendChild(node('strong', '', 'Sensor alert · ' + alert.kind));
    box.appendChild(node('div', '', alert.message));
    box.appendChild(node('small', '', alert.opened));
    var close = node('button', 'iot-toast-close', '×');
    close.type = 'button';
    close.setAttribute('aria-label', 'Dismiss this alert');
    close.addEventListener('click', function () { box.remove(); });
    box.appendChild(close);
    toastStack().appendChild(box);
  }

  function pollAlerts() {
    var url = FEED_URL + (lastAlertId === null ? '' : '?after=' + lastAlertId);
    fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) {
        if (!data) return;
        if (lastAlertId !== null && data.alerts.length) {
          data.alerts.forEach(showAlert);
          beep();
        }
        lastAlertId = data.latest_id;
      })
      .catch(function () { /* offline for a moment: try again on the next poll */ });
  }

  function renderDevices(devices) {
    var body = document.getElementById('iotDeviceRows');
    if (!body) return;
    body.textContent = '';
    if (!devices.length) {
      var empty = node('tr', '', null);
      var cell = node('td', '', 'No devices yet.');
      cell.colSpan = 4;
      empty.appendChild(cell);
      body.appendChild(empty);
      return;
    }
    devices.forEach(function (device) {
      var row = node('tr', '', null);
      row.setAttribute('data-device-id', device.id);

      var nameCell = node('td', '', null);
      nameCell.appendChild(node('strong', '', device.name));
      nameCell.appendChild(document.createElement('br'));
      nameCell.appendChild(node('small', '', device.device_id + (device.location ? ' · ' + device.location : '')));
      row.appendChild(nameCell);

      var statusCell = node('td', '', null);
      statusCell.appendChild(node('span', 'status ' + (device.status === 'online' ? 'confirmed' : 'cancelled'),
        device.status_label));
      row.appendChild(statusCell);

      row.appendChild(node('td', '', device.last_seen));

      var valuesCell = node('td', '', null);
      var keys = Object.keys(device.values);
      if (!keys.length) valuesCell.appendChild(node('small', '', 'No readings yet'));
      keys.forEach(function (key) {
        var value = device.values[key];
        var line = node('div', '', value.label + ': ');
        line.appendChild(node('strong', '', value.value + ' ' + value.unit));
        line.appendChild(document.createTextNode(' '));
        line.appendChild(node('small', '', '(' + value.at + ')'));
        valuesCell.appendChild(line);
      });
      row.appendChild(valuesCell);
      body.appendChild(row);
    });
  }

  function refreshLive() {
    fetch(LIVE_URL, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) {
        if (!data) return;
        renderDevices(data.devices);
        var online = data.devices.filter(function (d) { return d.status === 'online'; }).length;
        var setText = function (id, value) {
          var target = document.getElementById(id);
          if (target) target.textContent = String(value);
        };
        setText('iotOnline', online);
        setText('iotOffline', data.devices.length - online);
        setText('iotOpen', data.open_count);
      })
      .catch(function () { /* try again on the next refresh */ });
  }

  if (soundButton) {
    soundButton.addEventListener('click', function () {
      soundOn = !soundOn;
      soundButton.setAttribute('aria-pressed', soundOn ? 'true' : 'false');
      soundButton.textContent = soundOn ? 'Alert sounds are on' : 'Turn on alert sounds';
      if (soundOn) beep();  // a click gives the browser permission to play sound
    });
  }

  pollAlerts();
  window.setInterval(pollAlerts, 20000);
  if (document.getElementById('iotDeviceRows')) {
    window.setInterval(refreshLive, 10000);
  }
})();
