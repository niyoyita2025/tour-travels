"""
Background photos are cropped to their section's frame when they are uploaded, so the administrator
never has to crop by hand. Covers the crop itself, and the Control Panel upload that uses it.
"""

import io
import shutil
import tempfile

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from .image_frames import BACKGROUND_FRAMES, DEFAULT_FRAME, fit_to_frame, frame_for
from .models import BackgroundImage
from .tests import DataMixin


def picture(size, mode='RGB', fmt='PNG', colour=(18, 61, 50)):
    buffer = io.BytesIO()
    Image.new(mode, size, colour).save(buffer, format=fmt)
    return buffer.getvalue()


def cropped(data, frame):
    return Image.open(io.BytesIO(fit_to_frame(io.BytesIO(data), frame)))


class FrameCropTests(TestCase):
    def test_a_wide_photo_takes_the_intro_frame_exactly(self):
        self.assertEqual(cropped(picture((3000, 1000)), BACKGROUND_FRAMES['intro']).size, (1100, 760))

    def test_a_portrait_photo_is_cropped_to_the_wide_closing_banner(self):
        # 1000 px wide, cropped to the banner's 1920 : 560 proportion
        self.assertEqual(cropped(picture((1000, 2000)), BACKGROUND_FRAMES['final_cta']).size, (1000, 292))

    def test_a_small_photo_is_never_enlarged(self):
        self.assertEqual(cropped(picture((400, 300)), BACKGROUND_FRAMES['hero']).size, (400, 225))

    def test_the_result_is_a_jpeg(self):
        image = cropped(picture((800, 600)), DEFAULT_FRAME)
        self.assertEqual(image.format, 'JPEG')
        self.assertEqual(image.mode, 'RGB')

    def test_transparent_areas_become_white(self):
        image = cropped(picture((800, 400), mode='RGBA', colour=(0, 0, 0, 0)), BACKGROUND_FRAMES['about'])
        self.assertTrue(min(image.getpixel((5, 5))) >= 250)

    def test_every_section_has_a_frame_and_unknown_sections_get_the_default(self):
        for section in BackgroundImage.Section.values:
            self.assertIn(section, BACKGROUND_FRAMES)
        self.assertEqual(frame_for('nonsense'), DEFAULT_FRAME)


class BackgroundUploadTests(DataMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.media_root = tempfile.mkdtemp()
        self._media = override_settings(MEDIA_ROOT=self.media_root)
        self._media.enable()

    def tearDown(self):
        self._media.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_an_upload_is_stored_already_cropped_to_its_section(self):
        BackgroundImage.objects.create(section='intro', image=SimpleUploadedFile(
            'shot.png', picture((3000, 1000)), content_type='image/png'))
        row = BackgroundImage.objects.get(section='intro')
        self.assertTrue(row.image.name.endswith('.jpg'))
        with Image.open(row.image.path) as stored:
            self.assertEqual(stored.size, BACKGROUND_FRAMES['intro'])

    def test_the_control_panel_crops_the_upload_and_says_so(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('background_save', args=['final_cta']), {
            'image': SimpleUploadedFile('banner.png', picture((2400, 1200)), content_type='image/png')}, follow=True)
        self.assertContains(response, 'cropped to 1920 × 560 px')
        with Image.open(BackgroundImage.objects.get(section='final_cta').image.path) as stored:
            self.assertEqual(stored.size, (1920, 560))

    def test_the_control_panel_shows_the_best_size_for_each_section(self):
        self.client.force_login(self.staff)
        page = self.client.get(reverse('background_manage'))
        self.assertContains(page, 'Best size: 1100 × 760 px')
        self.assertContains(page, 'Best size: 1920 × 560 px')
