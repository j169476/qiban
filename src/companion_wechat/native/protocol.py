"""Pure validation and PE metadata; no native API is imported here."""
from __future__ import annotations

import hashlib
import re
import struct
from pathlib import Path

CORE_SIZE = 201552944
CORE_SHA256 = "10f8e995453e2da46d4f2b5080cd6da1f13cc5147746adc119ceae38cb039de5"
CORE_TIMESTAMP, CORE_IMAGE_SIZE = 0x6AB0A378, 0xC0AE000
PROFILES = (
    {"version": "4.1.15.13", "size": CORE_SIZE, "sha256": CORE_SHA256,
     "timestamp": CORE_TIMESTAMP, "image_size": CORE_IMAGE_SIZE,
     "account_rvas": (0xB912690, 0x8D804E8, 0x469A0, 0x8DFE2F8, 0x36F930)},
    {"version": "4.1.15.56", "size": 204305968,
     "sha256": "e0428a7b691853de496532523c2c09aa095f90941c37c9f4cfb3f137f3c66556",
     "timestamp": 0x6AD083C8, "image_size": 0xC34F000,
     "account_rvas": (0xBB92FA0, 0x8F514E8, 0x469A0, 0x8FD1468, 0x3855C0)},
)
EXPORTS = {"XlnOpen", "XlnPrepare", "XlnCommit", "XlnCancel", "XlnStatus", "XlnClose"}
HERE = Path(__file__).resolve().parent



class ProbeFailure(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise ProbeFailure(code)


def personal(value):
    return (isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,127}", value))
            and value != "filehelper" and not value.startswith("gh_"))


def fingerprint(path):
    path = Path(path)
    candidates = [p for p in PROFILES if path.stat().st_size == p["size"]]
    require(candidates, "unsupported_core")
    with path.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    matches = [p for p in candidates if digest == p["sha256"]]
    require(len(matches) == 1, "unsupported_core")
    return matches[0]


def pe_exports(data):
    """Parse only a locally built PE64's exports; never load the DLL to inspect it."""
    try:
        require(data[:2] == b"MZ", "invalid_bridge")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        require(data[pe:pe + 4] == b"PE\0\0", "invalid_bridge")
        machine, count = struct.unpack_from("<HH", data, pe + 4)
        optional_size = struct.unpack_from("<H", data, pe + 20)[0]
        require(machine == 0x8664 and 0 < count < 100 and optional_size >= 120
                and struct.unpack_from("<H", data, pe + 24)[0] == 0x20B, "invalid_bridge")
        sections = pe + 24 + optional_size

        def offset(rva, size=1):
            for i in range(count):
                _, base, raw_size, raw = struct.unpack_from("<IIII", data, sections + i * 40 + 8)
                if base <= rva and rva + size <= base + raw_size and raw + rva - base + size <= len(data):
                    return raw + rva - base
            raise ProbeFailure("invalid_bridge")

        rva, export_size = struct.unpack_from("<II", data, pe + 24 + 112)
        table = offset(rva, 40)
        _, funcs, names, addresses, pointers, ordinals = struct.unpack_from("<IIIIII", data, table + 16)
        require(names == funcs == len(EXPORTS), "unexpected_exports")
        output = {}
        for i in range(names):
            name_rva = struct.unpack_from("<I", data, offset(pointers + i * 4, 4))[0]
            start = offset(name_rva)
            end = data.index(b"\0", start, start + 64)
            name = data[start:end].decode("ascii")
            ordinal = struct.unpack_from("<H", data, offset(ordinals + i * 2, 2))[0]
            require(ordinal < funcs, "invalid_bridge")
            address = struct.unpack_from("<I", data, offset(addresses + ordinal * 4, 4))[0]
            require(not rva <= address < rva + export_size, "forwarded_export")
            offset(address)
            require(name not in output, "invalid_bridge")
            output[name] = address
        require(set(output) == EXPORTS, "unexpected_exports")
        return output
    except (struct.error, ValueError, UnicodeError, IndexError):
        raise ProbeFailure("invalid_bridge") from None


def build_signature():
    files = ["bridge.cpp", "helper_abi.inc", "core_version.h", "session.inc", "protocol.py"]
    return {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in files}
