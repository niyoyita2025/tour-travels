"""
Background photos are cropped to the frame their section shows on the website, so the administrator
never has to crop or resize by hand. Upload any photo: it is centred, cropped to the frame's proportions,
scaled to the frame's width (never enlarged) and saved as a JPEG.

The frames match the default design: the intro photo is a card about 1.45 : 1, the closing banner is a
wide strip about 3.4 : 1, and the page headers are short full-width bands.
"""

import os
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, ImageOps

DEFAULT_FRAME = (1800, 900)

BACKGROUND_FRAMES = {
    'hero': (1920, 1080),
    'intro': (1100, 760),
    'destinations': (1600, 900),
    'experiences': (1600, 900),
    'culture': (1600, 900),
    'booking': (1600, 900),
    'final_cta': (1920, 560),
    'tours_page': (1800, 450),
    'destinations_page': (1800, 450),
    'about': (1800, 450),
    'contact': (1800, 450),
    'faq': (1800, 450),
    'details': (1800, 450),
}


def frame_for(section):
    """(width, height) in pixels of the frame this section's photo is cropped to."""
    return BACKGROUND_FRAMES.get(section, DEFAULT_FRAME)


def fit_to_frame(source, frame):
    """
    Return JPEG bytes of `source` (a file-like image) cropped to `frame` = (width, height).
    Phone photos are turned upright, transparent areas become white, and the photo is never enlarged
    beyond its own width.
    """
    width, height = frame
    source.seek(0)
    image = ImageOps.exif_transpose(Image.open(source))
    if image.mode in ('RGBA', 'LA', 'P'):
        rgba = image.convert('RGBA')
        flat = Image.new('RGB', rgba.size, (255, 255, 255))
        flat.paste(rgba, mask=rgba.getchannel('A'))
        image = flat
    else:
        image = image.convert('RGB')

    target_width = min(width, image.width)
    target_height = max(1, round(target_width * height / width))
    fitted = ImageOps.fit(image, (target_width, target_height), method=Image.Resampling.LANCZOS,
                          centering=(0.5, 0.5))
    out = BytesIO()
    fitted.save(out, format='JPEG', quality=85, optimize=True, progressive=True)
    return out.getvalue()


def fitted_upload(source, name, section):
    """A new upload for `section`: the same photo cropped to the section's frame, named with a .jpg extension."""
    data = fit_to_frame(source, frame_for(section))
    stem = os.path.splitext(os.path.basename(name))[0] or 'background'
    return SimpleUploadedFile(f'{stem}.jpg', data, content_type='image/jpeg')
