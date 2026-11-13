# SPDX-License-Identifier: MIT
import hashlib
import tempfile
import unittest
from pathlib import Path

from image_integrity import inspect_kernel_image
from PIL import Image


class ImageIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / "image.png"
        Image.new("RGB", (12, 10), (10, 80, 200)).save(self.file)
        data = self.file.read_bytes()
        self.picture = dict(
            picWidth=12,
            picHeight=10,
            original=True,
            md5HexStr=hashlib.md5(data).hexdigest(),
            fileSize=str(len(data)),
        )
        self.done = dict(
            msgId="9007199254740993",
            msgElementId="9007199254740994",
            fileErrCode="0",
            fileSrvErrCode="0",
            filePath=str(self.file),
        )

    def inspect(self):
        return inspect_kernel_image(
            root=self.root,
            completion=self.done,
            message_id="9007199254740993",
            element_id="9007199254740994",
            picture=self.picture,
        )

    def test_original_checks_both_digest_and_size(self):
        self.assertTrue(self.inspect()["source_bytes_verified"])
        self.picture["fileSize"] = "1"
        with self.assertRaisesRegex(ValueError, "Original"):
            self.inspect()

    def test_nonoriginal_matching_bytes_are_verified(self):
        self.picture["original"] = False
        self.assertTrue(self.inspect()["source_bytes_verified"])
        self.assertFalse(self.inspect()["may_label_as_original"])

    def test_display_derivative_does_not_pass_source_checksum(self):
        self.picture.update(original=False, md5HexStr="0" * 32, fileSize="1")
        result = self.inspect()
        self.assertEqual(result["representation"], "kernel_display_derivative")
        self.assertFalse(result["source_bytes_verified"])
        self.assertFalse(result["may_label_as_original"])
        self.assertEqual(result["cache_content_key"], hashlib.sha256(self.file.read_bytes()).hexdigest())

    def test_original_digest_mismatch_is_rejected(self):
        self.picture["md5HexStr"] = "0" * 32
        with self.assertRaisesRegex(ValueError, "Original"):
            self.inspect()

    def test_wrong_callback_cannot_certify_image(self):
        self.done["msgElementId"] = "9007199254740995"
        with self.assertRaisesRegex(ValueError, "belong"):
            self.inspect()

    def test_server_failure_is_not_a_cached_success(self):
        self.done["fileSrvErrCode"] = "5"
        with self.assertRaisesRegex(ValueError, "failed"):
            self.inspect()

    def test_invalid_image_is_rejected_even_for_display(self):
        self.picture["original"] = False
        self.file.write_bytes(b"not an image")
        with self.assertRaises(OSError):
            self.inspect()

    def test_thumbnail_cannot_be_mistaken_for_full_image(self):
        self.picture.update(original=False, picWidth=120)
        with self.assertRaisesRegex(ValueError, "dimensions"):
            self.inspect()

    def test_path_outside_probe_is_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            target = Path(other) / "image.png"
            target.write_bytes(self.file.read_bytes())
            self.done["filePath"] = str(target)
            with self.assertRaisesRegex(ValueError, "outside"):
                self.inspect()


if __name__ == "__main__":
    unittest.main()
