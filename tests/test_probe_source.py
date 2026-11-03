import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/chat-relationship-workflow/scripts/probe_source.py'
spec = importlib.util.spec_from_file_location('probe_source', SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_rmfh_is_candidate_not_claimed_decrypted(self):
        path = self.root / 'backup'
        path.write_bytes(b'RMFH' + bytes(124) + b'ciphertext' + b'RMFT' + bytes(124))
        result = probe.inspect(path)
        self.assertEqual(result['signature_counts'], {'rmfh_candidate': 1})
        self.assertFalse(result['decrypted'])
        self.assertNotIn('ciphertext', json.dumps(result))

    def test_damaged_rmfh_not_accepted(self):
        path = self.root / 'broken'
        path.write_bytes(b'RMFH' + bytes(10))
        self.assertEqual(probe.classify(path), 'rmfh_damaged')

    def test_json_structure_not_extension(self):
        path = self.root / 'export.json'
        path.write_text(json.dumps({'chatlab': {}, 'members': [], 'messages': []}))
        self.assertEqual(probe.classify(path), 'chatlab_json_candidate')
        path.write_text('{invalid')
        self.assertEqual(probe.classify(path), 'json_invalid')

    def test_scanning_limit_exposes_partial_coverage(self):
        for number in range(3):
            (self.root / f'{number}.db').write_bytes(b'unknown')
        result = probe.inspect(self.root, max_files=2)
        self.assertEqual(result['files_inspected'], 2)
        self.assertTrue(result['limit_reached'])
        self.assertFalse(result['scan_complete'])

    def test_no_message_text_or_paths_in_success_report(self):
        (self.root / 'private-name.txt').write_text('PRIVATE_CHAT_MESSAGE', encoding='utf-8')
        serialized = json.dumps(probe.inspect(self.root))
        self.assertNotIn('PRIVATE_CHAT_MESSAGE', serialized)
        self.assertNotIn('private-name', serialized)

    def test_empty_directory_not_ready_for_analysis(self):
        result = probe.inspect(self.root)
        self.assertEqual(result['candidate_routes'], ['unknown_format'])
        self.assertFalse(result['parsed'])
        self.assertFalse(result['imported'])

    def test_file_hash_unchanged(self):
        path = self.root / 'a.db'
        content = b'SQLite format 3\x00' + bytes(100)
        path.write_bytes(content)
        self.assertEqual(probe.classify(path), 'sqlite_plaintext')
        self.assertEqual(path.read_bytes(), content)


if __name__ == '__main__':
    unittest.main()
