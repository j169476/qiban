"""The PostgreSQL bundle collects the Visual C++ runtime it imports, and refuses to guess.

CLEAN-175 showed that a clean Windows image need not carry a complete MSVC runtime for
``initdb.exe``. ``build_postgres.collect_vc_runtime`` therefore copies exactly the redistributable
modules the shipped binaries import into ``bin``, keeps the license originals beside them, and
records version, source and SHA-256 for the runtime-manifest. These tests build minimal PE images so
that the collection, the refusals and the manifest inventory can be checked without the PostgreSQL
distribution or a Visual Studio installation.
"""
import hashlib
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'desktop_probe'))

from build_postgres import VC_LICENSE_DIR, collect_vc_runtime, file_inventory  # noqa: E402

VCRUNTIME = 'vcruntime140.dll'
VCRUNTIME_ONE = 'vcruntime140_1.dll'
MSVCP = 'msvcp140.dll'
UNUSED_MODULE = 'msvcp140_2.dll'
# Operating-system imports: an API set and a system DLL that no bundle may ever collect.
KERNEL32 = 'kernel32.dll'
API_SET = 'api-ms-win-crt-runtime-l1-1-0.dll'
# Symbols the synthetic PostgreSQL binaries import; the synthetic redistributable must export them.
TERMINATE = '__std_terminate'
THROW = '_CxxThrowException'
FRAME_INFO = '_CreateFrameInfo'


def _pe(imports=None, exports=()):
    """A minimal PE32+ image with the given import table and, when asked, an export name table.

    It carries no version resource, so ``file_version`` reports None for it - which is exactly what a
    record of an unversioned file should say.
    """
    imports, exports = imports or {}, tuple(exports)
    image = bytearray(0x200 + 0x1000)
    image[0:2] = b'MZ'
    struct.pack_into('<I', image, 0x3c, 0x40)
    image[0x40:0x44] = b'PE\0\0'
    struct.pack_into('<H', image, 0x44, 0x8664)
    struct.pack_into('<H', image, 0x46, 1)
    struct.pack_into('<H', image, 0x54, 240)
    struct.pack_into('<H', image, 0x58, 0x20b)
    struct.pack_into('<I', image, 0xc4, 16)
    struct.pack_into('<II', image, 0xc8, 0x1c00 if exports else 0, 40 if exports else 0)
    struct.pack_into('<II', image, 0xd0, 0x1000 if imports else 0, 40 if imports else 0)
    struct.pack_into('<8sIIII', image, 0x148, b'.rdata', 0x1000, 0x1000, 0x1000, 0x200)

    def put(offset, data):
        assert offset + len(data) <= 0x1000, 'test PE image overflow'
        image[0x200 + offset:0x200 + offset + len(data)] = data

    names = 0x400
    for index, (module, symbols) in enumerate(imports.items()):
        thunks, hint = 0x100 + index * 0x80, names
        for position, symbol in enumerate(symbols):
            struct.pack_into('<Q', image, 0x200 + thunks + position * 8, 0x1000 + hint)
            put(hint, b'\0\0' + symbol.encode('latin1') + b'\0')
            hint += 2 + len(symbol) + 1
        put(hint, b'\0\0')
        names = hint + 2
        struct.pack_into('<IIIII', image, 0x200 + index * 20,
                         0x1000 + thunks, 0, 0, 0x1000 + 0x800 + index * 0x40, 0x1000 + thunks)
        put(0x800 + index * 0x40, module.encode('latin1') + b'\0')
    if exports:
        offsets = []
        for index, symbol in enumerate(exports):
            offsets.append(0xd00 + index * 0x40)
            put(0xd00 + index * 0x40, symbol.encode('latin1') + b'\0')
            struct.pack_into('<I', image, 0x200 + 0xc80 + index * 4, 0x1000 + offsets[-1])
        struct.pack_into('<I', image, 0x200 + 0xc00 + 24, len(exports))
        struct.pack_into('<I', image, 0x200 + 0xc00 + 32, 0x1000 + 0xc80)
    return bytes(image)


def _bundle(root):
    """A copied PostgreSQL resource directory whose binaries import the MSVC runtime and Windows."""
    bundle = root / 'bundle'
    (bundle / 'bin').mkdir(parents=True)
    (bundle / 'lib').mkdir()
    system = {KERNEL32: ('GetLastError',), API_SET: ('_initialize_narrow_environment',)}
    (bundle / 'bin' / 'initdb.exe').write_bytes(_pe({VCRUNTIME: (TERMINATE,), **system}))
    (bundle / 'bin' / 'postgres.exe').write_bytes(_pe({VCRUNTIME: (TERMINATE, THROW), **system}))
    (bundle / 'lib' / 'pgcrypto.dll').write_bytes(_pe({VCRUNTIME: (TERMINATE,)}))
    (bundle / 'bin' / 'icuuc77.dll').write_bytes(_pe(
        {MSVCP: ('_Xlength_error',), VCRUNTIME_ONE: (FRAME_INFO,), VCRUNTIME: (TERMINATE,)}))
    return bundle


def _redist(root):
    """A synthetic redistributable folder: the three imported modules plus one nobody imports."""
    runtime = root / 'redist'
    runtime.mkdir()
    (runtime / VCRUNTIME).write_bytes(_pe({}, (TERMINATE, THROW)))
    (runtime / VCRUNTIME_ONE).write_bytes(_pe({VCRUNTIME: (TERMINATE,)}, (FRAME_INFO,)))
    (runtime / MSVCP).write_bytes(_pe({VCRUNTIME: (TERMINATE,), VCRUNTIME_ONE: (FRAME_INFO,)},
                                     ('_Xlength_error',)))
    (runtime / UNUSED_MODULE).write_bytes(_pe({}, ('_Unused',)))
    # The originals of the redistribution terms, kept with their own names and extensions.
    licenses = [root / 'Visual_Studio_2026-License-Community_ENU.docx',
                root / 'Visual-Studio-2026-Redistribution.html']
    for index, license_file in enumerate(licenses):
        license_file.write_bytes(f'license original {index}\n'.encode('utf8'))
    return runtime, licenses


def _shipped_dlls(bundle):
    return sorted(path.name for path in (bundle / 'bin').glob('*.dll'))


def test_collects_the_imported_modules_and_keeps_the_manifest_inventory_exact(tmp_path):
    bundle, (runtime, licenses) = _bundle(tmp_path), _redist(tmp_path)
    record = collect_vc_runtime(bundle, runtime, licenses)
    rows = {row['module']: row for row in record['modules']}
    inventory = {row['path']: row['sha256'] for row in file_inventory(bundle)}
    # Everything the binaries import from the redistributable family and nothing more: the system
    # modules stay with Windows, and the unimported msvcp140_2.dll stays in the redist folder.
    assert sorted(rows) == [MSVCP, VCRUNTIME, VCRUNTIME_ONE]
    assert _shipped_dlls(bundle) == sorted([MSVCP, VCRUNTIME, VCRUNTIME_ONE, 'icuuc77.dll'])
    assert rows[VCRUNTIME]['imported_by'] == [
        'bin/icuuc77.dll', 'bin/initdb.exe', 'bin/msvcp140.dll', 'bin/postgres.exe',
        'bin/vcruntime140_1.dll', 'lib/pgcrypto.dll']
    # The record and the runtime-manifest files list describe the same bytes on disk.
    for row in record['modules'] + record['licenses']:
        assert inventory[row['path']] == row['sha256']
        assert hashlib.sha256((bundle / row['path']).read_bytes()).hexdigest() == row['sha256']
    # License originals keep their downloaded name, extension and bytes; no summary is written.
    assert [(row['name'], row['path']) for row in record['licenses']] == [
        (path.name, f'{VC_LICENSE_DIR}/{path.name}') for path in licenses]
    for path in licenses:
        kept = bundle / VC_LICENSE_DIR / path.name
        assert kept.suffix == path.suffix and kept.read_bytes() == path.read_bytes()
        assert kept.stat().st_size == path.stat().st_size
        assert f'{VC_LICENSE_DIR}/{path.name}' in inventory


def test_refuses_a_runtime_folder_that_lacks_a_required_module(tmp_path):
    bundle, (runtime, licenses) = _bundle(tmp_path), _redist(tmp_path)
    (runtime / VCRUNTIME_ONE).unlink()
    with pytest.raises(ValueError):
        collect_vc_runtime(bundle, runtime, licenses)
    assert _shipped_dlls(bundle) == ['icuuc77.dll']
    assert not (bundle / VC_LICENSE_DIR).exists()


def test_refuses_a_missing_license_before_copied_dlls(tmp_path):
    bundle, (runtime, _licenses) = _bundle(tmp_path), _redist(tmp_path)
    empty = tmp_path / 'empty.txt'
    empty.write_bytes(b'')
    for rejected in (None, [], [empty]):
        with pytest.raises(ValueError):
            collect_vc_runtime(bundle, runtime, rejected)
        # The license is checked first, so a rejected run leaves no DLL and no license behind.
        assert _shipped_dlls(bundle) == ['icuuc77.dll']
        assert not (bundle / VC_LICENSE_DIR).exists()


def test_reports_the_symbols_an_older_runtime_is_missing(tmp_path):
    bundle, (runtime, licenses) = _bundle(tmp_path), _redist(tmp_path)
    (runtime / VCRUNTIME).write_bytes(_pe({}, (TERMINATE,)))
    with pytest.raises(ValueError) as raised:
        collect_vc_runtime(bundle, runtime, licenses)
    # The refused symbol and the file that imports it are named, not silently skipped.
    assert THROW in str(raised.value)
    assert 'bin/postgres.exe' in str(raised.value)
    assert _shipped_dlls(bundle) == ['icuuc77.dll']
    assert not (bundle / VC_LICENSE_DIR).exists()
