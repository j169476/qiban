"""Record the tested Windows dependency set, excluding this editable checkout."""

import importlib.metadata
from pathlib import Path

project = Path(__file__).resolve().parents[1]
packages = sorted(
    (dist.metadata["Name"], dist.version)
    for dist in importlib.metadata.distributions()
    if dist.metadata["Name"].lower().replace("_", "-")
    not in ("qiban", "codex-bilibili-assistant", "pip", "setuptools", "wheel")
)
text = "# Tested on Windows, Python 3.11.9. Install with pip install -c constraints-windows.txt -e .[dev]\n"
text += "\n".join(f"{name}=={version}" for name, version in packages) + "\n"
(project / "constraints-windows.txt").write_text(text, encoding="utf-8")
