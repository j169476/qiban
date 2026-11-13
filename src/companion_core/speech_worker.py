"""Offline, one-request CPU speech worker; private pipes, no arbitrary input paths."""
import base64
import io
import json
import os
import subprocess
import sys
import wave
from pathlib import Path

MAX_REQUEST = 14 * 1024 * 1024


def resources():
    return (Path(sys.executable).parent / 'resources' if getattr(sys, 'frozen', False)
            else Path(os.environ['QIBAN_SPEECH_RESOURCES'])).resolve(strict=True)


def decode(content, max_ms, root):
    import av
    import numpy as np

    if content.startswith((b'#!SILK_V3', b'\x02#!SILK_V3')):
        # This child inherits the speech worker's Job. Parent cancellation kills both.
        child = subprocess.Popen([str(root/'node.exe'), str(Path(__file__).with_name('speech_decode.cjs')),
                                  str(root/'codec/index.cjs'), str(max_ms)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            raw, _ = child.communicate(content, timeout=20)
            if child.returncode == 3:
                raise ValueError('audio_duration')
            if child.returncode or not raw or len(raw) % 2 or len(raw) > max_ms * 32:
                raise ValueError('audio_decode')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        samples = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768
    elif content.startswith(b'RIFF') and content[8:12] == b'WAVE':
        # Validate declared PCM extent before decoding; then bound actual output too.
        with wave.open(io.BytesIO(content)) as source:
            if source.getframerate() <= 0 or source.getnframes()*1000 > max_ms*source.getframerate():
                raise ValueError('audio_duration')
        parts, total = [], 0
        with av.open(io.BytesIO(content)) as container:
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format='fltp', layout='mono', rate=16000)
            for frame in container.decode(stream):
                for converted in resampler.resample(frame):
                    chunk = converted.to_ndarray().reshape(-1)
                    total += len(chunk)
                    if total > max_ms*16:
                        raise ValueError('audio_duration')
                    parts.append(chunk)
            for converted in resampler.resample(None):
                chunk = converted.to_ndarray().reshape(-1)
                total += len(chunk)
                if total > max_ms*16:
                    raise ValueError('audio_duration')
                parts.append(chunk)
        samples = np.concatenate(parts) if parts else np.empty(0, dtype=np.float32)
    else:
        raise ValueError('audio_format')
    if not len(samples) or not np.isfinite(samples).all():
        raise ValueError('audio_decode')
    return samples


def transcribe(content, max_ms, root):
    import math

    from faster_whisper import WhisperModel

    samples = decode(content, max_ms, root)
    duration = math.ceil(len(samples)/16)
    engine = WhisperModel(str(root/'model'), device='cpu', compute_type='int8', cpu_threads=4,
                          num_workers=1, local_files_only=True)
    segments, _ = engine.transcribe(samples, language='zh', beam_size=3, vad_filter=True,
                                   condition_on_previous_text=False)
    units, chars = [], 0
    for segment in segments:
        text = segment.text.strip()
        chars += len(text)
        if chars > 100000 or len(units) >= 2000:
            raise ValueError('audio_transcript_limit')
        if text:
            units.append({'source': {'start_ms': min(duration, max(0, round(segment.start*1000))),
                                     'end_ms': min(duration, max(0, round(max(segment.start, segment.end)*1000)))}, 'text': text})
    return {'units': units, 'status': 'ok' if units else 'empty', 'format': 'speech',
            'scope': 'local_asr_transcript_may_contain_errors', 'truncated': False,
            'actual_audio_ms': duration, 'engine': 'faster-whisper-base', 'device': 'cpu'}


def main():
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    raw = sys.stdin.buffer.read(MAX_REQUEST+1)
    if len(raw) > MAX_REQUEST:
        raise ValueError('request_limit')
    request = json.loads(raw)
    if (set(request) not in ({'content', 'max_audio_ms'}, {'text', 'max_audio_ms'})
            or type(request['max_audio_ms']) is not int or not 1 <= request['max_audio_ms'] <= 600000):
        raise ValueError('request_shape')
    try:
        if 'text' in request:
            from companion_core.speech_synthesis import synthesize

            result = synthesize(request['text'], request['max_audio_ms'], resources())
        else:
            content = base64.b64decode(request['content'], validate=True)
            if not content or len(content) > 10*1024*1024:
                raise ValueError('request_size')
            result = transcribe(content, request['max_audio_ms'], resources())
    except ValueError as error:
        if str(error) not in {'audio_duration', 'audio_decode', 'audio_format', 'audio_transcript_limit',
                             'audio_text_invalid', 'audio_synthesis', 'audio_encode'}:
            raise
        result = {'error': str(error)}
    output = json.dumps(result, ensure_ascii=False).encode('utf8')
    if len(output) > 2*1024*1024:
        raise ValueError('response_limit')
    sys.stdout.buffer.write(output)


if __name__ == '__main__':
    main()
