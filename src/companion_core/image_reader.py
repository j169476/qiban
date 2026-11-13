"""Bounded static/animated image normalization, without paths or model calls."""
import base64
import io
import warnings

from PIL import Image, ImageOps

MAX_PIXELS = 16_000_000
MAX_FRAMES = 1000
MAX_PNG = 8 * 1024 * 1024


def extract_image(content, slots):
    if type(slots) is not int or not 1 <= slots <= 3:
        raise ValueError('image_slots')
    if not isinstance(content, bytes) or not content or len(content) > 10 * 1024 * 1024:
        raise ValueError('image_bytes')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in {'PNG', 'JPEG', 'WEBP', 'GIF'} or image.width * image.height > MAX_PIXELS:
                raise ValueError('image_format_or_pixels')
            count = getattr(image, 'n_frames', 1)
            if not 1 <= count <= MAX_FRAMES:
                raise ValueError('image_frames_limit')
            # APNG's optional default image is a poster, not an animation frame.
            first = 1 if getattr(image, 'default_image', False) else 0
            last = count - 1
            desired = sorted({first, (first + last) // 2, last})
            selected = desired if slots >= len(desired) else ([first, last] if slots == 2 else [first])
            frames = []
            for index in selected:
                image.seek(index)
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError('image_pixels')
                image.load()
                oriented = ImageOps.exif_transpose(image)
                visible = Image.new('RGBA', oriented.size, 'white')
                visible.alpha_composite(oriented.convert('RGBA'))
                clean = Image.new('RGB', oriented.size)
                clean.paste(visible.convert('RGB'))
                clean.thumbnail((2048, 2048))
                output = io.BytesIO()
                clean.save(output, format='PNG')
                raw = output.getvalue()
                if len(raw) > MAX_PNG:
                    raise ValueError('image_output_limit')
                frames.append({'index': index-first, 'width': clean.width, 'height': clean.height,
                               'png': base64.b64encode(raw).decode('ascii')})
            return {'frames': frames, 'total_frames': count-first, 'omitted_frames': count-first-len(frames),
                    'sampled': count-first > len(frames), 'budget_limited': len(selected) < len(desired)}
