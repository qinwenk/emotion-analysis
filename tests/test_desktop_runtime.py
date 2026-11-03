"""Synthetic runtime-key tests; no live process, key file or chat is read."""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/chat-relationship-workflow/scripts'
sys.path.insert(0, str(SCRIPTS))
import desktop_runtime as subject

KEY = bytes(range(32))
SALT = bytes(range(16))


def page(key=KEY, salt=SALT):
    data = bytearray(b'\x91' * subject.PAGE_SIZE)
    data[:16] = salt
    mac_key = hashlib.pbkdf2_hmac('sha512', key, bytes(v ^ 0x3a for v in salt), 2, 32)
    data[-64:] = hmac.new(mac_key, bytes(data[16:-64]) + struct.pack('<I', 1), hashlib.sha512).digest()
    return bytes(data)


def encoded_blob(key=KEY, salt=SALT):
    literal = ("x'" + key.hex() + salt.hex() + "'").encode()
    return bytes(v ^ subject._XOR_MASK[n % len(subject._XOR_MASK)] for n, v in enumerate(literal))


class SyntheticReader:
    def __init__(self, budget, *, key=KEY, salt=SALT, with_name=True, name_offset=0x1000):
        self.budget = budget
        self.base = 0x10000
        self.memory = bytearray(0x6000)
        self.closed = False
        if with_name:
            self.memory[name_offset:name_offset + len(subject._CIPHER_NAME)] = subject._CIPHER_NAME
        node, config, blob = 0x2000, 0x3000, 0x4000
        struct.pack_into('<QQ', self.memory, node + 0x10, self.base + name_offset, len(subject._CIPHER_NAME))
        struct.pack_into('<Q', self.memory, node + 0x28, self.base + config)
        payload = encoded_blob(key, salt)
        struct.pack_into('<QQ', self.memory, config + 0x90, self.base + blob, len(payload))
        self.memory[blob:blob + len(payload)] = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def regions(self):
        return [(self.base, len(self.memory))]

    def read(self, address, size):
        self.budget.check(size)
        offset = address - self.base
        if offset < 0 or offset + size > len(self.memory):
            return b''
        return bytes(self.memory[offset:offset + size])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='synthetic-runtime-')
        self.base = Path(self.tmp.name)
        self.root = self.base / 'xwechat_files'
        self.db = self.make_account('synthetic-owner')
        self.exe = self.base / 'Weixin.exe'
        self.exe.write_bytes(b'SYNTHETIC-NOT-EXECUTABLE')
        self.process = {'pid': 123, 'path': str(self.exe), 'version': '4.1.15.56'}
        self.windows_patch = patch.object(subject, '_require_windows')
        self.windows_patch.start()

    def tearDown(self):
        self.windows_patch.stop()
        self.tmp.cleanup()

    def make_account(self, account):
        db = self.root / account / 'db_storage'
        (db / 'contact').mkdir(parents=True)
        (db / 'message').mkdir()
        (db / 'contact/contact.db').write_bytes(page())
        (db / 'message/message_11.db').write_bytes(page())
        return db

    def assert_code(self, code, callback):
        with self.assertRaises(subject.DesktopRuntimeError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(KEY.hex(), str(caught.exception))

    def test_account_discovery_returns_all_without_selecting(self):
        second = self.make_account('synthetic-other')
        (self.root / 'Backup').mkdir()
        self.assertEqual(set(subject.discover_accounts([self.root])), {self.db.resolve(), second.resolve()})
        self.assertEqual(subject.discover_accounts([self.db]), [self.db.resolve()])

    def test_duplicate_roots_do_not_duplicate_account(self):
        self.assertEqual(subject.discover_accounts([self.root, self.root]), [self.db.resolve()])

    def test_missing_account_root_returns_empty(self):
        self.assertEqual(subject.discover_accounts([self.base / 'missing']), [])

    def test_linked_source_is_rejected(self):
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda p: p == self.db or original(p)):
            self.assert_code('linked_path', lambda: subject.discover_accounts([self.db]))

    def test_unc_sources_are_rejected_before_filesystem_access(self):
        with patch.object(Path, 'lstat', side_effect=AssertionError('must not access remote path')):
            self.assert_code('remote_path', lambda: subject._reject_links(Path('//synthetic-server/share/db_storage')))

    def test_shards_are_enumerated_not_fixed_to_six(self):
        (self.db / 'message/message_205.db').write_bytes(page())
        headers = subject._database_headers(self.db)
        self.assertEqual(set(headers), {'contact/contact.db', 'message/message_11.db', 'message/message_205.db'})

    def test_short_or_plain_database_rejected(self):
        target = self.db / 'message/message_11.db'
        target.write_bytes(b'short')
        self.assert_code('unsupported_database', lambda: subject._database_headers(self.db))
        target.write_bytes(b'SQLite format 3\x00' + bytes(4096 - 16))
        self.assert_code('unsupported_database', lambda: subject._database_headers(self.db))

    def test_missing_message_shards_is_explicit(self):
        (self.db / 'message/message_11.db').unlink()
        self.assert_code('no_message_databases', lambda: subject._database_headers(self.db))

    def test_version_gate_rejects_unverified_version(self):
        self.assert_code('unsupported_version', lambda: subject._select_processes({**self.process, 'version': '4.1.15.57'}))

    def test_same_installation_subprocesses_can_be_tried(self):
        second = {**self.process, 'pid': 999}
        with patch.object(subject, 'discover_processes', return_value=[second, self.process]):
            self.assertEqual(subject._select_processes(None), [self.process, second])

    def test_multiple_installations_are_not_guessed(self):
        second = {**self.process, 'pid': 999, 'path': str(self.base / 'other' / 'Weixin.exe')}
        with patch.object(subject, 'discover_processes', return_value=[self.process, second]):
            self.assert_code('needs_process_selection', lambda: subject._select_processes(None))

    def test_absent_process_requires_login(self):
        with patch.object(subject, 'discover_processes', return_value=[]):
            self.assert_code('needs_login', lambda: subject._select_processes(None))

    def test_inaccessible_process_is_not_silently_selected(self):
        with patch.object(subject, 'discover_processes', return_value=[{'pid': 123, 'path': None, 'version': None}]):
            self.assert_code('process_unreadable', lambda: subject._select_processes(None))

    def test_first_page_hmac_must_match(self):
        self.assertTrue(subject._verify_page_key(KEY, page()))
        self.assertFalse(subject._verify_page_key(bytes(reversed(KEY)), page()))
        changed = bytearray(page())
        changed[99] ^= 1
        self.assertFalse(subject._verify_page_key(KEY, changed))

    def test_synthetic_runtime_object_produces_verified_keys_only(self):
        budget = subject._Budget(30)
        result = subject._scan(SyntheticReader(budget), {'message/message_11.db': page()}, budget)
        self.assertEqual(result['message/message_11.db'], {'key_hex': KEY.hex(), 'salt_hex': SALT.hex()})

    def test_wrong_runtime_candidate_never_becomes_a_key(self):
        budget = subject._Budget(30)
        result = subject._scan(SyntheticReader(budget, key=bytes(reversed(KEY))), {'message/message_11.db': page()}, budget)
        self.assertEqual(result, {})

    def test_no_configuration_marker_returns_no_key(self):
        budget = subject._Budget(30)
        self.assertEqual(subject._scan(SyntheticReader(budget, with_name=False), {'target': page()}, budget), {})

    def test_marker_across_chunk_boundary_is_found(self):
        budget = subject._Budget(30)
        with patch.object(subject, 'CHUNK_SIZE', 256):
            result = subject._scan(SyntheticReader(budget, name_offset=0x10f8), {'target': page()}, budget)
        self.assertIn('target', result)

    def test_time_and_byte_budgets_stop_lookup(self):
        budget = subject._Budget(30, maximum=15)
        self.assert_code('key_scan_limit', lambda: budget.check(16))
        with patch.object(subject.time, 'monotonic', return_value=budget.started + 31):
            self.assert_code('key_scan_timeout', lambda: budget.check())

    def test_region_selection_preserves_verified_exclusive_size_limit(self):
        self.assertTrue(subject._readable_region(0x1000, 0x04, subject.MAX_REGION_BYTES - 1))
        self.assertFalse(subject._readable_region(0x1000, 0x04, subject.MAX_REGION_BYTES))
        self.assertFalse(subject._readable_region(0x1000, 0x04, subject.MAX_REGION_BYTES + 1))
        self.assertFalse(subject._readable_region(0x1000, 0x04, 0))
        self.assertFalse(subject._readable_region(0x2000, 0x04, 4096))
        self.assertFalse(subject._readable_region(0x1000, 0x104, 4096))

    def test_partial_reads_do_not_join_across_holes(self):
        budget = subject._Budget(30)
        with patch.object(subject, 'CHUNK_SIZE', 4):
            reads = iter([b'ab', b'cdxx'])
            chunks = list(subject._chunks([(0x10000, 8)], lambda address, length: next(reads), 2, budget))
        self.assertEqual(chunks, [(0x10000, b'ab'), (0x10004, b'cdxx')])

    def test_acquire_keys_returns_in_memory_without_output_or_files(self):
        before = {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        readers = []
        def factory(process, budget):
            reader = SyntheticReader(budget)
            readers.append(reader)
            return reader
        output = io.StringIO()
        with patch.object(subject, '_WindowsMemoryReader', side_effect=factory), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = subject.acquire_keys(self.db, self.process)
        self.assertEqual(len(result['databases']), 2)
        self.assertEqual(output.getvalue(), '')
        self.assertTrue(readers[0].closed)
        after = {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_auto_subprocess_scan_uses_salt_hmac_not_pid_order(self):
        processes = [self.process, {**self.process, 'pid': 999}]
        tried = []
        def factory(process, budget):
            tried.append(process['pid'])
            return SyntheticReader(budget, with_name=process['pid'] == 999)
        with patch.object(subject, 'discover_processes', return_value=processes), patch.object(subject, '_WindowsMemoryReader', side_effect=factory):
            result = subject.acquire_keys(self.db)
        self.assertEqual(tried, [123, 999])
        self.assertEqual(len(result['databases']), 2)

    def test_partial_key_acquisition_fails_without_returning_secrets(self):
        (self.db / 'message/message_11.db').write_bytes(page(key=bytes(reversed(KEY))))
        with patch.object(subject, '_WindowsMemoryReader', side_effect=lambda process, budget: SyntheticReader(budget)):
            self.assert_code('keys_unavailable', lambda: subject.acquire_keys(self.db, self.process))

    def test_acquire_rejects_invalid_timeout(self):
        for timeout in (0, -1, 301, float('nan'), float('inf'), True, '120'):
            self.assert_code('invalid_timeout', lambda: subject.acquire_keys(self.db, self.process, timeout))


if __name__ == '__main__':
    unittest.main()
