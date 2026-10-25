"""Synthetic pixels and fake OCR only; never read or save private screenshots."""

import builtins
import importlib
import json
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from companion_wechat import visual_probe


def output(texts=("PRIVATE text",), scores=(0.95,), offset=0):
    return SimpleNamespace(
        boxes=[[[2 + offset, 2], [12 + offset, 2], [12 + offset, 10], [2 + offset, 10]] for _ in texts],
        txts=texts, scores=scores,
    )


class FakeEngine:
    def __init__(self, *results, error=None):
        self.results = results or (output(),)
        self.error = error
        self.images = []

    def __call__(self, image):
        self.images.append((image.size, image.mode, image.tobytes(), dict(image.info)))
        if self.error:
            print("PRIVATE diagnostics")
            raise self.error
        return self.results[min(len(self.images) - 1, len(self.results) - 1)]


@pytest.fixture
def sample_path(tmp_path):
    path = tmp_path / "PRIVATE filename.png"
    Image.new("RGB", (30, 20), "white").save(path)
    return path


def test_report_has_aggregates_and_explicit_identity_limits_only(sample_path):
    engine = FakeEngine()
    report = visual_probe.run_visual_probe(sample_path, engine=engine)
    assert report["status"] == "ok"
    assert len(engine.images) == 2
    assert engine.images[0] == engine.images[1]
    assert report["runs"][0]["text_box_count"] == 1
    assert report["runs"][0]["confidence"]["minimum"] == 0.95
    assert report["consistency"]["text_sequence_equal"] is True
    assert report["consistency"]["evidence_present"] is True
    assert report["consistency"]["maximum_coordinate_delta"] == 0
    assert all(value is False for value in report["structure"].values())
    assert report["messages_sent"] == 0
    assert report["ui_accessed"] is False
    serialized = json.dumps(report)
    assert "PRIVATE" not in serialized
    assert str(sample_path) not in serialized
    assert "fingerprints_exported\": false" in serialized
    assert sample_path.exists()


def test_roi_is_the_only_image_data_passed_to_engine(sample_path):
    engine = FakeEngine(output(texts=(), scores=()))
    report = visual_probe.run_visual_probe(sample_path, engine=engine, roi=(5, 3, 15, 10))
    assert report["status"] == "no_text_detected"
    assert [item[:2] for item in engine.images] == [((10, 7), "RGB"), ((10, 7), "RGB")]
    assert all(item[3] == {} for item in engine.images)
    assert report["image"]["roi_applied"] is True
    assert report["consistency"]["evidence_present"] is False


@pytest.mark.parametrize("roi", [
    (-1, 0, 10, 10), (0, 0, 31, 20), (0, 0, 30, 21), (5, 5, 5, 6),
    (0, 0, 0, 0), (True, 0, 10, 10), (0.5, 0, 10, 10), (0, 0, 10), "0,0,10,10",
])
def test_invalid_roi_fails_before_engine(sample_path, roi):
    engine = FakeEngine()
    assert visual_probe.run_visual_probe(sample_path, engine=engine, roi=roi)["status"] == "roi_invalid"
    assert not engine.images


@pytest.mark.parametrize("limit,value", [("MAX_INPUT_BYTES", 8), ("MAX_IMAGE_PIXELS", 599), ("MAX_IMAGE_SIDE", 29)])
def test_large_input_rejected_before_ocr(sample_path, monkeypatch, limit, value):
    monkeypatch.setattr(visual_probe, limit, value)
    engine = FakeEngine()
    assert visual_probe.run_visual_probe(sample_path, engine=engine)["status"] == "image_too_large"
    assert not engine.images


def test_invalid_file_never_exposes_path_or_initializes_backend(tmp_path, monkeypatch):
    path = tmp_path / "PRIVATE invalid.png"
    path.write_bytes(b"not an image")
    monkeypatch.setattr(visual_probe, "_load_local_engine", lambda: pytest.fail("must not initialize OCR"))
    report = visual_probe.run_visual_probe(path)
    assert report["status"] == "image_invalid"
    assert "PRIVATE" not in json.dumps(report)


@pytest.mark.parametrize("path", ["https://example.invalid/private.png", r"\\server\share\private.png"])
def test_network_paths_rejected_without_loading(path):
    engine = FakeEngine()
    assert visual_probe.run_visual_probe(path, engine=engine)["status"] == "image_path_invalid"
    assert not engine.images


def test_animated_images_are_not_mistaken_for_one_snapshot(tmp_path):
    path = tmp_path / "animated.webp"
    first, second = Image.new("RGB", (20, 20), "white"), Image.new("RGB", (20, 20), "black")
    first.save(path, save_all=True, append_images=[second], format="WEBP")
    assert visual_probe.run_visual_probe(path, engine=FakeEngine())["status"] == "image_format_unsupported"


def test_recognition_changes_are_visible_without_text_output(sample_path):
    engine = FakeEngine(output(("PRIVATE one",), (0.4,)), output(("PRIVATE two",), (0.8,), offset=3))
    report = visual_probe.run_visual_probe(sample_path, engine=engine)
    assert report["status"] == "ok"
    assert report["runs"][0]["confidence"]["below_0_5"] == 1
    assert report["runs"][1]["confidence"]["at_least_0_8"] == 1
    assert report["consistency"]["text_sequence_equal"] is False
    assert report["consistency"]["geometry_within_2_pixels"] is False
    assert "PRIVATE" not in json.dumps(report)


def test_box_count_change_cannot_report_geometry_agreement(sample_path):
    report = visual_probe.run_visual_probe(sample_path, engine=FakeEngine(output(), output((), ())))
    assert report["consistency"]["box_count_equal"] is False
    assert report["consistency"]["maximum_coordinate_delta"] is None
    assert report["consistency"]["geometry_within_2_pixels"] is False


@pytest.mark.parametrize("bad", [
    SimpleNamespace(boxes=None, txts=("PRIVATE",), scores=(0.9,)),
    output(scores=()), output(scores=(float("nan"),)), output(scores=(float("inf"),)),
    output(scores=(1.01,)), output(scores=(-0.01,)), output(texts=(123,)),
    SimpleNamespace(boxes=[[[0, 0]]], txts=("PRIVATE",), scores=(0.9,)),
    output(offset=100), output(offset=-10), object(),
])
def test_malformed_output_is_sanitized(sample_path, bad):
    report = visual_probe.run_visual_probe(sample_path, engine=FakeEngine(bad))
    assert report["status"] == "ocr_output_invalid"
    assert "PRIVATE" not in json.dumps(report)
    assert not report["runs"]


def test_none_empty_result_is_supported(sample_path):
    result = SimpleNamespace(boxes=None, txts=None, scores=None)
    report = visual_probe.run_visual_probe(sample_path, engine=FakeEngine(result))
    assert report["status"] == "no_text_detected"
    assert report["runs"][0]["confidence"]["mean"] is None
    assert report["consistency"]["evidence_present"] is False


def test_sdk_errors_and_stdout_are_not_content_channels(sample_path, capsys):
    report = visual_probe.run_visual_probe(sample_path, engine=FakeEngine(error=RuntimeError("PRIVATE body")))
    assert report["status"] == "ocr_failed"
    assert "PRIVATE" not in json.dumps(report)
    assert capsys.readouterr().out == ""


def test_each_run_uses_fresh_pixels_even_if_engine_mutates_input(sample_path):
    values = []

    def mutating_engine(image):
        values.append(image.getpixel((0, 0)))
        image.putpixel((0, 0), (0, 0, 0))
        return output()

    assert visual_probe.run_visual_probe(sample_path, engine=mutating_engine)["status"] == "ok"
    assert values == [(255, 255, 255), (255, 255, 255)]


def test_missing_dependencies_are_fixed_diagnostics(sample_path, monkeypatch):
    def missing(name):
        raise visual_probe.metadata.PackageNotFoundError("PRIVATE")

    monkeypatch.setattr(visual_probe.metadata, "version", missing)
    report = visual_probe.run_visual_probe(sample_path)
    assert report["status"] == "dependency_missing"
    assert "PRIVATE" not in json.dumps(report)


def test_unverified_dependency_versions_are_not_used(sample_path, monkeypatch):
    monkeypatch.setattr(visual_probe.metadata, "version", lambda name: "99.0")
    assert visual_probe.run_visual_probe(sample_path)["status"] == "dependency_version_unsupported"


def test_missing_models_fail_without_importing_ocr(sample_path, tmp_path, monkeypatch):
    monkeypatch.setattr(visual_probe.metadata, "version", lambda name: {
        "rapidocr": visual_probe.RAPIDOCR_VERSION, "onnxruntime": visual_probe.ONNXRUNTIME_VERSION,
    }[name])
    monkeypatch.setattr(visual_probe.metadata, "distribution", lambda name: SimpleNamespace(
        locate_file=lambda name: tmp_path / name,
    ))
    assert visual_probe.run_visual_probe(sample_path)["status"] == "models_unavailable"


def test_crop_edge_counts_are_not_message_segmentation(sample_path):
    result = output()
    result.boxes[0][0] = [0, 0]
    report = visual_probe.run_visual_probe(sample_path, engine=FakeEngine(result))
    assert report["runs"][0]["boxes_touching_image_edge"] == 1
    assert report["structure"]["message_segmentation_verified"] is False


@pytest.fixture
def local_sdk(tmp_path, monkeypatch):
    model_dir = tmp_path / "rapidocr" / "models"
    model_dir.mkdir(parents=True)
    for filename in visual_probe._MODEL_FILES:
        (model_dir / filename).write_bytes(b"synthetic model placeholder")
    monkeypatch.setattr(visual_probe.metadata, "version", lambda name: {
        "rapidocr": visual_probe.RAPIDOCR_VERSION, "onnxruntime": visual_probe.ONNXRUNTIME_VERSION,
    }[name])
    monkeypatch.setattr(visual_probe.metadata, "distribution", lambda name: SimpleNamespace(
        locate_file=lambda name: tmp_path / name,
    ))
    state = {"character": "synthetic\ncharacters", "sessions": [], "params": None}

    def inference_session(path, *, sess_options, providers):
        state["sessions"].append((path, providers))
        return SimpleNamespace(get_modelmeta=lambda: SimpleNamespace(
            custom_metadata_map={"character": state["character"]},
        ))

    def rapid_ocr(*, params):
        state["params"] = params
        return FakeEngine()

    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        SessionOptions=SimpleNamespace, InferenceSession=inference_session,
    ))
    monkeypatch.setitem(sys.modules, "rapidocr", SimpleNamespace(RapidOCR=rapid_ocr))
    return state


def test_default_backend_passes_local_models_and_cpu_only(sample_path, local_sdk):
    report = visual_probe.run_visual_probe(sample_path)
    assert report["status"] == "ok"
    assert report["engine"]["versions"] == {"rapidocr": "3.10.0", "onnxruntime": "1.31.0"}
    assert report["engine"]["automatic_downloads_allowed"] is False
    assert report["engine"]["models"] == list(visual_probe._MODEL_FILES)
    assert local_sdk["sessions"][0][1] == ["CPUExecutionProvider"]
    assert local_sdk["params"]["Global.use_cls"] is False
    assert local_sdk["params"]["Global.text_score"] == 0.0
    for key in ("Det.model_path", "Rec.model_path"):
        assert "://" not in local_sdk["params"][key]
        assert local_sdk["params"][key].endswith(".onnx")


@pytest.mark.parametrize("character", [None, "", " ", []])
def test_no_embedded_dictionary_fails_before_rapidocr_can_download(sample_path, local_sdk, character):
    local_sdk["character"] = character
    assert visual_probe.run_visual_probe(sample_path)["status"] == "models_unavailable"
    assert local_sdk["params"] is None


def test_unrecognized_internal_exception_code_cannot_leak_text(sample_path):
    report = visual_probe.run_visual_probe(
        sample_path, engine=FakeEngine(error=visual_probe._Unavailable("PRIVATE")),
    )
    assert report["status"] == "ocr_failed"
    assert "PRIVATE" not in json.dumps(report)


def test_importing_probe_does_not_import_any_ocr_runtime(monkeypatch):
    original = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {"rapidocr", "onnxruntime", "numpy", "cv2"}:
            pytest.fail("OCR runtime must be lazy")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    importlib.reload(visual_probe)
