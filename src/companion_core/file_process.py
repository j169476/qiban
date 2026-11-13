"""Cancellable, memory/time/output-bounded local file parser process."""
import base64
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from .errors import AssistantError
from .file_worker import MAX_IMAGE_RESPONSE, MAX_REQUEST, MAX_RESPONSE
from .process_scope import ExtendedLimits, ProcessScope


def parse_file(name, content, *, guard, timeout=20):
    return _parse(name, content, guard=guard, timeout=timeout)


def parse_image(content, *, slots, guard, timeout=20):
    try:
        return _parse('image', content, guard=guard, timeout=timeout, image_slots=slots)
    except AssistantError as error:
        if error.code.startswith('file_'):
            raise AssistantError(error.code.replace('file_', 'image_', 1), '图片解码未完成，本轮未提供该图片。') from None
        raise


def parse_audio(content, *, max_audio_ms, guard, timeout=180):
    try:
        return _parse('audio', content, guard=guard, timeout=timeout, max_audio_ms=max_audio_ms)
    except AssistantError as error:
        if error.code.startswith('file_'):
            raise AssistantError(error.code.replace('file_', 'audio_', 1), '本地语音处理未完成，本轮未提供转写。') from None
        raise


def synthesize_audio(text, *, max_audio_ms, guard, timeout=180):
    try:
        return _parse('synthesis', b'', guard=guard, timeout=timeout, max_audio_ms=max_audio_ms,
                      synthesis_text=text)
    except AssistantError as error:
        if error.code.startswith('file_'):
            raise AssistantError(error.code.replace('file_', 'audio_', 1), '本地语音合成未完成，尚未发送。') from None
        raise


def _parse(name, content, *, guard, timeout, image_slots=None, max_audio_ms=None, synthesis_text=None):
    guard()
    if not isinstance(content, bytes) or len(content) > 10 * 1024 * 1024:
        raise AssistantError('file_limit', '文件大小超过解析上限。')
    request = {'name': name, 'content': base64.b64encode(content).decode('ascii')}
    if image_slots is not None:
        request['image_slots'] = image_slots
    if max_audio_ms is not None:
        request = {'content': request['content'], 'max_audio_ms': max_audio_ms}
    if synthesis_text is not None:
        request = {'text': synthesis_text, 'max_audio_ms': max_audio_ms}
    response_limit = MAX_IMAGE_RESPONSE if image_slots is not None else MAX_RESPONSE
    wire = json.dumps(request).encode('utf8')
    if len(wire) > MAX_REQUEST:
        raise AssistantError('file_limit', '文件解析请求超过上限。')
    command = ([sys.executable, '--file-reader'] if getattr(sys, 'frozen', False) else
               [sys.executable, '-m', 'companion_core.file_worker'])
    if max_audio_ms is not None:
        command = ([str(Path(sys.executable).parent.parent/'speech/Qiban.Speech.exe')]
                   if getattr(sys, 'frozen', False) else [sys.executable, '-m', 'companion_core.speech_worker'])
        if getattr(sys, 'frozen', False) and not Path(command[0]).is_file():
            raise AssistantError('audio_unavailable', '本地语音资源尚未安装。')
    scope, process, thread = ProcessScope(), None, None
    try:
        if os.name == 'nt':
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000 | 0x200
            limits.job_memory = (2048 if max_audio_ms is not None else 512) * 1024 * 1024
            if not scope.api.SetInformationJobObject(scope.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise AssistantError('file_process_limit', '无法设置文件解析资源限制。')
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) | (4 if os.name == 'nt' else 0),
            start_new_session=os.name != 'nt')
        scope.attach(process)
        scope.resume()
        completed = queue.Queue(maxsize=1)

        def exchange():
            try:
                process.stdin.write(wire)
                process.stdin.close()
                output = process.stdout.read(response_limit + 1)
                completed.put((output, None))
            except (OSError, ValueError):
                completed.put((None, 'file_process_failed'))

        thread = threading.Thread(target=exchange, daemon=True, name='qiban-file-parser')
        thread.start()
        deadline = time.monotonic() + timeout
        while True:
            guard()
            if time.monotonic() >= deadline:
                raise AssistantError('file_process_timeout', '文件解析超时，本轮未读取该文件。')
            try:
                output, error = completed.get(timeout=.05)
                break
            except queue.Empty:
                pass
        if error or len(output) > response_limit:
            raise AssistantError('file_process_failed', '文件解析进程未返回有效结果。')
        try:
            code = process.wait(timeout=min(1, max(.001, deadline-time.monotonic())))
            if code != 0:
                raise ValueError()
            result = json.loads(output)
            if max_audio_ms is not None and isinstance(result, dict) and result.get('error') in {
                    'audio_duration', 'audio_decode', 'audio_format', 'audio_transcript_limit',
                    'audio_text_invalid', 'audio_synthesis', 'audio_encode'}:
                raise AssistantError(result['error'], '语音超过时长上限或无法解析，本轮未读取。')
            if synthesis_text is not None:
                if (not isinstance(result, dict) or result.get('format') != 'SILK'
                        or not isinstance(result.get('audio'), str)):
                    raise ValueError()
            elif not isinstance(result, dict) or not isinstance(result.get('frames' if image_slots is not None else 'units'), list):
                raise ValueError()
        except (ValueError, subprocess.TimeoutExpired):
            raise AssistantError('file_process_failed', '文件解析进程未返回有效结果。') from None
        guard()
        return result
    finally:
        scope.close()
        if thread is not None:
            thread.join(timeout=2)
        if process is not None:
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()
