// Profile page: shows the chosen photo before saving, and counts the characters in "About you".
// The server checks every upload again, so this only makes the page friendlier.
(function () {
  'use strict';

  var MAX_BYTES = 5 * 1024 * 1024;
  var input = document.querySelector('input[name="photo"]');
  var preview = document.querySelector('[data-photo-preview] .avatar');
  var message = document.querySelector('[data-photo-message]');
  var previewUrl = null;

  if (input && preview) {
    input.addEventListener('change', function () {
      var file = input.files && input.files[0];
      if (!file) {
        message.textContent = '';
        return;
      }
      if (!/^image\/(jpeg|png|webp)$/.test(file.type)) {
        message.textContent = 'Use a JPG, PNG or WebP image.';
        input.value = '';
        return;
      }
      if (file.size > MAX_BYTES) {
        message.textContent = 'Choose a photo smaller than 5 MB.';
        input.value = '';
        return;
      }
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = URL.createObjectURL(file);
      preview.textContent = '';
      var img = document.createElement('img');
      img.src = previewUrl;
      img.alt = '';
      preview.appendChild(img);
      message.textContent = 'This is a preview. Save your profile to keep the photo.';
    });
  }

  document.querySelectorAll('[data-counter-for]').forEach(function (label) {
    var field = document.getElementById(label.getAttribute('data-counter-for'));
    if (!field) return;
    var max = Number(field.getAttribute('maxlength')) || 500;
    function update() {
      label.textContent = field.value.length + ' / ' + max;
    }
    field.addEventListener('input', update);
    update();
  });
})();
