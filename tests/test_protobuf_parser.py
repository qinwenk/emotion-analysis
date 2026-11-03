import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/chat-relationship-workflow/scripts/vendor/export_chat_messages.py'
spec = importlib.util.spec_from_file_location('protobuf_export', SCRIPT)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def varint(value):
    data = bytearray()
    while value >= 128:
        data.append((value & 127) | 128)
        value >>= 7
    return bytes(data + bytes([value]))


def blob(number, value):
    return varint(number << 3 | 2) + varint(len(value)) + value


def integer(number, value):
    return varint(number << 3) + varint(value)


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.chat = self.source / 'ChatPackage'
        self.chat.mkdir(parents=True)

    def test_fields_remain_neutral_and_precision_preserved(self):
        raw = (integer(1, 1) + blob(2, b'test-self') + blob(3, b'test-peer')
               + blob(4, '<b>合成消息</b>'.encode()) + integer(5, 1793664000000)
               + integer(9, 9223372036854775807))
        path = self.chat / 'sample-no-extension'
        encoded = blob(1, blob(1, raw))
        path.write_bytes(encoded)
        destination = self.root / 'export'
        count, types = exporter.export(self.source, destination)
        self.assertEqual(count, 1)
        self.assertEqual(types[1], 1)
        record = json.loads((destination / 'messages.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(record['field_2'], 'test-self')
        self.assertEqual(record['timestamp_ms'], '1793664000000')
        self.assertNotIn('sender', record)
        self.assertIn({'number': 9, 'wire': 0, 'uint64': '9223372036854775807'}, record['fields'])
        self.assertIn('&lt;b&gt;', (destination / 'messages.html').read_text(encoding='utf-8'))
        self.assertEqual(path.read_bytes(), encoded)

    def test_malformed_input_is_failure_not_empty_success(self):
        (self.chat / 'broken').write_bytes(b'\x0a\x7fshort')
        output = self.root / 'export'
        with self.assertRaises(exporter.DecodeError):
            exporter.export(self.source, output)
        self.assertFalse(output.exists())

    def test_existing_destination_not_replaced(self):
        (self.chat / 'sample').write_bytes(blob(1, blob(1, integer(1, 1) + blob(4, b'test'))))
        output = self.root / 'export'
        output.mkdir()
        (output / 'keep').write_text('keep')
        with self.assertRaises(FileExistsError):
            exporter.export(self.source, output)
        self.assertEqual((output / 'keep').read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
