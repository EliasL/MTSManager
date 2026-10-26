"""A fresh submodule must build without accepting source outside main history."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from MTS_Management.provenance import source_identity


class SourceIdentityTests(unittest.TestCase):
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
