"""R0 image receipt inspection; source identity and decoded cache identity differ.

SPDX-License-Identifier: MIT
No upstream native code is included. A display derivative is never certified as
source-identical or eligible for source-hash-based cache reuse.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image


def inspect_kernel_image(*, root, completion, message_id, element_id, picture):
    if str(completion["msgId"]) != str(message_id) or str(completion["msgElementId"]) != str(element_id):
        raise ValueError("Download completion does not belong to this image")
    if str(completion["fileErrCode"]) != "0" or str(completion["fileSrvErrCode"]) != "0":
        raise ValueError("Kernel image download failed")
    location = Path(completion["filePath"]).resolve(strict=True)
    if not location.is_relative_to(Path(root).resolve(strict=True)):
        raise ValueError("Image path is outside this probe")
    data = location.read_bytes()
    md5 = hashlib.md5(data).hexdigest()
    sha256 = hashlib.sha256(data).hexdigest()
    with Image.open(location) as check:
        check.verify()
    with Image.open(location) as image:
        image.load()
        dimensions = (image.width, image.height)
        image_format = image.format
    if dimensions != (int(picture["picWidth"]), int(picture["picHeight"])):
        raise ValueError("Delivered dimensions differ from message; not a full-size display image")
    matches = md5 == picture["md5HexStr"].lower() and len(data) == int(picture["fileSize"])
    if picture["original"] and not matches:
        raise ValueError("Original image bytes do not match the declared source")
    # Non-original images can be converted by the kernel (observed AVIF -> JPEG).
    # A matching callback and successful decode certify delivery/readability only.
    # They do NOT make an unmatched source checksum pass.
    return {
        "delivery_verified": True,
        "source_bytes_verified": matches,
        "representation": "source_bytes" if matches else "kernel_display_derivative",
        "original_requested": bool(picture["original"]),
        "local_md5": md5,
        "local_sha256": sha256,
        "local_bytes": len(data),
        "format": image_format,
        "dimensions": list(dimensions),
        "declared_md5": picture["md5HexStr"],
        "declared_bytes": int(picture["fileSize"]),
        "cache_content_key": sha256,
        "may_label_as_original": bool(picture["original"]) and matches,
    }
