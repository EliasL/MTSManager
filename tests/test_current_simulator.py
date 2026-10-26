"""Optional real-simulator smoke tests after `mts build` in this checkout."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from MTS_Management.config import Config
from MTS_Management.data import read_macros
from MTS_Management.files import read_json
from MTS_Management.inventory import inventory
from MTS_Management.runner import PACKAGE_ROOT, execute, prepare

EXE = PACKAGE_ROOT / "build/MTS2D"


@unittest.skipUnless(EXE.exists() and (EXE.parent/"build-manifest.json").exists(),
                     "Build the current MTS2D executable to run integration smoke tests")
class CurrentSimulatorTests(unittest.TestCase):
    def test_latest_final_dump_does_not_repeat_completed_loading(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = Config(rows=4, cols=4, boundary="periodic", initialGuessNoise=0,
                            totalLoad=.004, loadIncrement=.001, stopAfterSteps=2,
                            checkpointInterval=1, loading="noRelaxation", writeMeshVTUs=False)
            options = dict(executable=EXE, output_root=root/"output", state_dir=root/"state")
            first = prepare(config, **options)
            with redirect_stdout(io.StringIO()):
                execute(first)
            folder = first.parent.parent
            before = (folder/"macroData.csv").read_bytes()
            latest = prepare(config, checkpoint="latest", **options)
            with redirect_stdout(io.StringIO()):
                execute(latest)
            self.assertEqual((folder/"macroData.csv").read_bytes(), before)
            self.assertEqual(read_json(folder/"termination.json")["termination"]["steps"], 2)
            selected = read_json(latest/"request.json")["input"]["checkpoint"]
            self.assertNotIn("checkpoint_", Path(selected).name)  # Final dump, not the older rotating slots.
            self.assertEqual(read_json(latest/"status.json")["returncode"], 0)

    def test_real_step_target_summary_and_checkpoint_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = Config(rows=4, cols=4, initialGuessNoise=0, boundary="periodic",
                            totalLoad=.004, loadIncrement=.001, checkpointInterval=1,
                            loading="noRelaxation", stopAfterSteps=2,
                            writeMeshVTUs=False)
            options = dict(executable=EXE, output_root=root/"output", state_dir=root/"state")
            attempt = prepare(config, **options)
            with redirect_stdout(io.StringIO()):
                execute(attempt)
            folder = attempt.parent.parent
            summary = read_json(folder/"termination.json")["termination"]
            self.assertEqual(summary["steps"], 2)
            self.assertEqual(summary["stopReason"], "sample_targets")
            self.assertTrue(summary["targetsMet"])
            item = inventory(root/"output")[0]
            self.assertEqual(item["termination"]["steps"], 2)
            self.assertAlmostEqual(item["progress"]["fraction"], .5)
            # This checkpoint precedes the second loading step: its count is one.
            resumed = prepare(config, checkpoint=folder/"dumps/checkpoint_0.xml.gz", **options)
            with redirect_stdout(io.StringIO()):
                execute(resumed)
            rows = read_macros(folder/"macroData.csv")
            self.assertEqual(len(rows), 3)  # Initialization plus two accepted steps.
            self.assertEqual(int(rows[-1]["completed_steps"]), 2)
            self.assertEqual(read_json(folder/"termination.json")["termination"]["steps"], 2)

if __name__ == "__main__":
    unittest.main()
