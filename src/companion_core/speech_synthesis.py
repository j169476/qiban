"""Local Kokoro synthesis and bounded SILK encoding; no platform submission."""
import base64
import io
import json
import math
import os
import subprocess
import wave
from pathlib import Path


def synthesize(text, max_ms, root):
    import numpy as np
    import sherpa_onnx

    if not isinstance(text, str) or not text.strip() or len(text)>1000:
        raise ValueError('audio_text_invalid')
    if type(max_ms) is not int or not 1<=max_ms<=600000:
        raise ValueError('audio_duration')
    folder=(root/'tts').resolve(strict=True)
    # The FST loader needs fixed relative names on Windows Chinese paths.
    os.chdir(folder)
    config=sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(folder/'model.int8.onnx'),voices=str(folder/'voices.bin'),
                tokens=str(folder/'tokens.txt'),lexicon=str(folder/'lexicon-zh.txt')),
            num_threads=4,provider='cpu'),
        rule_fsts='date-zh.fst,number-zh.fst,phone-zh.fst')
    if not config.validate():
        raise ValueError('audio_synthesis')
    engine=sherpa_onnx.OfflineTts(config)
    maximum=engine.sample_rate*max_ms//1000
    observed=0
    exceeded=False
    def progress(samples, fraction):
        nonlocal observed,exceeded
        observed+=len(samples)
        exceeded=observed>maximum
        return 0 if exceeded else 1
    audio=engine.generate(text,sid=3,speed=1.0,callback=progress)
    samples=np.asarray(audio.samples,dtype=np.float32)
    if exceeded or len(samples)*1000>max_ms*audio.sample_rate:
        raise ValueError('audio_duration')
    if not len(samples) or not np.isfinite(samples).all() or float(np.max(np.abs(samples)))<.001:
        raise ValueError('audio_synthesis')
    stream=io.BytesIO()
    with wave.open(stream,'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(audio.sample_rate)
        output.writeframes((np.clip(samples,-1,1)*32767).astype('<i2').tobytes())
    child=subprocess.Popen([str(root/'node.exe'),str(Path(__file__).with_name('speech_encode.cjs')),
                            str(root/'codec/index.cjs'),str(max_ms)],stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        raw,_=child.communicate(stream.getvalue(),timeout=20)
        if child.returncode==3:
            raise ValueError('audio_duration')
        if child.returncode or len(raw)>2*1024*1024:
            raise ValueError('audio_encode')
        try:
            encoded=json.loads(raw)
            raw=base64.b64decode(encoded['audio'],validate=True)
            actual_ms=encoded['actual_audio_ms']
            if (not raw.startswith(b'\x02#!SILK_V3') or len(raw)>1024*1024
                    or type(actual_ms) is not int or not 1<=actual_ms<=max_ms):
                raise ValueError('audio_encode')
        except (KeyError,TypeError,ValueError):
            raise ValueError('audio_encode') from None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    return {'audio':base64.b64encode(raw).decode('ascii'),'format':'SILK',
            'actual_audio_ms':actual_ms, 'source_audio_ms':math.ceil(len(samples)*1000/audio.sample_rate),
            'engine':'kokoro-v1.1-zh-int8','speaker':3,'device':'cpu'}
