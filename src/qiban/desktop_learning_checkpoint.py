"""Single encrypted scratch file for interrupted, expensive learning-tree scans."""
import os
import time
from contextlib import contextmanager

from filelock import FileLock, Timeout


class Checkpoint:
    def __init__(self, store, signature, path):
        self.store, self.signature, self.path = store, signature, path
        self.count = 0
        self.active = False
        self.started = time.monotonic()

    def domain(self):
        return f"learning-layout:{self.signature}:{self.count}"

    def resume(self, rows):
        if not self.path.exists():
            return ""
        last = ""
        try:
            with self.path.open("rb+") as file:
                if file.readline(100) != self.signature.encode() + b"\n":
                    return ""
                while True:
                    pos = file.tell()
                    line = file.readline(3_000_000)
                    if not line:
                        break
                    try:
                        if not line.endswith(b"\n"):
                            raise ValueError()
                        notes = self.store.ledger_decode(self.domain(), line.strip().decode("ascii"))
                    except Exception:
                        file.truncate(pos)
                        break
                    for note in notes:
                        last = note["id"]
                        rows[last] = {**note, "type": "note"}
                    self.count += 1
                self.active = True
        except OSError:
            self.active = False
        return last

    def append(self, rows, batch, *, force=False):
        if not self.active and not force and time.monotonic() - self.started < 2:
            return
        try:
            with self.path.open("ab" if self.active else "wb") as file:
                if not self.active:
                    file.write(self.signature.encode() + b"\n")
                    batch = (n for n in rows.values() if n["type"] == "note")
                    self.count = 0
                from itertools import islice

                items = iter(batch)
                while chunk := list(islice(items, 32)):
                    value = self.store.ledger_encode(self.domain(), chunk)
                    file.write(value.encode("ascii") + b"\n")
                    self.count += 1
                file.flush()
                os.fsync(file.fileno())
            self.active = True
        except OSError:
            # Disposable acceleration must never prevent reading the source.
            self.active = False


@contextmanager
def checkpoint(store, signature):
    path = store.runtime / "learning-layout.partial"
    lock = FileLock(str(path) + ".lock")
    try:
        lock.acquire(timeout=0)
    except (Timeout, OSError):
        yield None
        return
    try:
        yield Checkpoint(store, signature, path)
    except BaseException:
        raise
    else:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    finally:
        lock.release()
