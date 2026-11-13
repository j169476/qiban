"""Owned download copies; delete only after the writing worker is proven gone.

Each native worker gets a fresh namespace. Recovery leaves live/unknown owners
alone and never scans or removes the SDK's account cache or user directories.
"""
import json
import os
import re
from pathlib import Path

from companion_core.errors import AssistantError
from companion_core.process_scope import process_exited as exited


def require_path(condition):
    if not condition:
        raise AssistantError('qq_media_path', 'QQ 附件暂存目录不符合所属进程范围。')


class NativeStaging:
    def __init__(self, data, nonce):
        require_path(isinstance(nonce, str) and re.fullmatch(r'[a-f0-9]{64}', nonce))
        self.root = Path(data).resolve(strict=True) / 'media-staging'
        self.root.mkdir(exist_ok=True)
        require_path(not self.root.is_symlink() and self.root.resolve(strict=True) == self.root)
        self.path = self.root / nonce

    def create(self, pid):
        require_path(type(pid) is int and pid > 0)
        self.path.mkdir(exist_ok=False)
        # The native process is suspended and cannot write before this marker.
        marker = self.path / 'owner.json'
        with marker.open('x', encoding='utf8') as stream:
            json.dump({'format': 1, 'worker_pid': pid}, stream)
            stream.flush()
            os.fsync(stream.fileno())

    def cleanup(self):
        """Caller has stopped and joined this particular owned worker."""
        if not self.path.exists():
            return
        self._remove(self.path)

    def recover(self, *, is_exited=exited):
        recovered = 0
        for folder in self.root.iterdir():
            if not re.fullmatch(r'[a-f0-9]{64}', folder.name) or not folder.is_dir():
                continue  # Legacy flat probe outputs are outside this ownership scheme.
            require_path(not folder.is_symlink() and folder.resolve(strict=True) == folder)
            marker = folder / 'owner.json'
            if not marker.is_file():
                continue  # Incomplete creation, no validated writer ownership.
            require_path(not marker.is_symlink() and marker.stat().st_size <= 1024)
            try:
                owner = json.loads(marker.read_text(encoding='utf8'))
            except (ValueError, UnicodeError):
                continue
            if (not isinstance(owner, dict) or set(owner) != {'format', 'worker_pid'} or owner['format'] != 1
                    or type(owner['worker_pid']) is not int or owner['worker_pid'] <= 0):
                continue
            if is_exited(owner['worker_pid']):
                self._remove(folder)
                recovered += 1
        return recovered

    def _remove(self, folder):
        require_path(folder.parent == self.root and not folder.is_symlink()
                     and folder.resolve(strict=True) == folder)
        files = list(folder.iterdir())
        # Validate the entire set before deleting anything. No recursion, paths
        # from metadata, symlinks, arbitrary subfolders or neighboring sessions.
        for file in files:
            require_path(file.name == 'owner.json' or re.fullmatch(r'[a-f0-9]{32}\.(part|bin|png|jpg|gif|webp)', file.name))
            require_path(file.is_file() and not file.is_symlink() and file.resolve(strict=True) == file)
        for file in files:
            if file.name != 'owner.json':
                file.unlink()
        (folder / 'owner.json').unlink(missing_ok=True)
        folder.rmdir()
