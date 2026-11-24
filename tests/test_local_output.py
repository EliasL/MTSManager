from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from MTS_Management.cluster import load_sites, paths
from MTS_Management.errors import ExpectedError
from MTS_Management.files import locked
from MTS_Management.local_output import change_local_output, plan_local_output


class LocalOutputTests(unittest.TestCase):
    def test_move_refuses_a_run_locked_outside_the_manager(self):
        with TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            sites = base / "sites.toml"
            sites.write_text("[sites.local]\n")
            run = base / "output/run"
            run.mkdir(parents=True)
            (run / "manifest.json").write_text("{}")
            target = base / "new"
            with locked(run / ".run.lock"):
                plan = plan_local_output(sites, "local", str(target), move=True)
                self.assertTrue(plan["blocked"])
                with self.assertRaisesRegex(ExpectedError, "Wait for these runs"):
                    change_local_output(sites, "local", str(target), move=True, source=plan["source"])
            self.assertTrue((run / "manifest.json").is_file())
            self.assertEqual(paths(load_sites(sites)["local"])["output_root"], str(run.parent))

    def test_failed_publication_leaves_source_and_archive_available(self):
        with TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            sites = base / "sites.toml"
            sites.write_text("[sites.local]\n")
            source, target = base / "output", base / "new"
            source.mkdir()
            (source / "data").write_text("original")
            archive = base / "output-archive"
            archive.mkdir()
            (archive / "data").write_text("archived")
            with patch("MTS_Management.local_output.write_json", side_effect=OSError("write failed")):
                with self.assertRaisesRegex(ExpectedError, "write failed"):
                    change_local_output(sites, "local", str(target), move=True, source=str(source), log=lambda _: None)
            self.assertEqual(paths(load_sites(sites)["local"])["output_root"], str(source))
            self.assertEqual((source / "data").read_text(), "original")
            self.assertEqual((archive / "data").read_text(), "archived")
            self.assertFalse(any(target.iterdir()))
            self.assertFalse(any((base / "new-archive").iterdir()))
            result = change_local_output(sites, "local", str(target), move=True, source=str(source), log=lambda _: None)
            self.assertFalse(result["errors"])
            self.assertEqual((target / "data").read_text(), "original")
            self.assertEqual((base / "new-archive/data").read_text(), "archived")
