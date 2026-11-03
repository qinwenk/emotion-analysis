"""Bounded, read-only Windows Weixin runtime key adapter.

Only Windows Weixin 4.1.15.56 is enabled. No injection, process writes,
debugger attachment, network access, memory dumps or key-file output.
Keys returned by acquire_keys are sensitive: keep them in the calling process.

Config.Cipher decoding/offsets adapted from TANGandXUE/wcdb-key-tool,
commit 79f1b5b92e12c66aa281b4a60a3c478b5f547dfa (MIT),
Copyright (c) 2025 CloudDreamAI / TANGandXUE.
See ../licenses/wcdb-key-tool-MIT.txt. No upstream CLI is bundled or executed.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import re
import struct
import time


SUPPORTED_VERSIONS = frozenset({'4.1.15.56'})
PAGE_SIZE = 4096
MAX_DATABASES = 512
MAX_SCAN_BYTES = 4 * 1024 ** 3
CHUNK_SIZE = 2 * 1024 ** 2
MAX_REGION_BYTES = 500 * 1024 ** 2  # Exclusive; matches the previously verified scan.
MAX_ADDRESS = 0x0000800000000000
MIN_ADDRESS = 0x10000
MAX_NEEDLES = 128
MAX_CANDIDATES = 4096
MAX_REGION_COUNT = 100000
_CIPHER_NAME = b'com.Tencent.WCDB.Config.Cipher'
_XOR_MASK = bytes.fromhex('d2c7442458020000004889442450488b450048844c2448488944254048584c24')
_LITERAL = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
_SHARD = re.compile(r'message_(\d+)\.db\Z')


def _readable_region(state: int, protection: int, size: int) -> bool:
    return state == 0x1000 and protection in {0x02, 0x04, 0x08, 0x20, 0x40, 0x80} and 0 < size < MAX_REGION_BYTES


class DesktopRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _require_windows() -> None:
    if os.name != 'nt' or struct.calcsize('P') != 8:
        raise DesktopRuntimeError('unsupported_platform', 'Runtime key lookup requires 64-bit Python on Windows.')


def _reject_links(path: Path) -> None:
    if str(path).startswith(('\\\\', '//')):
        raise DesktopRuntimeError('remote_path', 'Only local filesystem paths are supported by this runtime adapter.')
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise DesktopRuntimeError('source_unreadable', 'Cannot verify a selected local source path.') from None
        if part.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise DesktopRuntimeError('linked_path', 'Linked or redirected source paths are not supported.')


def discover_accounts(roots=None) -> list[Path]:
    """Find account db_storage directories in explicit roots, never guess one.

    This reads directory metadata only. Custom data locations can be supplied
    as roots or directly as the runner's --db-root. No recursive disk search.
    """
    _require_windows()
    if roots is None:
        profile = Path(os.environ.get('USERPROFILE') or Path.home())
        roots = [profile / 'xwechat_files', profile / 'Documents' / 'xwechat_files']
    found: dict[str, Path] = {}
    for value in roots:
        root = Path(value).absolute()
        _reject_links(root)
        if not root.is_dir():
            continue
        candidates = [root] if root.name == 'db_storage' else []
        if (root / 'db_storage').is_dir():
            candidates.append(root / 'db_storage')
        try:
            for index, entry in enumerate(root.iterdir()):
                if index >= 10000:
                    raise DesktopRuntimeError('account_scan_limit', 'The selected root contains too many entries; provide a narrower root.')
                if entry.is_dir() and entry.name not in {'Backup', 'backup'}:
                    candidates.append(entry / 'db_storage')
        except OSError:
            raise DesktopRuntimeError('account_directory_unreadable', 'Cannot read a selected account directory.') from None
        for candidate in candidates:
            _reject_links(candidate)
            if (candidate / 'contact' / 'contact.db').is_file() and (candidate / 'message').is_dir():
                _reject_links(candidate / 'contact' / 'contact.db')
                _reject_links(candidate / 'message')
                resolved = candidate.resolve()
                found[os.path.normcase(str(resolved))] = resolved
    return sorted(found.values(), key=lambda path: str(path).casefold())


def _kernel32():
    _require_windows()
    lib = ctypes.WinDLL('kernel32', use_last_error=True)
    lib.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    lib.OpenProcess.restype = wintypes.HANDLE
    lib.CloseHandle.argtypes = [wintypes.HANDLE]
    lib.CloseHandle.restype = wintypes.BOOL
    lib.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    lib.QueryFullProcessImageNameW.restype = wintypes.BOOL
    return lib


def _process_path(lib, handle) -> str:
    output = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(output))
    if not lib.QueryFullProcessImageNameW(handle, 0, output, ctypes.byref(size)):
        raise DesktopRuntimeError('process_unreadable', 'Cannot verify the running Weixin executable.')
    return output.value


def _file_version(path: str) -> str:
    """Read the numeric Windows file version, without executing the file."""
    _require_windows()
    lib = ctypes.WinDLL('version', use_last_error=True)
    lib.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    lib.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    lib.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    lib.GetFileVersionInfoW.restype = wintypes.BOOL
    lib.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    lib.VerQueryValueW.restype = wintypes.BOOL
    ignored = wintypes.DWORD()
    size = lib.GetFileVersionInfoSizeW(path, ctypes.byref(ignored))
    if not 0 < size <= 1024 * 1024:
        raise DesktopRuntimeError('version_unavailable', 'Cannot read the executable version.')
    buffer = ctypes.create_string_buffer(size)
    if not lib.GetFileVersionInfoW(path, 0, size, buffer):
        raise DesktopRuntimeError('version_unavailable', 'Cannot read the executable version.')
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not lib.VerQueryValueW(buffer, '\\', ctypes.byref(pointer), ctypes.byref(length)) or length.value < 52:
        raise DesktopRuntimeError('version_unavailable', 'The executable has no supported version resource.')
    values = struct.unpack('<13I', ctypes.string_at(pointer, 52))
    if values[0] != 0xFEEF04BD:
        raise DesktopRuntimeError('version_unavailable', 'The executable version resource is invalid.')
    return '.'.join(str(value) for value in (values[2] >> 16, values[2] & 0xffff, values[3] >> 16, values[3] & 0xffff))


def _process_ids() -> list[int]:
    lib = _kernel32()
    class Entry(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD),
                    ('pid', wintypes.DWORD), ('heap', ctypes.c_size_t),
                    ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                    ('parent', wintypes.DWORD), ('priority', wintypes.LONG),
                    ('flags', wintypes.DWORD), ('exe', wintypes.WCHAR * 260)]
    lib.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    lib.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    lib.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    lib.Process32FirstW.restype = wintypes.BOOL
    lib.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    lib.Process32NextW.restype = wintypes.BOOL
    snapshot = lib.CreateToolhelp32Snapshot(0x2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise DesktopRuntimeError('process_list_unavailable', 'Cannot enumerate running processes.')
    pids = []
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        has_entry = lib.Process32FirstW(snapshot, ctypes.byref(entry))
        while has_entry:
            if entry.exe.casefold() == 'weixin.exe':
                pids.append(int(entry.pid))
            has_entry = lib.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        lib.CloseHandle(snapshot)
    return pids


def _inspect_process(pid: int) -> dict:
    lib = _kernel32()
    handle = lib.OpenProcess(0x1000, False, pid)  # Metadata only.
    if not handle:
        return {'pid': pid, 'path': None, 'version': None, 'status': 'process_unreadable'}
    try:
        path = _process_path(lib, handle)
        if Path(path).name.casefold() != 'weixin.exe':
            raise DesktopRuntimeError('process_changed', 'The process changed during discovery.')
        _reject_links(Path(path))
        version = _file_version(path)
        return {'pid': pid, 'path': path, 'version': version,
                'status': 'supported' if version in SUPPORTED_VERSIONS else 'unsupported_version'}
    except DesktopRuntimeError as exc:
        return {'pid': pid, 'path': None, 'version': None, 'status': exc.code}
    finally:
        lib.CloseHandle(handle)


def discover_processes() -> list[dict]:
    """Return Weixin PID/path/version metadata. Does not read process memory."""
    _require_windows()
    return [_inspect_process(pid) for pid in sorted(_process_ids())]


def _select_processes(process) -> list[dict]:
    if process is None:
        candidates = discover_processes()
        if not candidates:
            raise DesktopRuntimeError('needs_login', 'Start and log in to the supported Windows Weixin client.')
    else:
        candidates = [process]
    groups = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or type(candidate.get('pid')) is not int or not 0 < candidate['pid'] <= 0xffffffff:
            raise DesktopRuntimeError('invalid_process', 'A discovered process PID, executable path and version are required.')
        path = candidate.get('path')
        if not isinstance(path, str) or not Path(path).is_absolute() or Path(path).name.casefold() != 'weixin.exe':
            raise DesktopRuntimeError('process_unreadable', 'Cannot verify one of the Weixin executable paths; select a readable process explicitly.')
        _reject_links(Path(path))
        groups.add((os.path.normcase(str(Path(path).resolve())), candidate.get('version')))
    if len(groups) != 1:
        raise DesktopRuntimeError('needs_process_selection', 'Multiple Weixin installations or versions are running; select a process explicitly.')
    if candidates[0].get('version') not in SUPPORTED_VERSIONS:
        raise DesktopRuntimeError('unsupported_version', 'Only Windows Weixin 4.1.15.56 is enabled for runtime key lookup.')
    return sorted(candidates, key=lambda item: item['pid'])


@dataclass
class _Budget:
    timeout: float
    maximum: int = MAX_SCAN_BYTES
    started: float = 0
    consumed: int = 0

    def __post_init__(self):
        self.started = time.monotonic()

    def check(self, requested=0):
        if time.monotonic() - self.started >= self.timeout:
            raise DesktopRuntimeError('key_scan_timeout', 'Runtime key lookup reached its time limit; no key material was published.')
        if requested < 0 or self.consumed + requested > self.maximum:
            raise DesktopRuntimeError('key_scan_limit', 'Runtime key lookup reached its read limit; no key material was published.')
        self.consumed += requested


class _WindowsMemoryReader:
    def __init__(self, process: dict, budget: _Budget):
        self.process, self.budget, self.handle = process, budget, None

    def __enter__(self):
        self.lib = _kernel32()
        self.handle = self.lib.OpenProcess(0x410, False, self.process['pid'])
        if not self.handle:
            raise DesktopRuntimeError('process_unreadable', 'Weixin memory is not readable with the current permissions; no elevation was attempted.')
        try:
            actual = _process_path(self.lib, self.handle)
            _reject_links(Path(actual))
            expected = Path(self.process['path']).resolve()
            if Path(actual).resolve() != expected or Path(actual).name.casefold() != 'weixin.exe':
                raise DesktopRuntimeError('process_changed', 'The selected process no longer matches the discovered executable.')
            if _file_version(actual) not in SUPPORTED_VERSIONS:
                raise DesktopRuntimeError('unsupported_version', 'The running executable version is not enabled.')
            self.lib.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
            self.lib.ReadProcessMemory.restype = wintypes.BOOL
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.handle:
            self.lib.CloseHandle(self.handle)
            self.handle = None

    def regions(self):
        class MemoryInfo(ctypes.Structure):
            _fields_ = [('base', ctypes.c_uint64), ('allocation', ctypes.c_uint64),
                        ('allocation_protect', wintypes.DWORD), ('pad1', wintypes.DWORD),
                        ('size', ctypes.c_uint64), ('state', wintypes.DWORD),
                        ('protect', wintypes.DWORD), ('kind', wintypes.DWORD), ('pad2', wintypes.DWORD)]
        self.lib.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(MemoryInfo), ctypes.c_size_t]
        self.lib.VirtualQueryEx.restype = ctypes.c_size_t
        address, count, result = 0, 0, []
        info = MemoryInfo()
        while address < MAX_ADDRESS:
            self.budget.check()
            count += 1
            if count > MAX_REGION_COUNT:
                raise DesktopRuntimeError('key_scan_limit', 'Process address-space enumeration exceeded its limit.')
            if not self.lib.VirtualQueryEx(self.handle, address, ctypes.byref(info), ctypes.sizeof(info)):
                break
            if _readable_region(info.state, info.protect, info.size):
                result.append((int(info.base), int(info.size)))
            next_address = int(info.base + info.size)
            if next_address <= address:
                break
            address = next_address
        return result

    def read(self, address, size):
        if not 0 < size <= CHUNK_SIZE or not MIN_ADDRESS <= address < MAX_ADDRESS or address + size > MAX_ADDRESS:
            return b''
        self.budget.check(size)
        buffer, received = ctypes.create_string_buffer(size), ctypes.c_size_t()
        self.lib.ReadProcessMemory(self.handle, address, buffer, size, ctypes.byref(received))
        return buffer.raw[:min(received.value, size)] if received.value else b''


def _database_headers(db_root) -> dict[str, bytes]:
    root = Path(db_root).absolute()
    _reject_links(root)
    contact, messages = root / 'contact' / 'contact.db', root / 'message'
    _reject_links(contact)
    _reject_links(messages)
    if not contact.is_file() or not messages.is_dir():
        raise DesktopRuntimeError('invalid_db_root', 'Select an account db_storage directory containing contact and message databases.')
    shards = []
    try:
        for path in messages.iterdir():
            if _SHARD.fullmatch(path.name):
                _reject_links(path)
                if not path.is_file():
                    raise DesktopRuntimeError('invalid_database', 'A message shard is not a regular file.')
                shards.append(path)
                if len(shards) >= MAX_DATABASES:
                    raise DesktopRuntimeError('database_limit', 'Too many message databases for this adapter.')
        if not shards:
            raise DesktopRuntimeError('no_message_databases', 'No supported message databases were found; sync records to this desktop account first.')
        headers = {}
        paths = [contact] + sorted(shards, key=lambda p: int(_SHARD.fullmatch(p.name).group(1)))
        for path in paths:
            with path.open('rb') as stream:
                first = stream.read(PAGE_SIZE)
            if len(first) != PAGE_SIZE or first.startswith(b'SQLite format 3\x00'):
                raise DesktopRuntimeError('unsupported_database', 'A selected database is not the supported encrypted page layout.')
            headers[path.relative_to(root).as_posix()] = first
        return headers
    except OSError:
        raise DesktopRuntimeError('database_unreadable', 'Cannot read the selected database headers.') from None


def _verify_page_key(key: bytes, page: bytes) -> bool:
    if len(key) != 32 or len(page) != PAGE_SIZE:
        return False
    mac_key = hashlib.pbkdf2_hmac('sha512', key, bytes(v ^ 0x3a for v in page[:16]), 2, 32)
    mac = hmac.new(mac_key, page[16:PAGE_SIZE - 64] + struct.pack('<I', 1), hashlib.sha512)
    return hmac.compare_digest(mac.digest(), page[-64:])


def _chunks(regions, read, overlap, budget):
    for base, size in regions:
        offset, tail = 0, b''
        while offset < size:
            budget.check()
            length = min(CHUNK_SIZE, size - offset)
            chunk = read(base + offset, length) or b''
            data_base = base + offset - len(tail)
            data = tail + chunk
            if data:
                yield data_base, data
            # A partial read leaves a gap; do not invent continuity across it.
            tail = data[-overlap:] if overlap and len(chunk) == length else b''
            offset += length


def _candidates(blob: bytes):
    if not 0 < len(blob) <= 1024:
        return
    decoded = bytes(value ^ _XOR_MASK[index % len(_XOR_MASK)] for index, value in enumerate(blob))
    seen = set()
    for match in _LITERAL.finditer(decoded):
        run = match.group(1)
        starts = [0] if len(run) <= 96 else list(dict.fromkeys([0, *range(0, len(run) - 63, 32), len(run) - 64]))
        for start in starts:
            key = bytes.fromhex(run[start:start + 64].decode('ascii'))
            salt = bytes.fromhex(run[start + 64:start + 96].decode('ascii')) if start + 96 <= len(run) else None
            item = (key, salt)
            if item not in seen:
                seen.add(item)
                yield item


def _u64(data, offset):
    return struct.unpack_from('<Q', data, offset)[0] if len(data) >= offset + 8 else 0


def _scan(reader, headers, budget) -> dict[str, dict]:
    regions = reader.regions()
    addresses = set()
    for base, data in _chunks(regions, reader.read, len(_CIPHER_NAME) - 1, budget):
        offset = data.find(_CIPHER_NAME)
        while offset >= 0:
            addresses.add(base + offset)
            if len(addresses) > MAX_NEEDLES:
                raise DesktopRuntimeError('key_scan_limit', 'Too many runtime configuration matches.')
            offset = data.find(_CIPHER_NAME, offset + 1)
    if not addresses:
        return {}
    patterns = [struct.pack('<QQ', address, len(_CIPHER_NAME)) for address in sorted(addresses)]
    remaining, result, seen_objects, seen_candidates = dict(headers), {}, set(), set()
    for base, data in _chunks(regions, reader.read, 0x80, budget):
        for pattern in patterns:
            budget.check()
            offset = data.find(pattern)
            while offset >= 0:
                budget.check()
                node = reader.read(base + offset - 0x10, 0x50) or b''
                if _u64(node, 0x10) in addresses and _u64(node, 0x18) == len(_CIPHER_NAME):
                    pointer = _u64(node, 0x28)
                    if MIN_ADDRESS <= pointer < MAX_ADDRESS and pointer not in seen_objects:
                        seen_objects.add(pointer)
                        if len(seen_objects) > MAX_CANDIDATES:
                            raise DesktopRuntimeError('key_scan_limit', 'Too many runtime configuration candidates.')
                        obj = reader.read(pointer + 0x88, 0x28) or b''
                        data_ptr, length = _u64(obj, 8), _u64(obj, 0x10)
                        if MIN_ADDRESS <= data_ptr < MAX_ADDRESS and 0 < length <= 1024:
                            blob = reader.read(data_ptr, length) or b''
                            if len(blob) == length:
                                for key, embedded in _candidates(blob):
                                    candidate = (key, embedded)
                                    if candidate in seen_candidates:
                                        continue
                                    seen_candidates.add(candidate)
                                    if len(seen_candidates) > MAX_CANDIDATES:
                                        raise DesktopRuntimeError('key_scan_limit', 'Too many runtime key candidates.')
                                    for relative, page in list(remaining.items()):
                                        budget.check()
                                        if _verify_page_key(key, page):
                                            result[relative] = {'key_hex': key.hex(), 'salt_hex': page[:16].hex()}
                                            del remaining[relative]
                                    if not remaining:
                                        return result
                offset = data.find(pattern, offset + 1)
    return result


def acquire_keys(db_root, process=None, timeout=120) -> dict:
    """Return verified key material for one selected account, in memory only.

    Multiple accounts are resolved by the caller before invoking this function.
    An unsupported version, exhausted budget or any missing database key fails
    the operation. Partial key maps are never printed, saved, or returned.
    """
    _require_windows()
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise DesktopRuntimeError('invalid_timeout', 'Runtime lookup timeout must be between 0 and 300 seconds.')
    selected = _select_processes(process)
    budget = _Budget(float(timeout))
    headers = _database_headers(db_root)
    budget.check()
    keys, readable = {}, False
    for candidate in selected:
        try:
            with _WindowsMemoryReader(candidate, budget) as reader:
                readable = True
                remaining = {path: page for path, page in headers.items() if path not in keys}
                keys.update(_scan(reader, remaining, budget))
        except DesktopRuntimeError as exc:
            # Same-installation subprocesses can have different access rights.
            # Never skip an identity/version change or an exhausted budget.
            if process is not None or exc.code != 'process_unreadable':
                raise
        if len(keys) == len(headers):
            break
    budget.check()
    if not readable:
        raise DesktopRuntimeError('process_unreadable', 'No selected Weixin process was readable with the current permissions.')
    if len(keys) != len(headers):
        raise DesktopRuntimeError('keys_unavailable', f'Verified keys for {len(keys)} of {len(headers)} selected databases. Confirm the selected account is logged in and synced.')
    return {'source': 'Windows Weixin 4.1.15.56 read-only runtime; first-page HMAC verified', 'databases': keys}
