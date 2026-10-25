"""Isolated, bounded local speech recognition worker. Never downloads models."""

import argparse
import json
import math
from pathlib import Path


def transcribe(audio, model_path, limit, char_limit):
    from faster_whisper import WhisperModel

    model = WhisperModel(str(model_path), device="cpu", compute_type="int8", cpu_threads=2,
                         num_workers=1, local_files_only=True)
    stream, info = model.transcribe(str(audio), beam_size=3, vad_filter=True,
                                    condition_on_previous_text=False)
    rows, chars, truncated = [], 0, False
    for row in stream:
        text = row.text.strip()
        if not text or not all(math.isfinite(v) for v in (row.start, row.end)):
            continue
        if chars + len(text) > char_limit:
            truncated = True
            break
        if 0 <= row.start < row.end <= limit + 1:
            rows.append({"start": float(row.start), "end": min(float(row.end), limit), "text": text})
            chars += len(text)
    return {"segments": rows, "language": info.language, "truncated": truncated,
            "note": "本地 Whisper 自动转写，可能存在漏字、误听及说话人归属错误。"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("model", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--chars", type=int, default=60000)
    args = parser.parse_args()
    result = transcribe(args.audio, args.model, args.seconds, args.chars)
    args.output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
