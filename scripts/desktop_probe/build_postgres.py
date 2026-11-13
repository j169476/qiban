"""Copy the fixed PostgreSQL runtime, add the app-local MSVC runtime, and per-process UTF-8 manifests.

PostgreSQL's Windows build links against the Microsoft Visual C++ runtime. A clean Windows
installation does not always carry a complete copy of it: the CLEAN-175 acceptance run on a pristine
Windows 11 Enterprise image stopped because ``initdb.exe`` could not find
``VCRUNTIME140_1.dll`` (``resources/speech`` and ``resources/qq`` ship their own copies for their own
processes, and the copy in ``System32`` is neither complete nor ours to rely on). The modules the
shipped PostgreSQL binaries actually import are therefore collected next to ``bin/postgres.exe`` and
registered in the same ``runtime-manifest.json`` that already lists every other file.

Nothing is taken from ``System32`` and no machine-wide redistributable is installed; the collector
only accepts an explicit folder of a Microsoft redistributable (``--vc-runtime``) plus the license
originals that must be kept with it (``--vc-license``, repeated once per file). The license originals
are copied byte for byte under their own names - the redistribution terms live in the Visual Studio
license document and the redistribution page, not in the two-line ``Redist.txt`` that only points at
them - and both the modules and the licenses are recorded with version, source and SHA-256, so the
bundle can be traced back to the exact redistributable it came from. When the upstream distribution
already ships what it imports, nothing is copied and no input is required.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import shutil
import struct
import subprocess
from pathlib import Path

MANIFEST = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
<assemblyIdentity type="win32" name="Qiban.PostgreSQL" version="18.6.0.0"/>
<application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
<activeCodePage xmlns="http://schemas.microsoft.com/SMI/2019/WindowsSettings">UTF-8</activeCodePage>
</windowsSettings></application></assembly>
'''

# The Microsoft Visual C++ redistributable modules a PostgreSQL build can depend on. The Universal C
# runtime (ucrtbase.dll and the api-ms-win-crt-*.dll API sets) is an operating-system component of
# Windows 10 1903 and later and is deliberately not collected here.
VC_RUNTIME_FAMILY = frozenset({
    'concrt140.dll', 'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll', 'msvcp140_atomic_wait.dll',
    'msvcp140_codecvt_ids.dll', 'vccorlib140.dll', 'vcomp140.dll', 'vcruntime140.dll',
    'vcruntime140_1.dll', 'vcruntime140_threads.dll'})
# The license originals keep the name and extension they were downloaded with, so nothing in the
# bundle has to be reconstructed from a summary.
VC_LICENSE_DIR = 'licenses/microsoft-visual-c++-redistributable'
PE_SUFFIXES = frozenset({'.exe', '.dll'})
# A collected runtime may need another module of the same redistributable; the closure is small.
MAX_RUNTIME_ROUNDS = 16


def _image(data, source):
    """Return ``(directories, translate)`` for one PE image; ``translate`` maps an RVA to a file offset."""
    if data[:2] != b'MZ':
        raise ValueError(f'Not a PE image: {source}')
    header = struct.unpack_from('<I', data, 0x3c)[0]
    if data[header:header+4] != b'PE\0\0':
        raise ValueError(f'Not a PE image: {source}')
    count, = struct.unpack_from('<H', data, header+6)
    optional_size, = struct.unpack_from('<H', data, header+20)
    optional = header+24
    magic, = struct.unpack_from('<H', data, optional)
    sections, base = [], optional+optional_size
    for index in range(count):
        head = base+index*40
        virtual_size, address, raw_size, raw = struct.unpack_from('<IIII', data, head+8)
        sections.append((address, max(virtual_size, raw_size), raw, raw_size))

    def translate(rva):
        for address, span, raw, raw_size in sections:
            if address <= rva < address+span:
                return raw+(rva-address) if rva-address < raw_size else None
        return None

    return optional+(112 if magic == 0x20b else 96), translate, magic


def _string(data, translate, rva):
    offset = translate(rva)
    end = data.find(b'\0', offset) if offset is not None else -1
    if end < 0:
        raise ValueError('Malformed PE image table')
    return data[offset:end].decode('latin1')


def _thunks(data, translate, rva, width):
    """The symbol names one import thunk array names, skipping ordinal imports."""
    names, offset = set(), translate(rva)
    if offset is None:
        return names
    while True:
        value, = struct.unpack_from('<Q' if width == 8 else '<I', data, offset)
        if not value:
            return names
        if not value >> (width*8-1):
            names.add(_string(data, translate, value+2))
        offset += width


def imported_modules(data, source):
    """``{module name: {imported symbol names}}`` of one PE image, names lowercased.

    Every entry of the import directory is returned, including the operating-system modules and the
    ``api-ms-win-*`` API sets; symbol names are only read for the redistributable family, because that
    is the only family this collector ever copies. The caller filters by ``VC_RUNTIME_FAMILY``.
    """
    directories, translate, magic = _image(data, source)
    width = 8 if magic == 0x20b else 4
    rva, = struct.unpack_from('<I', data, directories+8)
    modules = {}
    offset = translate(rva) if rva else None
    while offset is not None:
        original, _, _, name, first = struct.unpack_from('<IIIII', data, offset)
        if not any((original, name, first)):
            break
        module = _string(data, translate, name).lower()
        modules[module] = _thunks(data, translate, original or first, width) \
            if module in VC_RUNTIME_FAMILY else set()
        offset += 20
    return modules


def exported_symbols(data, source):
    """The symbol names one PE image exports."""
    directories, translate, _ = _image(data, source)
    rva, = struct.unpack_from('<I', data, directories)
    base = translate(rva) if rva else None
    if base is None:
        return set()
    names_rva, = struct.unpack_from('<I', data, base+32)
    count, = struct.unpack_from('<I', data, base+24)
    table = translate(names_rva) if count and names_rva else None
    if table is None:
        return set()
    return {_string(data, translate, struct.unpack_from('<I', data, table+index*4)[0])
            for index in range(count)}


def file_version(path):
    """The fixed file version of one Windows PE file, or None when it has no readable version resource."""
    version = ctypes.WinDLL('version', use_last_error=True)
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    info = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, info):
        return None
    pointer, length = ctypes.c_void_p(), ctypes.c_uint()
    if not version.VerQueryValueW(info, '\\', ctypes.byref(pointer), ctypes.byref(length)) or not pointer:
        return None
    fixed = struct.unpack_from('<13I', ctypes.string_at(pointer, 52), 0)
    if fixed[0] != 0xFEEF04BD:
        return None
    return f'{fixed[2] >> 16}.{fixed[2] & 0xFFFF}.{fixed[3] >> 16}.{fixed[3] & 0xFFFF}'


def _pe_files(directory):
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return [path for path in sorted(directory.rglob('*'))
            if path.is_file() and path.suffix.lower() in PE_SUFFIXES]


def _runtime_file(directory, module):
    """The redistributable file named ``module`` in one explicit runtime folder."""
    for path in sorted(Path(directory).iterdir()):
        if path.name.lower() == module:
            if path.is_symlink() or not path.is_file():
                raise ValueError(f'--vc-runtime entry is not a plain file: {path}')
            return path
    return None


def _licenses(license_files):
    """The validated ``--vc-license`` originals as ``[(path, bytes)]``; raises when none is usable.

    Checked before anything is copied, so a rejected call leaves the bundle untouched.
    """
    retained, seen = [], set()
    for license_file in license_files or ():
        path = Path(license_file).resolve(strict=True)
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f'--vc-license must be one readable, non-empty file: {path}')
        if path.name.casefold() in seen:
            raise ValueError(f'--vc-license received {path.name} more than once')
        seen.add(path.name.casefold())
        retained.append((path, path.read_bytes()))
    if not retained:
        raise ValueError(
            'The collected Visual C++ runtime needs its license kept in the bundle: pass --vc-license '
            'once for each original of the redistribution terms (the Visual Studio license document '
            'and the redistribution page), not a hand-written summary and not the two-line Redist.txt '
            'that only links to them.')
    return retained


def _importing_files(importers, module):
    """The bundle-relative files that import ``module``, in sorted order."""
    return sorted({importer for needed in importers.get(module, {}).values()
                   for importer in needed})


def collect_vc_runtime(bundle, runtime=None, license_files=None):
    """Copy the Visual C++ runtime modules ``bundle`` imports into ``bundle/bin`` and record them.

    ``bundle`` is the already copied PostgreSQL resource directory. ``runtime`` must be one folder of
    a Microsoft redistributable (for example ``...\\VC\\Redist\\MSVC\\14.50.35710\\x64\\
    Microsoft.VC145.CRT``); ``license_files`` are the originals of the license that stays with it. The
    return value is the manifest record, or None when the bundle already ships every module it needs.
    """
    bundle = Path(bundle)
    installed = {path.name.lower(): path for path in (bundle/'bin').iterdir() if path.is_file()} \
        if (bundle/'bin').is_dir() else {}
    # Only the redistributable family is a candidate: kernel32.dll and the api-ms-win-* API sets are
    # operating-system components, never something this bundle could or should carry.
    symbols, importers, required = {}, {}, {}
    for directory in ('bin', 'lib'):
        for path in _pe_files(bundle/directory):
            relative = path.relative_to(bundle).as_posix()
            for module, names in imported_modules(path.read_bytes(), path).items():
                if module not in VC_RUNTIME_FAMILY or module in installed:
                    continue
                required.setdefault(module, None)
                symbols.setdefault(module, set()).update(names)
                needed_by = importers.setdefault(module, {})
                for name in names:
                    needed_by.setdefault(name, set()).add(relative)
    if not required:
        return None
    if runtime is None:
        raise ValueError(
            'The shipped PostgreSQL binaries import the Microsoft Visual C++ runtime '
            f'({", ".join(sorted(required))}) which this bundle does not contain. Pass --vc-runtime '
            'pointing at one Microsoft redistributable folder such as '
            r'"%ProgramFiles%\Microsoft Visual Studio\2022\Community\VC\Redist\MSVC\<version>\x64'
            r'\Microsoft.VC143.CRT". Copying System32 or installing a machine-wide redistributable is '
            'not an accepted substitute.')
    runtime = Path(runtime).resolve(strict=True)
    licenses = _licenses(license_files)
    # Only what the binaries import, plus what those modules import in turn - never a whole folder.
    sources, pending, rounds = {}, sorted(required), 0
    while pending:
        module, pending = pending[0], pending[1:]
        if module in sources:
            continue
        rounds += 1
        if rounds > MAX_RUNTIME_ROUNDS:
            raise ValueError('The Visual C++ runtime dependency chain did not settle')
        source = _runtime_file(runtime, module)
        if source is None:
            raise ValueError(f'--vc-runtime does not contain {module}, which is required by '
                             f'{", ".join(_importing_files(importers, module))}; the file must come '
                             'from the same Microsoft redistributable folder as the other modules.')
        data = source.read_bytes()
        sources[module] = (source, data)
        for dependency, names in imported_modules(data, source).items():
            if dependency not in VC_RUNTIME_FAMILY or dependency in installed:
                continue
            symbols.setdefault(dependency, set()).update(names)
            needed_by = importers.setdefault(dependency, {})
            for name in names:
                needed_by.setdefault(name, set()).add(f'bin/{source.name}')
            if dependency not in sources:
                pending.append(dependency)
    verified, absent = [], {}
    for module in sorted(sources):
        source, data = sources[module]
        # Compare the names actually imported with the names actually exported, so an older
        # redistributable is reported by the exact symbols it is missing instead of being copied.
        missing = symbols.get(module, set()) - exported_symbols(data, source)
        if missing:
            absent[module] = (source, missing)
            continue
        target = bundle/'bin'/source.name
        if target.exists():
            raise ValueError(f'bin/{source.name} already exists; the runtime is collected only when '
                             'the bundle lacks it')
        verified.append((module, source, data, target))
    if absent:
        detail = '; '.join(
            f'{source.name} ({file_version(source)}) lacks ' + ', '.join(
                f'{name} (imported by {", ".join(sorted(importers[module][name]))})'
                for name in sorted(missing))
            for module, (source, missing) in sorted(absent.items()))
        raise ValueError('--vc-runtime is not compatible with PostgreSQL: ' + detail +
                         '. Use the redistributable of the same or a newer Visual C++ toolset '
                         'instead of an older copy from another package.')
    modules = []
    for module, source, data, target in verified:
        shutil.copyfile(source, target)
        modules.append({'module': module, 'path': f'bin/{source.name}', 'version': file_version(source),
                        'source': source.as_posix(), 'sha256': hashlib.sha256(data).hexdigest(),
                        'imported_by': _importing_files(importers, module)})
    records = []
    for path, data in licenses:
        target = bundle/VC_LICENSE_DIR/path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        records.append({'path': target.relative_to(bundle).as_posix(), 'name': path.name,
                        'source': path.as_posix(), 'sha256': hashlib.sha256(data).hexdigest()})
    return {'name': 'microsoft-visual-c++-redistributable',
            'runtime': runtime.as_posix(),
            'modules': modules,
            'licenses': records}


def file_inventory(root):
    """``[{'path', 'sha256'}]`` of every file below ``root``, sorted by path.

    This is the ``files`` list of the runtime-manifest, so whatever the collector copied - runtime
    modules and license originals alike - is covered by the same digest check.
    """
    root = Path(root)
    return [{'path': path.relative_to(root).as_posix(),
             'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(root.rglob('*')) if path.is_file()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--vc-runtime', type=Path,
                        help=r'one Microsoft redistributable folder such as '
                             r'...\VC\Redist\MSVC\<version>\x64\Microsoft.VC14x.CRT; only needed when '
                             'the shipped binaries import the runtime and the bundle lacks it')
    parser.add_argument('--vc-license', type=Path, action='append', metavar='FILE',
                        help='one original of the Microsoft redistribution terms, repeated once per '
                             'file; name and bytes are kept as they were downloaded, and the '
                             'two-line Redist.txt that only links to the terms is not accepted')
    args = parser.parse_args()
    source, output = args.source.resolve(strict=True), args.output.resolve()
    version = subprocess.check_output([str(source/'bin/postgres.exe'), '--version'], text=True).strip()
    if version != 'postgres (PostgreSQL) 18.6':
        raise ValueError('This package definition is for PostgreSQL 18.6')
    output.mkdir(parents=True, exist_ok=False)
    originals = []
    for folder in ['bin', 'lib', 'share']:
        for file in (source/folder).rglob('*'):
            if file.is_file():
                if file.is_symlink() or file.suffix == '.manifest':
                    raise ValueError('Unexpected source link or pre-existing manifest')
                target = output/file.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, target)
                originals.append({'path': file.relative_to(source).as_posix(),
                                  'sha256': hashlib.sha256(file.read_bytes()).hexdigest()})
    for name in ['server_license.txt', 'commandlinetools_3rd_party_licenses.txt']:
        shutil.copyfile(source/name, output/name)
    # After the copy, so the check sees exactly the binaries that will be shipped.
    vc_runtime = collect_vc_runtime(output, args.vc_runtime, args.vc_license)
    for binary in (output/'bin').glob('*.exe'):
        binary.with_suffix('.exe.manifest').write_text(MANIFEST, encoding='utf8')
    (output/'upstream-files.json').write_text(json.dumps({'version': version, 'files': originals}, indent=2), encoding='utf8')
    files = file_inventory(output)
    manifest = {'format': 1, 'entrypoint': 'bin/postgres.exe', 'version': version, 'files': files}
    if vc_runtime is not None:
        manifest['vc-runtime'] = vc_runtime
    (output/'runtime-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf8')
    print(json.dumps({'files': len(files),
                      'bytes': sum(p.stat().st_size for p in output.rglob('*') if p.is_file()),
                      'vc-runtime': [row['module'] for row in (vc_runtime or {}).get('modules', ())],
                      'vc-license': [row['path'] for row in (vc_runtime or {}).get('licenses', ())]}))


if __name__ == '__main__':
    main()
