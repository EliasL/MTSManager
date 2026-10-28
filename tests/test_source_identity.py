"""A fresh submodule must build without accepting source outside main history."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from MTS_Management.provenance import source_identity, source_hash, install_source, source_archive
from MTS_Management.files import write_json


class SourceIdentityTests(unittest.TestCase):
    def test_source_update_keeps_compiled_files_and_removes_obsolete_tracked_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, staged = root / "MTS2D", root / ".mts-source-upload-fixture"
            for directory, content, files in ((source, "old", ["src/main.cpp", "src/old.cpp"]),
                                               (staged, "new", ["src/main.cpp"])):
                (directory / "src").mkdir(parents=True)
                for name in files:
                    (directory / name).write_text(content)
                write_json(directory / "SOURCE_FILES.json", files)
                write_json(directory / "SOURCE.json", {"branch": "main", "revision": content,
                           "source_sha256": source_hash(directory)})
            (source / "build").mkdir()
            binary = source / "build/MTS2D"
            binary.write_bytes(b"retained old binary")
            expected = source_archive(staged, staged / "source.tar.gz")
            for name in ("src/main.cpp", "SOURCE_FILES.json", "SOURCE.json"):
                (staged / name).unlink()
            install_source(source, staged, expected)
            self.assertEqual(source_identity(source), expected)
            self.assertEqual(binary.read_bytes(), b"retained old binary")
            self.assertFalse((source / "src/old.cpp").exists())
            self.assertFalse(staged.exists())

    def test_detached_mainline_snapshot_preserves_revision_and_rejects_other_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            source = root / "src/main.cpp"
            source.write_text("int main() {}\n")
            def git(*args):
                return subprocess.check_output(["git", "-C", str(root), *args],
                                               text=True, stderr=subprocess.PIPE).strip()
            git("init", "-q", "-b", "main")
            git("config", "user.name", "MTS test")
            git("config", "user.email", "test@example.invalid")
            git("add", "src")
            git("commit", "-qm", "mainline source")
            revision = git("rev-parse", "HEAD")
            git("update-ref", "refs/remotes/origin/main", revision)
            git("checkout", "-q", "--detach")
            self.assertEqual(source_identity(root)["revision"], revision)
            self.assertEqual(git("branch", "--show-current"), "")
            git("checkout", "-q", "-b", "experimental")
            source.write_text("int main() { return 1; }\n")
            git("commit", "-qam", "outside main history")
            git("checkout", "-q", "--detach")
            with self.assertRaisesRegex(ValueError, "require MTS2D main"):
                source_identity(root)
