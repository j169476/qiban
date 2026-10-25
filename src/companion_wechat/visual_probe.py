"""One-shot, offline OCR diagnostics; never captures a window or emits recognized text.

The injected engine is a trusted local callable accepting a fresh RGB Pillow image and
returning RapidOCR's ``boxes``, ``txts`` and ``scores`` attributes. It is intended for
synthetic tests. The default engine uses only the pinned, installed CPU models.
Run this bounded operation in a subprocess when a wall-clock deadline is required.
"""

from __future__ import annotations

import io
import math
import os
import time
import warnings
from contextlib import redirect_stderr, redirect_stdout
from importlib import metadata
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

RAPIDOCR_VERSION = "3.10.0"
ONNXRUNTIME_VERSION = "1.31.0"
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_SIDE = 8192
MAX_TEXT_BOXES = 4096
_MODEL_FILES = ("PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx")
_FORMATS = {"PNG", "JPEG", "BMP", "WEBP"}
_ERROR_STATUSES = {
    "image_path_invalid", "image_invalid", "image_too_large", "image_format_unsupported", "roi_invalid",
    "dependency_missing", "dependency_version_unsupported", "dependency_unavailable", "models_unavailable",
    "ocr_output_invalid",
}


class _Unavailable(Exception):
    """Internal fixed status code; never carry third-party exception details."""


class _Discard(io.TextIOBase):
    def write(self, value: str) -> int:
        return len(value)


def _read_image(image_path: str | os.PathLike[str], roi: tuple[int, int, int, int] | None):
    try:
        raw_path = os.fspath(image_path)
        if not isinstance(raw_path, str) or "://" in raw_path or raw_path.startswith(("\\\\", "//")):
            raise _Unavailable("image_path_invalid")
        path = Path(raw_path).resolve(strict=True)
        if str(path).startswith(("\\\\", "//")) or not path.is_file():
            raise _Unavailable("image_path_invalid")
        with path.open("rb") as stream:
            payload = stream.read(MAX_INPUT_BYTES + 1)
        if len(payload) > MAX_INPUT_BYTES:
            raise _Unavailable("image_too_large")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as source:
                if source.format not in _FORMATS or getattr(source, "n_frames", 1) != 1:
                    raise _Unavailable("image_format_unsupported")
                width, height = source.size
                if width * height > MAX_IMAGE_PIXELS or max(width, height) > MAX_IMAGE_SIDE:
                    raise _Unavailable("image_too_large")
                # ROI coordinates refer to the displayed, EXIF-oriented image.
                oriented = ImageOps.exif_transpose(source)
                width, height = oriented.size
                if roi is not None:
                    if (not isinstance(roi, (tuple, list)) or len(roi) != 4
                            or any(type(item) is not int for item in roi)):
                        raise _Unavailable("roi_invalid")
                    left, top, right, bottom = roi
                    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                        raise _Unavailable("roi_invalid")
                    oriented = oriented.crop(roi)
                image = oriented.convert("RGB")
                # Neither source metadata nor the pathname is passed to OCR.
                image.info.clear()
                return image, {
                    "input_width": width, "input_height": height,
                    "width": image.width, "height": image.height, "roi_applied": roi is not None,
                }
    except _Unavailable:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise _Unavailable("image_too_large") from None
    except (OSError, ValueError, TypeError, UnidentifiedImageError):
        raise _Unavailable("image_invalid") from None


def _load_local_engine():
    """Initialize only local model files; reject fallback dictionary downloads.

RapidOCR 3.10.0 skips model downloads for explicit model_path values. Its ONNX
recognizer also needs embedded ``character`` metadata to avoid a dictionary
download. Check that before handing it any user pixels. Do not invoke .vis().
    """
    try:
        installed = {
            "rapidocr": metadata.version("rapidocr"),
            "onnxruntime": metadata.version("onnxruntime"),
        }
    except metadata.PackageNotFoundError:
        raise _Unavailable("dependency_missing") from None
    if installed != {"rapidocr": RAPIDOCR_VERSION, "onnxruntime": ONNXRUNTIME_VERSION}:
        raise _Unavailable("dependency_version_unsupported")
    distribution = metadata.distribution("rapidocr")
    paths = [Path(distribution.locate_file(f"rapidocr/models/{name}")) for name in _MODEL_FILES]
    if any(not path.is_file() or path.stat().st_size == 0 for path in paths):
        raise _Unavailable("models_unavailable")
    try:
        import onnxruntime as ort
        from rapidocr import RapidOCR
    except (ImportError, OSError):
        raise _Unavailable("dependency_unavailable") from None

    options = ort.SessionOptions()
    options.log_severity_level = 4
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    try:
        session = ort.InferenceSession(str(paths[1]), sess_options=options, providers=["CPUExecutionProvider"])
        character = session.get_modelmeta().custom_metadata_map.get("character")
        if not isinstance(character, str) or not character.strip():
            raise _Unavailable("models_unavailable")
        del session
    except _Unavailable:
        raise
    except Exception:
        raise _Unavailable("models_unavailable") from None
    engine = RapidOCR(params={
        "Global.use_cls": False,
        "Global.log_level": "critical",
        "Global.text_score": 0.0,
        "Det.model_path": str(paths[0]),
        "Rec.model_path": str(paths[1]),
        "EngineConfig.onnxruntime.use_cuda": False,
        "EngineConfig.onnxruntime.use_dml": False,
        "EngineConfig.onnxruntime.intra_op_num_threads": 2,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    })
    return engine, {
        "name": "rapidocr", "versions": installed, "provider": "CPUExecutionProvider",
        "models": list(_MODEL_FILES), "orientation_classifier_enabled": False,
        "recognition_score_filter": 0.0, "automatic_downloads_allowed": False,
        "first_run_includes_lazy_model_initialization": True,
    }


def _validated_output(output: Any, width: int, height: int):
    try:
        boxes, texts, scores = output.boxes, output.txts, output.scores
        if boxes is None and texts is None and scores is None:
            return (), (), ()
        if boxes is None or texts is None or scores is None:
            raise ValueError
        count = len(boxes)
        if not (count == len(texts) == len(scores) and count <= MAX_TEXT_BOXES):
            raise ValueError
        safe_boxes = []
        safe_scores = []
        safe_texts = []
        for box, text_value, score in zip(boxes, texts, scores, strict=True):
            if not isinstance(text_value, str) or len(text_value) > 16_384 or len(box) != 4:
                raise ValueError
            score = float(score)
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError
            points = []
            for point in box:
                if len(point) != 2:
                    raise ValueError
                x, y = float(point[0]), float(point[1])
                if not (math.isfinite(x) and math.isfinite(y) and 0 <= x <= width and 0 <= y <= height):
                    raise ValueError
                points.append((x, y))
            safe_boxes.append(tuple(points))
            safe_scores.append(score)
            safe_texts.append(text_value)
        return tuple(safe_boxes), tuple(safe_texts), tuple(safe_scores)
    except Exception:
        raise _Unavailable("ocr_output_invalid") from None


def _statistics(boxes, scores, width: int, height: int, elapsed: float):
    confidence = {
        "minimum": round(min(scores), 4) if scores else None,
        "mean": round(sum(scores) / len(scores), 4) if scores else None,
        "maximum": round(max(scores), 4) if scores else None,
        "below_0_5": sum(score < 0.5 for score in scores),
        "from_0_5_to_0_8": sum(0.5 <= score < 0.8 for score in scores),
        "at_least_0_8": sum(score >= 0.8 for score in scores),
    }
    touching_edge = sum(
        any(x <= 1 or y <= 1 or x >= width - 1 or y >= height - 1 for x, y in box)
        for box in boxes
    )
    return {
        "text_box_count": len(boxes), "confidence": confidence,
        "elapsed_seconds": round(elapsed, 4), "boxes_touching_image_edge": touching_edge,
    }


def run_visual_probe(
    image_path: str | os.PathLike[str], *, engine=None, roi: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    """OCR the same local image twice, returning aggregate diagnostics only.

No paths, boxes, text, avatars, titles, or content fingerprints are returned.
The caller owns the image file; this function never saves or deletes it. An
injected engine must be trusted and local; this is not a sandbox for its code.
    """
    report: dict[str, Any] = {
        "schema_version": 1, "mode": "offline_image", "status": "probe_failed",
        "read_only": True, "ui_accessed": False, "messages_sent": 0,
        "text_redacted": True, "recognized_text_exported": False, "image_exported": False,
        "content_fingerprints_exported": False,
        "engine": {"name": "injected" if engine is not None else "rapidocr"},
        "runs": [],
        "structure": {
            "text_boxes_are_messages": False, "message_segmentation_verified": False,
            "reading_order_verified": False, "sender_identity_verified": False,
            "conversation_identity_verified": False, "stable_message_ids_available": False,
            "new_message_detection_verified": False, "history_completeness_verified": False,
        },
        "limitations": [
            "visible_pixels_only", "same_image_repeat_is_not_live_reliability",
            "two_repeats_do_not_measure_new_message_continuity",
            "recognition_confidence_is_not_identity_confidence",
            "recognition_confidence_is_not_measured_accuracy",
            "crop_edges_may_cut_text", "unsupported_media_and_occluded_history_not_recovered",
            "human_takeover_not_verified",
        ],
    }
    image = None
    try:
        image, report["image"] = _read_image(image_path, roi)
        # Suppress Python-side SDK diagnostics; raw exception strings are never
        # returned. The CLI also isolates/captures native-library stderr.
        with redirect_stdout(_Discard()), redirect_stderr(_Discard()):
            if engine is None:
                started = time.perf_counter()
                engine, report["engine"] = _load_local_engine()
                report["engine"]["initialization_seconds"] = round(time.perf_counter() - started, 4)
            observations = []
            for _ in range(2):
                started = time.perf_counter()
                with image.copy() as sample:
                    output = engine(sample)
                    observation = _validated_output(output, image.width, image.height)
                elapsed = time.perf_counter() - started
                observations.append(observation)
                report["runs"].append(_statistics(observation[0], observation[2], image.width, image.height, elapsed))
            first, second = observations
            count_equal = len(first[0]) == len(second[0])
            max_delta = max((
                abs(a - b)
                for box_a, box_b in zip(first[0], second[0])
                for point_a, point_b in zip(box_a, box_b)
                for a, b in zip(point_a, point_b)
            ), default=0.0) if count_equal else None
            report["consistency"] = {
                "comparison": "ordered_results_on_identical_pixels",
                "box_count_equal": count_equal, "text_sequence_equal": first[1] == second[1],
                "geometry_within_2_pixels": max_delta <= 2 if max_delta is not None else False,
                "maximum_coordinate_delta": round(max_delta, 4) if max_delta is not None else None,
                "evidence_present": bool(first[0]) and bool(second[0]),
            }
            report["status"] = "ok" if first[0] or second[0] else "no_text_detected"
    except _Unavailable as exc:
        report["status"] = str(exc) if str(exc) in _ERROR_STATUSES else "ocr_failed"
    except Exception:
        report["status"] = "ocr_failed"
    finally:
        if image is not None:
            image.close()
    return report
