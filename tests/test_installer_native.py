"""Native installer archive tests with synthetic bytes, never an executed binary."""
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("installer_native", Path(__file__).resolve().parents[1] / "setup/extract_native.py")
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


class NativeArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / "better-sqlite3"
        self.package.mkdir()
        (self.package / "package.json").write_text('{"version":"synthetic"}')
        self.archive = self.root / "synthetic.tar.gz"

    def build(self, entries):
        with tarfile.open(self.archive, "w:gz") as output:
            for name, data, kind in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                if kind == tarfile.REGTYPE:
                    member.size = len(data)
                    output.addfile(member, io.BytesIO(data))
                else:
                    member.linkname = "../../outside" if kind == tarfile.SYMTYPE else ""
                    output.addfile(member)
        return hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def test_expected_binary_is_written_and_other_package_files_preserved(self):
        digest = self.build([("build", b"", tarfile.DIRTYPE), ("build/Release", b"", tarfile.DIRTYPE), (native.MEMBER, b"MZsynthetic", tarfile.REGTYPE)])
        result = native.install_native(self.archive, self.package, digest)
        self.assertEqual(result.read_bytes(), b"MZsynthetic")
        self.assertEqual((self.package / "package.json").read_text(), '{"version":"synthetic"}')
        self.assertFalse(list(result.parent.glob(".sqlite-native-*")))

    def test_wrong_hash_preserves_existing_binary(self):
        self.build([(native.MEMBER, b"MZnew", tarfile.REGTYPE)])
        output = self.package / native.MEMBER
        output.parent.mkdir(parents=True)
        output.write_bytes(b"MZold")
        with self.assertRaisesRegex(ValueError, "checksum"):
            native.install_native(self.archive, self.package, "0" * 64)
        self.assertEqual(output.read_bytes(), b"MZold")

    def test_extra_traversal_entry_stops_before_any_extraction(self):
        digest = self.build([(native.MEMBER, b"MZsafe", tarfile.REGTYPE), ("../outside", b"bad", tarfile.REGTYPE)])
        with self.assertRaisesRegex(ValueError, "unsafe"):
            native.install_native(self.archive, self.package, digest)
        self.assertFalse((self.root / "outside").exists())
        self.assertFalse((self.package / "build").exists())

    def test_link_at_expected_path_is_rejected(self):
        digest = self.build([(native.MEMBER, b"", tarfile.SYMTYPE)])
        with self.assertRaisesRegex(ValueError, "unsafe"):
            native.install_native(self.archive, self.package, digest)
        self.assertFalse((self.package / "build").exists())

    def test_duplicate_binary_entries_are_rejected(self):
        digest = self.build([(native.MEMBER, b"MZone", tarfile.REGTYPE), (native.MEMBER, b"MZtwo", tarfile.REGTYPE)])
        with self.assertRaisesRegex(ValueError, "exactly one"):
            native.install_native(self.archive, self.package, digest)
        self.assertFalse((self.package / "build").exists())

    def test_non_windows_payload_is_not_installed(self):
        digest = self.build([(native.MEMBER, b"not a Windows DLL", tarfile.REGTYPE)])
        with self.assertRaisesRegex(ValueError, "Windows binary"):
            native.install_native(self.archive, self.package, digest)
        self.assertFalse((self.package / "build").exists())


if __name__ == "__main__":
    unittest.main()
