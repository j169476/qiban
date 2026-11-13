import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from companion_core.errors import AssistantError
from companion_core.file_process import parse_image
from companion_core.image_reader import extract_image


def animation(fmt='GIF'):
    out = io.BytesIO()
    frames = [Image.new('RGB', (12, 10), color) for color in ('red', 'green', 'blue', 'yellow', 'black')]
    frames[0].save(out, format=fmt, save_all=True, append_images=frames[1:], duration=100, loop=0)
    return out.getvalue()


@pytest.mark.parametrize('slots,indices', [(1, [0]), (2, [0, 4]), (3, [0, 2, 4])])
@pytest.mark.parametrize('fmt', ['GIF', 'WEBP', 'PNG'])
def test_animation_samples_expected_positions_and_visible_pixels(slots, indices, fmt):
    result = extract_image(animation(fmt), slots)
    assert result['total_frames'] == 5 and result['omitted_frames'] == 5-len(indices)
    assert [frame['index'] for frame in result['frames']] == indices
    assert result['budget_limited'] == (slots < 3)
    for frame in result['frames']:
        with Image.open(io.BytesIO(base64.b64decode(frame['png']))) as normalized:
            assert normalized.format == 'PNG' and normalized.mode == 'RGB'
            assert not normalized.info
            expected = [(255, 0, 0), (0, 128, 0), (0, 0, 255), (255, 255, 0), (0, 0, 0)][frame['index']]
            assert all(abs(a-b) < 8 for a, b in zip(normalized.getpixel((5, 5)), expected))


def test_transparent_pixels_do_not_reveal_hidden_rgb_and_static_uses_one_slot():
    out = io.BytesIO()
    Image.new('RGBA', (3000, 10), (255, 0, 0, 0)).save(out, format='PNG')
    result = extract_image(out.getvalue(), 3)
    assert len(result['frames']) == 1 and not result['sampled']
    with Image.open(io.BytesIO(base64.b64decode(result['frames'][0]['png']))) as image:
        assert image.width == 2048 and image.getpixel((0, 0)) == (255, 255, 255)


def test_real_worker_and_model_copy_accept_memory_images_without_original_paths(tmp_path):
    from companion_codex.app_server import copy_inputs

    result = parse_image(animation(), slots=2, guard=lambda: None)
    images = [base64.b64decode(frame['png']) for frame in result['frames']]
    copied = copy_inputs(SimpleNamespace(work=tmp_path), images)
    assert len(copied) == 2 and copied[0]['type'] == 'localImage'
    with Image.open(copied[1]['path']) as image:
        assert image.getpixel((0, 0)) == (0, 0, 0)


def test_worker_bad_data_is_explicit_unread():
    with pytest.raises(AssistantError) as error:
        parse_image(b'not a picture', slots=3, guard=lambda: None)
    assert error.value.code == 'image_process_failed'


def test_worker_cancellation_is_not_converted_to_decode_failure():
    calls = []
    def guard():
        calls.append(1)
        if len(calls) > 1:
            raise AssistantError('test_cancel', 'cancel')
    with pytest.raises(AssistantError) as error:
        parse_image(animation(), slots=3, guard=guard)
    assert error.value.code == 'test_cancel'


def test_worker_timeout_is_explicit():
    with pytest.raises(AssistantError) as error:
        parse_image(animation(), slots=3, guard=lambda: None, timeout=.000001)
    assert error.value.code == 'image_process_timeout'


def test_oversized_pixels_and_frame_count_are_rejected(monkeypatch):
    from companion_core import image_reader
    monkeypatch.setattr(image_reader, 'MAX_PIXELS', 10)
    with pytest.raises(ValueError):
        extract_image(animation(), 3)
    monkeypatch.setattr(image_reader, 'MAX_PIXELS', 1000)
    monkeypatch.setattr(image_reader, 'MAX_FRAMES', 4)
    with pytest.raises(ValueError):
        extract_image(animation(), 3)
