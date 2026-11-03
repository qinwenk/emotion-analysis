import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'skills/chat-relationship-workflow/scripts/runtime_launcher.py'
spec = importlib.util.spec_from_file_location('runtime_launcher_tested', SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class ManagedLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.skill = self.root / 'installed skill'
        self.skill.mkdir()
        managed = self.root / '专用 runtime'
        self.config = {'schema': 1, 'runtime_root': str(managed)}
        for key, rel in [('python', 'python/python.exe'), ('node', 'node/node.exe'), ('clb', 'npm/clb.cmd')]:
            p = managed / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('fixture', encoding='utf-8')
            self.config[key] = str(p)
        entry = managed / 'npm/node_modules/chatlab-cli/bin/chatlab.mjs'
        entry.parent.mkdir(parents=True)
        entry.write_text('fixture', encoding='utf-8')
        self.save()

    def save(self):
        (self.skill / 'runtime.json').write_text(json.dumps(self.config), encoding='utf-8-sig')

    def runtime(self):
        return launcher.load_runtime(self.skill)

    def test_load_installer_config_and_missing_config(self):
        self.assertTrue(self.runtime()['entry'].is_file())
        (self.skill / 'runtime.json').unlink()
        with self.assertRaises(launcher.LaunchError) as caught:
            self.runtime()
        self.assertEqual(caught.exception.code, 'runtime_not_ready')

    def test_external_interpreter_is_rejected(self):
        external = self.root / 'external.exe'
        external.touch()
        self.config['python'] = str(external)
        self.save()
        with self.assertRaises(launcher.LaunchError):
            self.runtime()

    def test_literals_and_managed_cli_are_preserved(self):
        runtime = self.runtime()
        args = ['run', '--contact', '测试 "名字" & $(nothing)', '--run-dir', 'D:/空间/任务']
        command = launcher.build_command(runtime, 'workflow', args, self.skill)
        self.assertEqual(command[5:], args + ['--clb', str(runtime['clb'])])
        self.assertEqual(args[-1], 'D:/空间/任务')
        query = ['messages', 'search', '他说"没空" & 不是拒绝', '--format', 'agent']
        self.assertEqual(launcher.build_command(runtime, 'chatlab', query)[2:], query)

    def test_explicit_cli_not_overwritten_and_status_needs_none(self):
        runtime = self.runtime()
        command = launcher.build_command(runtime, 'workflow', ['resume', '--clb=D:/chosen/clb.cmd'])
        self.assertNotIn('--clb', command)
        self.assertNotIn('--clb', launcher.build_command(runtime, 'workflow', ['status', '--run-dir', 'D:/one']))

    def test_invalid_request_not_executable(self):
        for args in ('run --contact test', ['run', None], ['run', 'a\0b']):
            with self.assertRaises(launcher.LaunchError):
                launcher.build_command(self.runtime(), 'workflow', args)
        with self.assertRaises(launcher.LaunchError):
            launcher.build_command(self.runtime(), 'arbitrary', [])

    def test_child_environment_keeps_real_data_location_without_global_changes(self):
        env = {'Path': 'original', 'USERPROFILE': 'real-user', 'CHATLAB_DATA_DIR': 'chosen-data',
               launcher.REQUEST_VAR: 'private parameters'}
        child = launcher.child_environment(self.runtime(), env)
        self.assertEqual(env['Path'], 'original')
        self.assertEqual(child['USERPROFILE'], 'real-user')
        self.assertEqual(child['CHATLAB_DATA_DIR'], 'chosen-data')
        self.assertNotIn(launcher.REQUEST_VAR, child)
        self.assertTrue(child['PATH'].endswith(os.pathsep + 'original'))

    def test_health_check_isolates_before_chatlab(self):
        runtime = self.runtime()
        original = {'USERPROFILE': 'real-user', 'CHATLAB_DATA_DIR': 'real-data', 'PATH': 'original'}
        seen = []
        def capture(cmd, env):
            seen.append((list(cmd), dict(env)))
            if cmd[0] == str(runtime['python']):
                return json.dumps({'version': [3, 12, 12], 'bits': 64, 'cryptography': '50.0.2', 'zstandard': '0.25.0'})
            if cmd[-1] == '--version':
                return 'v22.21.1'
            if cmd[-1] == 'require("node:os").homedir()':
                return env['USERPROFILE']
            self.assertNotEqual(env['USERPROFILE'], 'real-user')
            self.assertNotEqual(env['CHATLAB_DATA_DIR'], 'real-data')
            self.assertIn(str(runtime['runtime_root']), env['CHATLAB_DATA_DIR'])
            if '-e' in cmd:
                self.assertIn(':memory:', cmd[2])
                return '{"value":1}'
            self.assertEqual(cmd[-1], 'manifest')
            return '{"version":"0.37.7","apiVersion":1}'
        with patch.object(launcher, '_capture', side_effect=capture):
            result = launcher.check_runtime(runtime, original)
        self.assertTrue(result['ok'])
        self.assertFalse(result['user_chatlab_accessed'])
        self.assertTrue(result['sqlite_read_write'])
        self.assertEqual(len(seen), 5)
        self.assertEqual(original['USERPROFILE'], 'real-user')

    def test_isolation_failure_never_starts_chatlab(self):
        with patch.object(launcher, '_capture', side_effect=[
            '{"version":[3,12,12],"bits":64,"cryptography":"50","zstandard":"0.25"}',
            'v22.21.1', str(self.root / 'wrong-profile')]) as capture:
            with self.assertRaises(launcher.LaunchError) as caught:
                launcher.check_runtime(self.runtime(), {})
        self.assertEqual(caught.exception.code, 'check_not_isolated')
        self.assertEqual(capture.call_count, 3)

    def test_broken_native_database_does_not_report_ready(self):
        runtime = self.runtime()
        def capture(cmd, env):
            if cmd[0] == str(runtime['python']):
                return '{"version":[3,12,12],"bits":64,"cryptography":"50","zstandard":"0.25"}'
            if cmd[-1] == '--version':
                return 'v22.21.1'
            if cmd[-1] == 'require("node:os").homedir()':
                return env['USERPROFILE']
            if cmd[-1] == 'manifest':
                return '{"version":"0.37.7","apiVersion":1}'
            raise launcher.LaunchError('runtime_check_failed', 'Synthetic native module failure')
        with patch.object(launcher, '_capture', side_effect=capture):
            with self.assertRaises(launcher.LaunchError) as caught:
                launcher.check_runtime(runtime, {})
        self.assertEqual(caught.exception.code, 'runtime_check_failed')

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell stdout regression')
    def test_powershell_child_keeps_output_and_exit_status(self):
        powershell = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        if not powershell.is_file():
            self.skipTest('Windows PowerShell not available')
        probe = self.root / 'forward output.py'
        probe.write_text(
            'import sys,os\nfrom pathlib import Path\nfrom unittest.mock import patch\n'
            f'sys.path.insert(0,{str(SCRIPT.parent)!r})\nimport runtime_launcher as app\n'
            'runtime={k:Path(sys.executable) for k in ("node","python","clb")}\n'
            'command=[sys.executable,"-c","import sys; print(\'forwarded-stdout\'); '
            'print(\'forwarded-stderr\',file=sys.stderr); sys.exit(7)"]\n'
            'with patch.object(app,"load_runtime",return_value=runtime), '
            'patch.object(app,"build_command",return_value=command):\n'
            '    raise SystemExit(app.main())\n', encoding='utf-8')
        shell = self.root / 'probe.ps1'
        quote = lambda s: "'" + str(s).replace("'", "''") + "'"
        shell.write_text(f'& {quote(sys.executable)} -B -X utf8 {quote(probe)}\nexit $LASTEXITCODE\n', encoding='utf-8-sig')
        result = subprocess.run([str(powershell), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(shell)],
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn('forwarded-stdout', result.stdout)
        self.assertIn('forwarded-stderr', result.stderr)


if __name__ == '__main__':
    unittest.main()
