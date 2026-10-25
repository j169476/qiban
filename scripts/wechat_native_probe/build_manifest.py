"""Called only after cl succeeds; inspect the locally built DLL without loading it."""
import hashlib
import json

from .protocol import DLL, build_signature, pe_exports

if __name__ == "__main__":
    data = DLL.read_bytes()
    pe_exports(data)
    DLL.with_suffix(".json").write_text(json.dumps({
        "sources": build_signature(), "sha256": hashlib.sha256(data).hexdigest()}, indent=2), encoding="utf-8")
