"""Isolated orchestration tests: no live process, account or ChatLab access."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/chat-relationship-workflow/scripts'
sys.path.insert(0, str(SCRIPTS))
import desktop_workflow as flow
import desktop_runtime as runtime
import desktop_export as exporter
import chatlab_pipeline as pipeline


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / 'source' / 'db_storage'
        (self.source / 'contact').mkdir(parents=True)
        (self.source / 'message').mkdir()
        (self.source / 'contact/contact.db').write_bytes(b'synthetic')
        self.directory = self.base / 'run'
        self.request = {'contact': 'Synthetic peer', 'db_root': str(self.source), 'scan_timeout': 120}
        self.identity = {'self_id': 'synthetic-self', 'target_id': 'synthetic-peer', 'target_name': 'Synthetic peer'}
        self.coverage = {'source_records': 3, 'selected_records': 3, 'full_phone_history_verified': False}
        self.secret = 'c5' * 32
        self.key_provider = patch.object(runtime, 'acquire_keys', side_effect=lambda *a, **k: {
            'databases': {'message/message_0.db': {'key_hex': self.secret}}}).start()
        self.resolver = patch.object(exporter, 'resolve_contact', return_value=self.identity).start()
        self.exporter = patch.object(exporter, 'export_chat', side_effect=self.export).start()
        self.session = patch.object(pipeline, 'resolve_session_id', return_value='synthetic-session').start()
        self.importer = patch.object(pipeline, 'import_verified', side_effect=self.import_chat).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.temp.cleanup)

    def export(self, source, **kwargs):
        directory = kwargs['output']
        directory.mkdir()
        (directory / 'chatlab.json').write_text('{"synthetic": true}', encoding='utf-8')
        (directory / 'raw_messages.jsonl').write_text('{"synthetic": true}\n', encoding='utf-8')
        audit = {'coverage': self.coverage, 'exported_messages': 3}
        (directory / 'audit.json').write_text(json.dumps(audit), encoding='utf-8')
        return audit

    def import_chat(self, export, session, **kwargs):
        self.assertEqual(kwargs['expected_owner_id'], self.identity['self_id'])
        self.assertEqual(kwargs['expected_peer_id'], self.identity['target_id'])
        kwargs['callback']('chatlab_verified', {'new': 3, 'key_hex': self.secret})
        return {'status': 'import_verified', 'session_id': session, 'new_count': 3}

    def test_pipeline_stops_at_analysis_ready_and_keeps_keys_out_of_files(self):
        state = flow.run(self.request, self.directory)
        self.assertEqual(state['status'], 'analysis_ready')
        self.assertEqual(state['session_id'], 'synthetic-session')
        self.assertFalse((self.directory / 'relationship-report.txt').exists())
        handoff = json.loads((self.directory / 'analysis-request.json').read_text(encoding='utf-8'))
        self.assertFalse(handoff['coverage']['full_phone_history_verified'])
        for file in self.directory.rglob('*'):
            if file.is_file():
                self.assertNotIn(self.secret, file.read_text(encoding='utf-8'))

    def test_export_failure_never_imports(self):
        self.exporter.side_effect = exporter.ExportError('empty range', code='empty_time_range', details={'coverage': self.coverage})
        with self.assertRaises(exporter.ExportError):
            flow.run(self.request, self.directory)
        self.importer.assert_not_called()
        state = flow.load_state(self.directory)
        self.assertEqual(state['status'], 'needs_attention')
        self.assertEqual(state['error']['code'], 'empty_time_range')

    def test_import_failure_can_resume_frozen_export_without_reading_wechat_again(self):
        self.importer.side_effect = pipeline.ChatLabPipelineError('chatlab_timeout', 'importing', 'timeout')
        with self.assertRaises(pipeline.ChatLabPipelineError):
            flow.run(self.request, self.directory)
        self.importer.side_effect = self.import_chat
        result = flow.resume(self.directory)
        self.assertEqual(result['status'], 'analysis_ready')
        self.assertEqual(self.key_provider.call_count, 1)
        self.assertEqual(self.exporter.call_count, 1)

    def test_modified_export_is_rejected_before_import(self):
        flow.run(self.request, self.directory)
        (self.directory / 'export-1/chatlab.json').write_text('changed')
        with self.assertRaises(flow.WorkflowError) as caught:
            flow.resume(self.directory)
        self.assertEqual(caught.exception.code, 'artifact_changed')
        self.assertEqual(self.importer.call_count, 1)

    def test_resume_cannot_change_contact_after_export(self):
        flow.run(self.request, self.directory)
        with self.assertRaises(flow.WorkflowError) as caught:
            flow.resume(self.directory, {'contact': 'Someone else'})
        self.assertEqual(caught.exception.code, 'frozen_scope')

    def test_ambiguous_account_can_be_selected_on_resume(self):
        request = dict(self.request)
        request.pop('db_root')
        with patch.object(runtime, 'discover_accounts', return_value=[self.source, self.base / 'other']):
            with self.assertRaises(flow.WorkflowError) as caught:
                flow.run(request, self.directory)
        self.assertEqual(caught.exception.code, 'needs_account_selection')
        self.key_provider.assert_not_called()
        result = flow.resume(self.directory, {'db_root': str(self.source)})
        self.assertEqual(result['status'], 'analysis_ready')

    def test_target_ambiguity_preserved_without_guessing(self):
        self.resolver.side_effect = exporter.ExportError('ambiguous', code='target_ambiguous', details={'candidates': ['a', 'b']})
        with self.assertRaises(exporter.ExportError):
            flow.run(self.request, self.directory)
        self.exporter.assert_not_called()
        self.assertEqual(flow.load_state(self.directory)['error']['details']['candidates'], ['a', 'b'])

    def test_existing_directory_not_overwritten(self):
        self.directory.mkdir()
        marker = self.directory / 'keep'
        marker.write_text('keep')
        with self.assertRaises(flow.WorkflowError):
            flow.run(self.request, self.directory)
        self.assertEqual(marker.read_text(), 'keep')
        self.key_provider.assert_not_called()

    def test_source_output_overlap_rejected(self):
        with self.assertRaises(flow.WorkflowError):
            flow.run(self.request, self.source / 'output')
        self.assertFalse((self.source / 'output').exists())

    def test_auto_discovered_source_is_protected_before_output_creation(self):
        request = dict(self.request)
        request.pop('db_root')
        with patch.object(runtime, 'discover_accounts', return_value=[self.source]):
            with self.assertRaises(flow.WorkflowError) as caught:
                flow.run(request, self.source / 'output')
        self.assertEqual(caught.exception.code, 'source_output_overlap')
        self.assertFalse((self.source / 'output').exists())
        self.key_provider.assert_not_called()

    def test_finish_requires_real_local_files_and_import_verification(self):
        flow.run(self.request, self.directory)
        report = self.directory / 'report.txt'
        ledger = self.directory / 'ledger.txt'
        with self.assertRaises(flow.WorkflowError):
            flow.finish_analysis(self.directory, report, ledger)
        report.write_text('Synthetic evidence-based report', encoding='utf-8')
        ledger.write_text('Synthetic evidence coverage', encoding='utf-8')
        result = flow.finish_analysis(self.directory, report, ledger)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['analysis_artifacts']['report']['sha256'], hashlib.sha256(report.read_bytes()).hexdigest())
        self.assertEqual(flow.resume(self.directory)['status'], 'complete')
        self.assertEqual(self.importer.call_count, 1)

    def test_outside_or_same_analysis_files_rejected(self):
        flow.run(self.request, self.directory)
        outside = self.base / 'outside.txt'
        outside.write_text('not this run')
        inside = self.directory / 'report.txt'
        inside.write_text('report')
        with self.assertRaises(flow.WorkflowError):
            flow.finish_analysis(self.directory, outside, inside)
        with self.assertRaises(flow.WorkflowError):
            flow.finish_analysis(self.directory, inside, inside)

    def test_invalid_range_fails_before_state_or_scan(self):
        with self.assertRaises(flow.WorkflowError):
            flow.run(self.request | {'since': 1700000100, 'until': 1700000000}, self.directory)
        self.assertFalse(self.directory.exists())
        self.key_provider.assert_not_called()

    def test_date_boundaries_have_explicit_offset_and_inclusive_end(self):
        first = flow.time_boundary('2026-01-01')
        last = flow.time_boundary('2026-01-01', end=True)
        self.assertEqual(last - first, 86399)
        self.assertEqual(first, flow.time_boundary('2025-12-31T16:00:00+00:00'))
        with self.assertRaises(flow.WorkflowError):
            flow.time_boundary('not a date')

    def test_cli_error_does_not_dump_unexpected_exception(self):
        import contextlib
        import io
        with patch.object(flow, 'discover', side_effect=RuntimeError(self.secret)), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(flow.main(['discover']), 2)
        self.assertNotIn(self.secret, out.getvalue())


if __name__ == '__main__':
    unittest.main()
