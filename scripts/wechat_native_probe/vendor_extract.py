"""Regenerate the tiny audited ABI subset, from the exact locally cloned revision."""
import subprocess
from pathlib import Path

REVISION = "a81c495770b397646ba0661118927c83589c41fc"
HERE = Path(__file__).resolve().parent


def main():
    upstream = HERE.parents[1] / ".dev/wechat/native-helper-upstream"
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=upstream, text=True).strip()
    if revision != REVISION or subprocess.check_output(["git", "status", "--porcelain"], cwd=upstream):
        raise SystemExit("Unmodified pinned helper checkout required")
    source = (upstream / "native/selftest_bridge.cpp").read_text(encoding="utf-8")
    parts = []
    for start, end in [("static int record_exception", 'extern "C" __declspec(dllexport) DWORD WINAPI WxInspectCoreVersion'),
                       ("static void release_shared", "static void write_json_string")]:
        parts.append(source[source.index(start):source.index(end)])
    (HERE / "helper_abi.inc").write_text(
        "// SPDX-License-Identifier: LicenseRef-helper-noncommercial-1.0\n"
        f"// Audited subset of Prometheus-first/helper {REVISION}; see LICENSE.helper.\n" + "\n".join(parts),
        encoding="utf-8")
    for origin, target in [("native/core_version.h", "core_version.h"), ("LICENSE", "LICENSE.helper"),
                           ("THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.helper.md")]:
        (HERE / target).write_bytes((upstream / origin).read_bytes())


if __name__ == "__main__":
    main()
