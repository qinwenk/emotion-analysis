"""Synthetic contract tests; no real chat access or ChatLab imports."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "skills/chat-relationship-workflow/scripts/chatlab_pipeline.py"
spec = importlib.util.spec_from_file_location("chatlab_pipeline", SCRIPT)
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def envelope(data, command="sql", session="selected", **meta):
    return {"ok": True, "command": command, "data": data,
            "meta": {"session": {"id": session}, **meta}}


def identity(match=True):
    return {"meta_rows": 1, "matching_meta": int(match), "owner_members": 1,
            "peer_members": 1, "foreign_members": 0, "system_members": 1}


def snapshot(owner=0, peer=0, system=0, first=None, last=None):
    return {"total": owner + peer + system, "owner_count": owner, "peer_count": peer,
            "system_count": system, "orphan_count": 0, "invalid_system_count": 0,
            "first_ts": first, "last_ts": last}


class FakeCLI:
    def __init__(self, existing=False, before=None, after=None, new=3, duplicates=0):
        self.existing = existing
        self.before = before or snapshot()
        self.after = after or snapshot(1, 1, 1, 100, 102)
        self.new, self.duplicates = new, duplicates
        self.calls = []
        self.imported = False
        self.scope_matches = True
        self.malformed_preview = False
        self.validation_warning = False
        self.preview_new = None
        self.returncode = 0
        self.timeout_stage = None
        self.skipped = 0
        self.session_changes = False
        self.list_calls = 0
        self.on_validate = None

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        args = argv[2:]
        cmd = args[0]
        if cmd == self.timeout_stage:
            raise subprocess.TimeoutExpired(argv, 1, output="DO NOT LEAK BODY", stderr="DO NOT LEAK KEY")
        if cmd == "manifest":
            response = {"version": "0.37.7", "apiVersion": 1}
        elif cmd == "validate":
            response = envelope({"valid": True, "errorCount": 0, "warningCount": int(self.validation_warning),
                                 "issues": [], "stats": {"messageCount": 3}}, "validate")
            if self.on_validate:
                self.on_validate()
        elif cmd == "sessions":
            self.list_calls += 1
            present = self.existing or (self.session_changes and self.list_calls > 1)
            items = [{"id": "selected", "platform": "weixin", "type": "private"}] if present else []
            response = envelope({"items": items}, "sessions.list")
        elif cmd == "sql":
            row = identity(self.scope_matches) if "AS meta_rows" in args[1] else self.after if self.imported else self.before
            response = envelope({"rows": [row], "truncated": False})
        elif cmd == "import":
            preview = "--dry-run" in args
            new = self.preview_new if preview and self.preview_new is not None else self.new
            data = {"success": not (preview and self.malformed_preview), "sessionId": "selected",
                    "importMode": "incremental" if self.existing else "created",
                    "newMessageCount": new, "duplicateCount": self.duplicates}
            if preview:
                data["totalMessageCount"] = 3
            else:
                self.imported = True
                data["diagnostics"] = {"messagesReceived": 3, "messagesWritten": self.new,
                                       "messagesSkipped": self.skipped, "duplicateCount": self.duplicates,
                                       "skipReasons": {"invalidTimestamp": self.skipped}}
            response = envelope(data, "import", dryRun=preview)
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(argv, self.returncode, json.dumps(response), "private stderr must not escape")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.export = Path(self.temp.name) / "chat with & symbols.json"
        self.payload = {
            "chatlab": {"version": "0.0.2"},
            "meta": {"ownerId": "self' account", "platform": "weixin", "type": "private", "name": "synthetic"},
            "members": [{"platformId": "self' account", "accountName": "Self"},
                        {"platformId": "peer", "accountName": "Peer"}],
            "messages": [
                {"sender": "self' account", "timestamp": 100, "type": 0, "content": "one", "platformMessageId": "1"},
                {"sender": "peer", "timestamp": 101, "type": 0, "content": "two", "platformMessageId": "2"},
                {"sender": "SYSTEM", "timestamp": 102, "type": 80, "content": "event", "platformMessageId": "3"},
            ],
        }
        self.write()

    def write(self):
        self.export.write_text(json.dumps(self.payload), encoding="utf-8")

    def execute(self, fake, **kwargs):
        with patch.object(pipeline, "resolve_clb_command", return_value=["node", "fake.mjs"]), patch.object(pipeline.subprocess, "run", side_effect=fake):
            return pipeline.import_verified(self.export, "selected", **kwargs)

    def assert_no_import(self, fake):
        self.assertFalse(fake.imported)

    def test_success_checks_counts_scope_and_no_shell(self):
        fake, events = FakeCLI(), []
        result = self.execute(fake, callback=lambda stage, details: events.append((stage, details)))
        self.assertEqual(result["status"], "import_verified")
        self.assertEqual(result["new_count"], 3)
        self.assertEqual(result["sender_deltas"], {"owner": 1, "peer": 1, "system": 1})
        self.assertEqual(events[-1][0], "chatlab_verified")
        for argv, kwargs in fake.calls:
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["encoding"], "utf-8")
            self.assertNotIn("--raw", argv)
            if argv[2] == "sql":
                self.assertNotIn("content", argv[3])
                self.assertNotIn("self' account", argv[3])
            if argv[2] == "import":
                self.assertEqual(argv[3], str(self.export))

    def test_repeat_import_preserves_counts(self):
        before = snapshot(1, 1, 1, 100, 102)
        result = self.execute(FakeCLI(True, before, before, new=0, duplicates=3))
        self.assertEqual(result["new_count"], 0)
        self.assertEqual(result["duplicate_count"], 3)
        self.assertEqual(result["before"], result["after"])

    def test_mixed_incremental_import(self):
        fake = FakeCLI(True, snapshot(1, 0, 0, 100, 100), snapshot(1, 1, 1, 100, 102), new=2, duplicates=1)
        result = self.execute(fake)
        self.assertEqual(result["after"]["total"] - result["before"]["total"], 2)
        self.assertEqual(result["sender_deltas"], {"owner": 0, "peer": 1, "system": 1})

    def test_warning_blocks_import(self):
        fake = FakeCLI()
        fake.validation_warning = True
        with self.assertRaises(pipeline.ChatLabPipelineError) as caught:
            self.execute(fake)
        self.assertEqual(caught.exception.stage, "validate")
        self.assert_no_import(fake)

    def test_failed_preview_blocks_import(self):
        fake = FakeCLI()
        fake.malformed_preview = True
        with self.assertRaises(pipeline.ChatLabPipelineError) as caught:
            self.execute(fake)
        self.assertEqual(caught.exception.stage, "dry_run")
        self.assert_no_import(fake)

    def test_wrong_existing_identity_blocks_preview(self):
        fake = FakeCLI(True)
        fake.scope_matches = False
        with self.assertRaises(pipeline.ChatLabPipelineError) as caught:
            self.execute(fake)
        self.assertEqual(caught.exception.code, "session_identity_mismatch")
        self.assertFalse(any(argv[2] == "import" for argv, _ in fake.calls))

    def test_timeout_is_sanitized(self):
        fake = FakeCLI()
        fake.timeout_stage = "import"
        with self.assertRaises(pipeline.ChatLabPipelineError) as caught:
            self.execute(fake)
        self.assertEqual(caught.exception.code, "chatlab_timeout")
        self.assertNotIn("DO NOT LEAK", str(caught.exception))
        self.assert_no_import(fake)

    def test_failure_exit_is_sanitized(self):
        fake = FakeCLI()
        fake.returncode = 1
        with self.assertRaises(pipeline.ChatLabPipelineError) as caught:
            self.execute(fake)
        self.assertNotIn("private stderr", str(caught.exception))
        self.assertEqual(caught.exception.details, {"exit_code": 1})
        self.assert_no_import(fake)

    def test_invalid_json_is_not_exposed(self):
        with patch.object(pipeline, "resolve_clb_command", return_value=["node", "fake.mjs"]), patch.object(pipeline.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "PRIVATE CONTENT", "")):
            with self.assertRaises(pipeline.ChatLabPipelineError) as caught:
                pipeline.import_verified(self.export, "selected")
        self.assertEqual(caught.exception.code, "chatlab_invalid_json")
        self.assertNotIn("PRIVATE CONTENT", str(caught.exception))

    def test_input_identity_and_stable_ids_required(self):
        for mutation, wanted in ((lambda p: p["messages"][1].pop("platformMessageId"), "unstable_message_ids"),
                                 (lambda p: p["messages"][1].update(sender="outsider"), "unknown_sender")):
            original = json.loads(json.dumps(self.payload))
            mutation(self.payload)
            self.write()
            fake = FakeCLI()
            with self.assertRaises(pipeline.ChatLabPipelineError) as caught:
                self.execute(fake)
            self.assertEqual(caught.exception.code, wanted)
            self.assertEqual(fake.calls, [])
            self.payload = original

    def test_file_changed_after_validation_stops_before_write(self):
        fake = FakeCLI()
        fake.on_validate = lambda: self.export.write_text("{}", encoding="utf-8")
        with self.assertRaises(pipeline.ChatLabPipelineError) as caught:
            self.execute(fake)
        self.assertEqual(caught.exception.code, "export_changed")
        self.assert_no_import(fake)

    def test_concurrent_session_creation_stops(self):
        fake = FakeCLI()
        fake.session_changes = True
        with self.assertRaises(pipeline.ChatLabPipelineError) as caught:
            self.execute(fake)
        self.assertEqual(caught.exception.code, "session_changed")
        self.assert_no_import(fake)

    def test_after_count_mismatch_reports_verification_failure(self):
        fake = FakeCLI(after=snapshot(1, 1, 0, 100, 101))
        with self.assertRaises(pipeline.ChatLabPipelineError) as caught:
            self.execute(fake)
        self.assertEqual(caught.exception.code, "import_verification_failed")
        self.assertTrue(fake.imported)

    def test_wrong_sender_distribution_fails_new_session(self):
        fake = FakeCLI(after=snapshot(2, 0, 1, 100, 102))
        with self.assertRaises(pipeline.ChatLabPipelineError) as caught:
            self.execute(fake)
        self.assertEqual(caught.exception.code, "sender_count_mismatch")

    def test_skipped_messages_fail_after_import_without_claiming_success(self):
        fake = FakeCLI()
        fake.skipped = 1
        with self.assertRaises(pipeline.ChatLabPipelineError) as caught:
            self.execute(fake)
        self.assertEqual(caught.exception.stage, "import")
        self.assertTrue(fake.imported)

    def test_explicit_identity_guard(self):
        fake = FakeCLI()
        with self.assertRaises(pipeline.ChatLabPipelineError) as caught:
            self.execute(fake, expected_owner_id="someone_else")
        self.assertEqual(caught.exception.code, "export_identity_mismatch")
        self.assertEqual(fake.calls, [])

    def test_command_resolves_npm_shim_without_shell(self):
        root = Path(self.temp.name)
        shim = root / "clb.cmd"
        shim.write_text("do not execute this shell file", encoding="utf-8")
        entry = root / "node_modules/chatlab-cli/bin/chatlab.mjs"
        entry.parent.mkdir(parents=True)
        entry.write_text("", encoding="utf-8")
        node = root / "node.exe"
        node.write_bytes(b"")
        self.assertEqual(pipeline.resolve_clb_command(shim), [str(node), str(entry)])

    def test_session_reuse_and_ambiguity(self):
        class LookupCLI:
            def __init__(self, *args):
                pass
            def manifest(self):
                return {}
            def sessions(self):
                return [{"id": i, "platform": "weixin", "type": "private"} for i in self.ids]
            def sql(self, sid, query, stage):
                return identity(sid.startswith("match"))
        LookupCLI.ids = ["unrelated", "match_old"]
        with patch.object(pipeline, "_Client", LookupCLI):
            self.assertEqual(pipeline.resolve_session_id("me", "peer"), "match_old")
            LookupCLI.ids = ["unrelated"]
            generated = pipeline.resolve_session_id("me", "peer")
            self.assertRegex(generated, r"^wechat-desktop-[0-9a-f]{20}$")
            self.assertEqual(generated, pipeline.resolve_session_id("me", "peer"))
            self.assertNotEqual(generated, pipeline.resolve_session_id("peer", "me"))
            LookupCLI.ids = ["match_old", "match_new"]
            with self.assertRaises(pipeline.ChatLabPipelineError) as caught:
                pipeline.resolve_session_id("me", "peer")
            self.assertEqual(caught.exception.code, "session_ambiguous")
            self.assertEqual(caught.exception.details["session_ids"], LookupCLI.ids)


if __name__ == "__main__":
    unittest.main()
