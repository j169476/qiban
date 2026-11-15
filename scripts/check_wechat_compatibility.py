"""Offline DLL review aid; never opens a process or sends a message.

Requires pefile and capstone in the review environment. Both input DLLs must
match a registered SHA-256. Compares complete unwind-table functions (or leaf
functions up to their return/tail jump), retaining internal branch offsets.
External call/data relocations are reported, not treated as behavioral proof.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from companion_wechat.native.protocol import fingerprint  # noqa: E402


def function_signature(pe, rva):
    import capstone

    image = pe.get_memory_mapped_image()
    table = pe.OPTIONAL_HEADER.DATA_DIRECTORY[3]
    end = None
    for offset in range(table.VirtualAddress, table.VirtualAddress + table.Size, 12):
        start, finish, _ = struct.unpack_from("<III", image, offset)
        if start == rva:
            end = finish
            break
    decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    decoder.detail = True
    rows, references, consumed = [], [], 0
    for instruction in decoder.disasm(image[rva:end or rva+256], rva):
        raw = bytearray(instruction.bytes)
        if any(o.type == capstone.CS_OP_MEM and o.mem.base == capstone.x86.X86_REG_RIP
               for o in instruction.operands):
            references.append({"offset": instruction.address-rva, "kind": "data",
                               "target": hex(instruction.address+instruction.size+instruction.disp)})
            raw[instruction.disp_offset:instruction.disp_offset+instruction.disp_size] = bytes(instruction.disp_size)
        branch = instruction.group(capstone.CS_GRP_JUMP) or instruction.group(capstone.CS_GRP_CALL)
        if branch and instruction.operands and instruction.operands[0].type == capstone.CS_OP_IMM:
            target = instruction.operands[0].imm
            if end is None or not rva <= target < end:
                references.append({"offset": instruction.address-rva, "kind": instruction.mnemonic, "target": hex(target)})
                raw[instruction.imm_offset:instruction.imm_offset+instruction.imm_size] = bytes(instruction.imm_size)
        rows.append(raw.hex())
        consumed += instruction.size
        if end is None and (instruction.group(capstone.CS_GRP_RET) or instruction.mnemonic == "jmp"):
            break
    else:
        if end is None:
            raise ValueError("Leaf function exceeded review boundary")
    if not rows or end is not None and consumed != end-rva:
        raise ValueError("Incomplete disassembly")
    return rows, references


def main():
    import pefile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    reference, candidate = fingerprint(args.reference), fingerprint(args.candidate)
    old, new = pefile.PE(str(args.reference), fast_load=True), pefile.PE(str(args.candidate), fast_load=True)
    for pe, profile in ((old, reference), (new, candidate)):
        if (pe.FILE_HEADER.Machine != 0x8664 or pe.FILE_HEADER.TimeDateStamp != profile["timestamp"]
                or pe.OPTIONAL_HEADER.SizeOfImage != profile["image_size"]):
            raise ValueError("PE metadata differs from registry")
    checks = []
    for name in ("app_getter", "identity_getter", "app", "scheduler", "schedule", "text_builder", "allocate", "free"):
        before, refs_before = function_signature(old, reference["rvas"][name])
        after, refs_after = function_signature(new, candidate["rvas"][name])
        checks.append({"name": name, "instructions": len(after), "same_structure": before == after,
                       "reference_relocations": refs_before, "candidate_relocations": refs_after})
    print(json.dumps({"reference": reference["version"], "candidate": candidate["version"],
                      "checks": checks, "live_send_tested": False}, indent=2))
    raise SystemExit(0 if all(row["same_structure"] for row in checks) else 1)


if __name__ == "__main__":
    main()
