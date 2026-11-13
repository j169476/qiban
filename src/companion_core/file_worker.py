"""One bounded file request over private pipes; no input/output file paths."""
import base64
import json
import sys

from .file_reader import Limits, extract_bytes

MAX_REQUEST = 14 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024
MAX_IMAGE_RESPONSE = 34 * 1024 * 1024


def main():
    raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if len(raw) > MAX_REQUEST:
        raise ValueError('request_limit')
    request = json.loads(raw)
    if (set(request) not in ({'name', 'content'}, {'name', 'content', 'image_slots'}) or not isinstance(request['name'], str)
            or len(request['name']) > 512):
        raise ValueError('request_shape')
    data = base64.b64decode(request['content'], validate=True)
    if len(data) > Limits().file_bytes:
        raise ValueError('file_limit')
    if 'image_slots' in request:
        from .image_reader import extract_image

        result = extract_image(data, request['image_slots'])
    else:
        result = extract_bytes(request['name'], data)
    output = json.dumps(result, ensure_ascii=False).encode('utf8')
    if len(output) > (MAX_IMAGE_RESPONSE if 'image_slots' in request else MAX_RESPONSE):
        raise ValueError('response_limit')
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
