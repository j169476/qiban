"""Bounded exception metadata, excluding messages, locals and source text."""
import re
from datetime import datetime, timezone
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
KEY = "runtime-exception-diagnostics"


def _label(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./:+-]{1,160}", value) else "unknown"


def record_exception(store, error, *, stage, generation=None, platform=None, observation_id=None):
    """Best-effort encrypted state on service stores; at most 50 recent entries."""
    try:
        frames, trace = [], error.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            try:
                source = Path(code.co_filename).resolve().relative_to(SOURCE).as_posix()
            except ValueError:
                source = None
            if source and source.startswith(("companion_", "bili_assistant/", "qiban/")):
                frames.append({"file": _label(source), "function": _label(code.co_name), "line": trace.tb_lineno})
            trace = trace.tb_next
        entry = {"created_at": datetime.now(timezone.utc).isoformat(), "type": _label(type(error).__name__),
                 "stage": _label(stage), "generation": _label(generation) if generation else None,
                 "platform": _label(platform) if platform else None, "frames": frames[-8:],
                 "observation_id": _label(observation_id) if observation_id else None}
        with store.lock, store.db() as db:
            items = store.state(KEY, [], db=db)
            store.set_state(KEY, (items + [entry])[-50:], db)
        return entry
    except Exception:
        # Diagnostics cannot turn a possibly submitted action into a retry.
        store._observation_write_failures = getattr(store, "_observation_write_failures", 0) + 1
        return None
