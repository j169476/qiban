"""MIT: local-only speech feasibility worker, synthetic inputs, no platform actions."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import time
import wave
from contextlib import ExitStack
from pathlib import Path

TEXTS = [
    "你好，我已经准备好了。今天想聊点什么？",
    "今天是二零二六年十月二十九日，下午三点半见面。请记得带上那三份文件。",
    "刚才网络断开了，现在已经恢复。发送结果还不确定，我不会重复发送这条消息。",
    "今天是2026年10月29日，下午3点30分见面，一共需要25元。",
]


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf8")


def tts(args):
    import numpy as np
    import sherpa_onnx

    folder = args.model.resolve(strict=True)
    # kaldifst uses narrow paths on Windows; fixed relative asset names avoid
    # requiring users to install the application under an ASCII-only directory.
    os.chdir(folder)
    config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(folder / "model.int8.onnx"),
                voices=str(folder / "voices.bin"),
                tokens=str(folder / "tokens.txt"),
                lexicon=str(folder / "lexicon-zh.txt"),
            ),
            num_threads=4,
            provider="cpu",
        ),
        rule_fsts="date-zh.fst,number-zh.fst,phone-zh.fst",
    )
    if not config.validate():
        raise ValueError("TTS model configuration invalid")
    began = time.perf_counter()
    engine = sherpa_onnx.OfflineTts(config)
    report = {"engine": "sherpa-onnx", "version": sherpa_onnx.__version__,
              "device": "cpu", "threads": 4, "speaker": 3,
              "load_s": time.perf_counter() - began, "samples": []}
    save(args.output / "loaded.json", {"loaded": True})
    for i, text in enumerate(TEXTS):
        began = time.perf_counter()
        audio = engine.generate(text, sid=3, speed=1.0)
        elapsed = time.perf_counter() - began
        samples = np.asarray(audio.samples, dtype=np.float32)
        if not len(samples) or not np.isfinite(samples).all():
            raise ValueError("TTS generated empty or nonfinite audio")
        duration = len(samples) / audio.sample_rate
        if duration > 120 or float(np.max(np.abs(samples))) < 0.001:
            raise ValueError("TTS generated excessive or silent audio")
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
        path = args.output / f"sample-{i + 1}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(audio.sample_rate)
            output.writeframes(pcm.tobytes())
        report["samples"].append({"file": path.name, "text": text, "seconds": duration,
                                  "generation_s": elapsed, "rtf": elapsed / duration,
                                  "sample_rate": audio.sample_rate,
                                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        save(args.output / "result.json", report)
    return report


def loaded_gpu_libraries():
    """Read actual loaded module paths after inference, not search candidates."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetModuleFileNameW.restype = wintypes.DWORD
    names = ["cublas64_12.dll", "cublasLt64_12.dll", "cudart64_12.dll", "cudnn64_9.dll",
             "cudnn_ops64_9.dll", "cudnn_cnn64_9.dll", "cudnn_graph64_9.dll",
             "cudnn_engines_precompiled64_9.dll", "cudnn_engines_runtime_compiled64_9.dll"]
    result = []
    for name in names:
        handle = kernel.GetModuleHandleW(name)
        if handle:
            buffer = ctypes.create_unicode_buffer(32768)
            if not kernel.GetModuleFileNameW(handle, buffer, len(buffer)):
                raise ctypes.WinError(ctypes.get_last_error())
            result.append({"name": name, "path": buffer.value})
    return result


def asr(args):
    from faster_whisper import WhisperModel

    began = time.perf_counter()
    engine = WhisperModel(str(args.model.resolve(strict=True)), device=args.device,
                          compute_type="int8" if args.device == "cpu" else "float16",
                          cpu_threads=4, num_workers=1, local_files_only=True)
    report = {"engine": "faster-whisper", "version": importlib.metadata.version("faster-whisper"),
              "device": args.device, "actual_device": engine.model.device,
              "actual_compute_type": engine.model.compute_type, "load_s": time.perf_counter() - began, "samples": []}
    save(args.output / "loaded.json", {"loaded": True})
    paths = sorted(args.audio.glob("sample-*.wav"))
    for index, path in enumerate(paths * args.rounds):
        with wave.open(str(path), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
        began = time.perf_counter()
        segments, info = engine.transcribe(str(path), language="zh", beam_size=3,
                                           vad_filter=True, condition_on_previous_text=False)
        rows = [{"start": s.start, "end": s.end, "text": s.text} for s in segments]
        elapsed = time.perf_counter() - began
        report["samples"].append({"file": path.name, "seconds": duration,
                                  "recognition_s": elapsed, "rtf": elapsed / duration,
                                  "round": index // len(paths) + 1,
                                  "text": "".join(s["text"] for s in rows),
                                  "segments": rows, "language": info.language})
        save(args.output / "result.json", report)
    if not report["samples"]:
        raise ValueError("No synthetic WAV samples provided")
    if args.device == "cuda":
        report["loaded_native_libraries"] = loaded_gpu_libraries()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["tts", "asr"])
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--rounds", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--dll-dir", type=Path, action="append", default=[])
    args = parser.parse_args()
    if args.mode == "tts" and args.device != "cpu":
        parser.error("This Kokoro probe only validates CPU synthesis")
    if args.mode == "asr" and args.audio is None:
        parser.error("asr requires --audio")
    args.output = args.output.resolve()
    args.model = args.model.resolve(strict=True)
    if args.audio is not None:
        args.audio = args.audio.resolve(strict=True)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    args.output.mkdir(parents=True, exist_ok=True)
    save(args.output / "started.json", {"mode": args.mode, "pid": os.getpid()})
    try:
        with ExitStack() as libraries:
            paths = [str(path.resolve(strict=True)) for path in args.dll_dir]
            for path in paths:
                libraries.enter_context(os.add_dll_directory(path))
            if paths:
                os.environ["PATH"] = os.pathsep.join([*paths, os.environ.get("PATH", "")])
            result = tts(args) if args.mode == "tts" else asr(args)
        result["completed"] = True
        save(args.output / "result.json", result)
    except Exception as error:
        save(args.output / "error.json", {"type": type(error).__name__, "message": str(error)})
        raise


if __name__ == "__main__":
    main()
