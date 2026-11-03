"""Synthetic-only tests. Never reads local WeChat databases or real key files."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/chat-relationship-workflow/scripts'
sys.path.insert(0, str(SCRIPTS))
import desktop_export as subject
import desktop_db_reader

SELF = 'synthetic-owner'
TARGET = 'synthetic-target'
TABLE = 'Msg_' + hashlib.md5(TARGET.encode()).hexdigest()
KEY = bytes(range(32)).hex()
TIME = 1700000000


def connection(rows, missing_column=False):
    con = sqlite3.connect(':memory:')
    columns = 'local_id INTEGER,sort_seq INTEGER,server_id INTEGER,real_sender_id INTEGER,create_time INTEGER,local_type INTEGER'
    if not missing_column:
        columns += ',message_content BLOB,compress_content BLOB'
    con.execute(f'CREATE TABLE "{TABLE}"({columns})')
    con.execute('CREATE TABLE Name2Id(user_name TEXT)')
    con.executemany('INSERT INTO Name2Id(rowid,user_name) VALUES (?,?)', [(1, SELF), (2, TARGET), (3, 'third-person')])
    if not missing_column:
        con.executemany(f'INSERT INTO "{TABLE}" VALUES (?,?,?,?,?,?,?,?)', rows)
    con.commit()
    con.execute('PRAGMA query_only=ON')
    return con


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-desktop-export-')
        self.base = Path(self.temp.name)
        self.root = self.base / 'db_storage'
        (self.root / 'message').mkdir(parents=True)
        self.key_file = self.base / 'keys.json'
        self.output = self.base / 'export'
        self.rows = {
            'message_0.db': [
                (1, 1, 101, 1, TIME, 1, '测试文本', None),
                (2, 2, 0, 2, TIME, 1, b'same second same content', None),
                (3, 3, 0, 2, TIME, 1, b'same second same content', None),
                (4, 4, 104, 0, TIME + 1, 10000, '系统事件', None),
                (5, 5, 105, 2, TIME + 2, 9000, None, None),
            ],
            'message_1.db': [
                (7, 7, 101, 1, TIME, 1, '测试文本', None),
                (8, 8, 106, 2, TIME + 3, 49,
                 '<msg><appmsg><type>57</type><title>回复</title><refermsg><displayname>我</displayname><content>被引用的话</content></refermsg></appmsg></msg>', None),
            ],
        }
        self.source_before = {}
        for name in self.rows:
            database = self.root / 'message' / name
            database.write_bytes(b'SYNTHETIC-NOT-A-REAL-ENCRYPTED-DATABASE')
            self.source_before[name] = database.read_bytes()
        self.key_file.write_text(json.dumps({'databases': {f'message/{name}': {'key_hex': KEY} for name in self.rows}}), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def export(self, output=None, missing_column=False, **kwargs):
        with patch.object(desktop_db_reader, 'read_decrypted_db', side_effect=lambda path, key: connection(self.rows[path.name], missing_column)):
            return subject.export_chat(self.root, self.key_file, SELF, TARGET, output or self.output, **kwargs)

    def test_complete_adapter_output_and_source_protection(self):
        report = self.export()
        document = json.loads((self.output / 'chatlab.json').read_text(encoding='utf-8'))
        self.assertEqual(report['raw_records'], 7)
        self.assertEqual(report['exported_messages'], 6)
        self.assertEqual(report['identical_duplicates_removed'], 1)
        self.assertEqual(report['system_records'], 1)
        self.assertEqual(report['participant_messages'], 5)
        self.assertEqual(report['local_source_ids'], 2)
        self.assertEqual(report['unsupported_type_records_preserved'], 1)
        ids = [m['platformMessageId'] for m in document['messages']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len([m for m in document['messages'] if m['content'] == 'same second same content']), 2)
        self.assertIn('[引用 我] 被引用的话', document['messages'][-1]['content'])
        self.assertTrue(any(m['type'] == 99 and '未解析' in m['content'] for m in document['messages']))
        self.assertFalse(report['chatlab_import_performed'])
        for path in self.output.iterdir():
            self.assertNotIn(KEY, path.read_text(encoding='utf-8'))
        for name, data in self.source_before.items():
            self.assertEqual((self.root / 'message' / name).read_bytes(), data)

    def test_fallback_ids_are_repeatable(self):
        self.export()
        other = self.base / 'second-export'
        self.export(other)
        first = json.loads((self.output / 'chatlab.json').read_text(encoding='utf-8'))['messages']
        second = json.loads((other / 'chatlab.json').read_text(encoding='utf-8'))['messages']
        self.assertEqual(first, second)

    def test_in_memory_key_mapping_never_needs_a_key_file(self):
        mapping = json.loads(self.key_file.read_text())
        self.key_file.unlink()
        with patch.object(desktop_db_reader, 'read_decrypted_db', side_effect=lambda path, key: connection(self.rows[path.name])):
            report = subject.export_chat(self.root, self_id=SELF, target_id=TARGET,
                                         output=self.output, key_mapping=mapping)
        self.assertEqual(report['exported_messages'], 6)
        self.assertFalse(self.key_file.exists())
        for path in self.output.iterdir():
            self.assertNotIn(KEY, path.read_text(encoding='utf-8'))

    def test_inclusive_time_filter_preserves_coverage_and_system_record(self):
        report = self.export(since=TIME + 1, until=TIME + 2)
        document = json.loads((self.output / 'chatlab.json').read_text(encoding='utf-8'))
        self.assertEqual([m['timestamp'] for m in document['messages']], [TIME + 1, TIME + 2])
        self.assertEqual(report['system_records'], 1)
        self.assertEqual(report['raw_records'], 2)
        self.assertEqual(report['coverage']['source_records'], 7)
        self.assertEqual(report['coverage']['available_first_timestamp'], TIME)
        self.assertEqual(report['coverage']['available_last_timestamp'], TIME + 3)
        self.assertFalse(report['coverage']['full_phone_history_verified'])
        self.assertEqual(report['coverage']['selected_unique_messages'], 2)
        raw = (self.output / 'raw_messages.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('测试文本', raw)

    def test_out_of_window_bodies_and_sender_identities_are_not_processed(self):
        row = list(self.rows['message_0.db'][0]); row[3] = 3; row[6] = b'\xff'
        self.rows['message_0.db'][0] = tuple(row)
        report = self.export(since=TIME + 1)
        self.assertEqual(report['exported_messages'], 3)

    def test_empty_requested_window_exposes_coverage_without_output(self):
        with self.assertRaises(subject.ExportError) as caught:
            self.export(since=TIME + 10)
        self.assertEqual(caught.exception.code, 'empty_time_range')
        self.assertEqual(caught.exception.details['coverage']['source_records'], 7)
        self.assertEqual(caught.exception.details['coverage']['selected_records'], 0)
        self.assertFalse(self.output.exists())

    def test_empty_target_is_distinct_from_empty_window(self):
        self.rows = {name: [] for name in self.rows}
        with self.assertRaises(subject.ExportError) as caught:
            self.export()
        self.assertEqual(caught.exception.code, 'target_records_not_found')
        self.assertEqual(caught.exception.details['coverage']['source_records'], 0)
        self.assertFalse(self.output.exists())

    def test_invalid_time_filter_fails_before_reading(self):
        for bounds in ({'since': TIME + 1, 'until': TIME}, {'since': True}, {'until': 1.5}):
            with patch.object(desktop_db_reader, 'read_decrypted_db') as reader:
                with self.assertRaises(subject.ExportError):
                    subject.export_chat(self.root, self.key_file, SELF, TARGET, self.output, **bounds)
                reader.assert_not_called()

    def test_file_and_memory_keys_are_mutually_exclusive(self):
        with self.assertRaises(subject.ExportError) as caught:
            self.export(key_mapping={})
        self.assertEqual(caught.exception.code, 'invalid_key_mapping')

    def test_existing_output_is_untouched(self):
        self.output.mkdir()
        sentinel = self.output / 'keep.txt'
        sentinel.write_text('keep')
        with self.assertRaises(subject.ExportError):
            self.export()
        self.assertEqual(sentinel.read_text(), 'keep')
        self.assertEqual([p.name for p in self.output.iterdir()], ['keep.txt'])

    def test_unknown_schema_stops_without_publishing(self):
        with self.assertRaises(subject.ExportError):
            self.export(missing_column=True)
        self.assertFalse(self.output.exists())

    def test_third_party_sender_stops_without_publishing(self):
        row = list(self.rows['message_0.db'][0]); row[3] = 3
        self.rows['message_0.db'][0] = tuple(row)
        with self.assertRaises(subject.ExportError):
            self.export()
        self.assertFalse(self.output.exists())

    def test_conflicting_id_stops_without_publishing(self):
        row = list(self.rows['message_1.db'][0]); row[6] = 'conflicting message'
        self.rows['message_1.db'][0] = tuple(row)
        with self.assertRaises(subject.ExportError):
            self.export()
        self.assertFalse(self.output.exists())

    def test_timestamp_unit_error_stops(self):
        row = list(self.rows['message_0.db'][0]); row[4] = TIME * 1000
        self.rows['message_0.db'][0] = tuple(row)
        with self.assertRaises(subject.ExportError):
            self.export()
        self.assertFalse(self.output.exists())

    def test_missing_key_stops_before_reading(self):
        self.key_file.write_text('{"databases":{}}')
        with patch.object(desktop_db_reader, 'read_decrypted_db') as reader:
            with self.assertRaises(subject.ExportError):
                subject.export_chat(self.root, self.key_file, SELF, TARGET, self.output)
            reader.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_null_source_id_and_system_sender_are_preserved(self):
        row = list(self.rows['message_0.db'][3]); row[2] = None; row[3] = None
        self.rows['message_0.db'][3] = tuple(row)
        report = self.export()
        self.assertEqual(report['system_records'], 1)
        self.assertEqual(report['local_source_ids'], 3)

    def test_invalid_utf8_is_not_silently_replaced(self):
        with self.assertRaises(subject.ExportError):
            subject.decode_content(b'\xff\xfe')

    def test_compression_limits_and_decoding(self):
        import zstandard
        compressed = zstandard.ZstdCompressor().compress('压缩中文'.encode())
        self.assertEqual(subject.decode_content(compressed), '压缩中文')
        compressed = zstandard.ZstdCompressor().compress(b'a' * 1000)
        with patch.object(subject, 'MAX_CONTENT_BYTES', 64):
            with self.assertRaises(subject.ExportError):
                subject.decode_content(compressed)

    def test_truncated_or_trailing_compressed_data_stops(self):
        import zstandard
        compressed = zstandard.ZstdCompressor().compress(b'abc' * 100)
        for value in (compressed[:-2], compressed + b'trailing'):
            with self.assertRaises(subject.ExportError):
                subject.decode_content(value)

    def test_untrusted_xml_is_preserved_not_expanded(self):
        text = '<!DOCTYPE a [<!ENTITY x "expanded">]><msg><appmsg>&x;</appmsg></msg>'
        kind, content = subject.classify(49, text)
        self.assertEqual(kind, 99)
        self.assertEqual(content, text)

    def test_main_only_prints_summary(self):
        argv = ['desktop_export.py', '--db-root', str(self.root), '--key-file', str(self.key_file),
                '--self-id', SELF, '--target-id', TARGET, '--output', str(self.output)]
        text = io.StringIO()
        with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(text), patch.object(
                desktop_db_reader, 'read_decrypted_db', side_effect=lambda path, key: connection(self.rows[path.name])):
            self.assertEqual(subject.main(), 0)
        result = json.loads(text.getvalue())
        self.assertEqual(result['exported_messages'], 6)
        self.assertNotIn(KEY, text.getvalue())
        self.assertNotIn('测试文本', text.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
