"""Verify the QQ wheel uses only its declared core and third-party dependencies."""
import argparse
import importlib.abc
import json
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace


class BlockIntegrations(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"bili_assistant", "companion_codex", "companion_service", "companion_wechat"}:
            raise ImportError("Standalone QQ imported integration: " + fullname)


def check(qq_wheel, core_wheel):
    with tempfile.TemporaryDirectory(prefix="qiban-qq-package-") as directory:
        root = Path(directory)
        for wheel, package in ((qq_wheel, "companion_qq"), (core_wheel, "companion_core")):
            with zipfile.ZipFile(wheel) as archive:
                assert all(name.startswith(package + "/") or ".dist-info/" in name for name in archive.namelist())
                archive.extractall(root / "installed")
        sys.path.insert(0, str(root / "installed"))
        sys.meta_path.insert(0, BlockIntegrations())
        import companion_qq.lifecycle
        from companion_core import __version__
        from companion_core.process_scope import ProcessScope
        from companion_qq.module import QQModule

        assert Path(companion_qq.lifecycle.__file__).is_relative_to(root / "installed")
        assert companion_qq.lifecycle.ProcessScope is ProcessScope
        module = QQModule(SimpleNamespace(), root)
        assert module.options is None and module.bridge is None
        return {"ok": True, "version": __version__, "integration_imports_blocked": True,
                "qq_and_core_wheels_only": True, "process_ownership_available": True, "role_started": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qq_wheel", type=Path)
    parser.add_argument("core_wheel", type=Path)
    args = parser.parse_args()
    print(json.dumps(check(args.qq_wheel, args.core_wheel), indent=2))
