"""Behavioral installer checks using temporary fake skills; never touch user data.

The real dependency-download smoke test is performed separately in an isolated
runtime. CI exercises PowerShell 5.1 path isolation, backup, quoting and ZIP guards.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ.get("WINDIR", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
SKILLS = ("chat-relationship-workflow", "emotion-analysis-evidence", "emotion-analysis-advisor")


@unittest.skipUnless(os.name == "nt" and POWERSHELL.is_file(), "Requires Windows PowerShell 5.1")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="emotion installer ")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / "source"
        self.source.mkdir()
        (self.source / "setup").mkdir()
        for relative in ("Install-Workflow.ps1", "setup/Installer.Core.ps1"):
            shutil.copy2(ROOT / relative, self.source / relative)
        for name in SKILLS:
            skill = self.source / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Synthetic installer fixture.\n---\nTest only.\n", encoding="utf-8")
        self.skills = self.work / "installed skills"
        self.runtime = self.work / "private runtime"

    def install(self, *, skills=None):
        return subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(self.source / "Install-Workflow.ps1"), "-SkillsOnly", "-SkillsRoot", str(skills or self.skills),
             "-RuntimeRoot", str(self.runtime)], capture_output=True, timeout=60,
        )

    def ps(self, code):
        script = self.work / "probe.ps1"
        script.write_text("$ErrorActionPreference='Stop'\n. '" + str(self.source / "setup/Installer.Core.ps1").replace("'", "''") + "'\n" + code, encoding="utf-8-sig")
        return subprocess.run([str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], capture_output=True, timeout=60)

    def test_installs_three_skills_without_private_config_or_cache(self):
        main = self.source / "skills" / SKILLS[0]
        (main / "runtime.json").write_text('{"private":true}', encoding="utf-8")
        (main / "__pycache__").mkdir()
        (main / "__pycache__/ignored.pyc").write_bytes(b"not distributable")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        for name in SKILLS:
            self.assertEqual((self.skills/name/"SKILL.md").read_bytes(), (self.source/"skills"/name/"SKILL.md").read_bytes())
        self.assertFalse((self.skills/SKILLS[0]/"runtime.json").exists())
        self.assertFalse((self.skills/SKILLS[0]/"__pycache__").exists())

    def test_reinstall_is_idempotent_and_update_preserves_backup(self):
        self.assertEqual(self.install().returncode, 0)
        target = self.skills / SKILLS[0]
        (target / "runtime.json").write_text('{"schema":1}', encoding="utf-8")
        self.assertEqual(self.install().returncode, 0)
        self.assertFalse((self.runtime / "backups").exists())
        original = (target / "SKILL.md").read_bytes()
        (self.source / "skills" / SKILLS[0] / "SKILL.md").write_text("Updated fixture", encoding="utf-8")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        backups = list((self.runtime / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "SKILL.md").read_bytes(), original)
        self.assertEqual((backups[0] / "runtime.json").read_text(), '{"schema":1}')
        self.assertEqual((target / "runtime.json").read_text(), '{"schema":1}')

    def test_refuses_output_under_source_before_creating_folders(self):
        destination = self.source / "installed"
        result = self.install(skills=destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(destination.exists())
        self.assertFalse(self.runtime.exists())

    def test_missing_companion_stops_before_writes(self):
        (self.source / "skills" / SKILLS[2] / "SKILL.md").unlink()
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.skills.exists())
        self.assertFalse(self.runtime.exists())

    def test_argument_round_trip_has_no_shell_interpretation(self):
        echo = self.work / "echo args.py"
        echo.write_text("import json,sys; print(json.dumps(sys.argv[1:]))", encoding="utf-8")
        values = ["", "two words", 'middle"quote', 'slash\\"quote', "ending slash \\", "literal;$(nothing)", "合成名称"]
        literal = lambda value: "'" + str(value).replace("'", "''") + "'"
        args = ",".join(literal(v) for v in [echo, *values])
        code = f"Invoke-InstallProcess {literal(sys.executable)} @({args}) @{{}} {literal(self.work)} 'argument check'\n"
        result = self.ps(code)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(json.loads(result.stdout.decode("utf-8-sig")), values)

    def test_zip_traversal_rejected_before_extracting(self):
        archive = self.work / "bad.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("../escaped.txt", "must not write")
        result = self.ps(f"Expand-VerifiedArchive '{archive}' '{self.work / 'extract'}'\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.work / "escaped.txt").exists())
        self.assertFalse((self.work / "extract").exists())

    def test_partial_archive_is_recovered_even_with_existing_executable(self):
        archive = self.work / "runtime.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("node/node.exe", b"synthetic executable")
            output.writestr("node/npm/npm-cli.js", "synthetic npm")
        destination = self.work / "runtime files"
        (destination / "node").mkdir(parents=True)
        (destination / "node/node.exe").write_bytes(b"partial previous extraction")
        marker = destination / "complete"
        marker.write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
        result = self.ps(f"Ensure-InstallArchive '{archive}' '{destination}' '{marker}' @('{destination / 'node/node.exe'}','{destination / 'node/npm/npm-cli.js'}')\n")
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual((destination / "node/node.exe").read_bytes(), b"synthetic executable")
        self.assertEqual((destination / "node/npm/npm-cli.js").read_text(), "synthetic npm")

    def test_cached_broken_package_is_reinstalled_once_then_verified(self):
        marker = self.work / "package-marker"
        marker.write_text("trusted-hash")
        result = self.ps("$script:counts=@{install=0;verify=0}\n"
                         "$install={ $script:counts.install++ }\n"
                         "$verify={ $script:counts.verify++; if ($script:counts.install -eq 0) { throw 'broken dependency' } }\n"
                         f"$null=Invoke-VerifiedPackageSetup $true '{marker}' 'trusted-hash' $install $verify\n"
                         "$script:counts | ConvertTo-Json -Compress\n")
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        counts = json.loads(result.stdout.decode().splitlines()[-1])
        self.assertEqual(counts, {"install": 1, "verify": 2})
        self.assertEqual(marker.read_text(), "trusted-hash")

    def test_failed_repair_leaves_marker_invalid_and_does_not_loop(self):
        marker = self.work / "package-marker"
        marker.write_text("trusted-hash")
        result = self.ps("$script:counts=@{install=0;verify=0}\n"
                         "$install={ $script:counts.install++ }\n"
                         "$verify={ $script:counts.verify++; throw 'still broken' }\n"
                         f"try {{ $null=Invoke-VerifiedPackageSetup $true '{marker}' 'trusted-hash' $install $verify }} catch {{ }}\n"
                         "$script:counts | ConvertTo-Json -Compress\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout.decode().splitlines()[-1]), {"install": 1, "verify": 2})
        self.assertEqual(marker.read_text(), "incomplete")

    def test_timeout_terminates_created_child_process_tree(self):
        import ctypes
        pid_file = self.work / "own-processes.json"
        parent = self.work / "spawn children.py"
        parent.write_text("import json,os,subprocess,sys,time\nfrom pathlib import Path\n"
                          "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                          "Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid]))\n"
                          "time.sleep(90)\n", encoding="utf-8")
        result = self.ps(f"Invoke-InstallProcess '{sys.executable}' @('{parent}','{pid_file}') @{{}} '{self.work}' 'timeout probe' 2\n")
        self.assertNotEqual(result.returncode, 0)
        pids = json.loads(pid_file.read_text())
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        for pid in pids:
            handle = kernel.OpenProcess(0x00100000, False, pid)
            if handle:
                try:
                    status = kernel.WaitForSingleObject(handle, 2000)
                    if status == 258:  # Clean up only this test's own child if the assertion would fail.
                        subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
                    self.assertNotEqual(status, 258, f"Installer left its child {pid} running")
                finally:
                    kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
