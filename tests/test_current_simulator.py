"""Optional real-simulator smoke tests after `mts build` in this checkout."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np

from mtscripts.config import Config
from mtscripts.data import read_macros
from mtscripts.energy import ContiEnergy
from mtscripts.files import read_json
from mtscripts.inventory import inventory
from mtscripts.provenance import executable_identity, verify_schema
from mtscripts.runner import PACKAGE_ROOT, execute, prepare

EXE = PACKAGE_ROOT / "build/MTS2D"
SOURCE = PACKAGE_ROOT.parent / "MTS2D"


@unittest.skipUnless(EXE.exists() and (EXE.parent/"build-manifest.json").exists(),
                     "Build the current MTS2D executable to run integration smoke tests")
class CurrentSimulatorTests(unittest.TestCase):
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

    def test_real_run_checkpoint_and_csv(self):
        executable_identity(EXE)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = Config(rows=4, cols=4, initialGuessNoise=0, boundary="periodic",
                            totalLoad=.003, loadIncrement=.001, checkpointInterval=1,
                            nrVTUFrames=2, loading="noRelaxation")
            attempt = prepare(config, executable=EXE, output_root=root/"output", state_dir=root/"state",
                              campaign="IntegrationSmoke", label="SmokeL4s0")
            with redirect_stdout(io.StringIO()):
                execute(attempt)
            self.assertEqual(read_json(attempt/"status.json")["state"], "finished")
            folder = attempt.parent.parent
            rows = read_macros(folder/"macroData.csv")
            self.assertAlmostEqual(float(rows[-1]["load"]), .003)
            for row in rows:
                gamma = float(row["load"])
                energy = ContiEnergy.energy_from_simple_shear(gamma)
                # MTS2D stores mean energy per triangle; this mesh has area 1/2.
                self.assertAlmostEqual(float(row["avg_energy"]), .5*energy, places=9)
            checkpoint = sorted((folder/"dumps").glob("*.xml.gz"))[-1]
            resumed = prepare(config, executable=EXE, output_root=root/"output", state_dir=root/"state",
                              checkpoint=checkpoint, campaign="IntegrationSmoke")
            with redirect_stdout(io.StringIO()):
                execute(resumed)
            self.assertEqual(read_json(resumed/"status.json")["state"], "finished")
            self.assertEqual(inventory(root/"output")[0]["progress"]["fraction"], 1)

    @unittest.skipUnless(SOURCE.exists(), "Current simulator source is not beside this checkout")
    def test_cpp_energy_formula_and_schema(self):
        verify_schema(SOURCE)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/"reference.cpp"
            source.write_text('''#include "Simulation/energyFunctions.h"
#include <iostream>
#include <iomanip>
int main() {
  std::cout << std::setprecision(17);
  for (double c12 : {0.0, 0.1, 0.3}) {
    std::cout << ContiPotential::energyDensityFormula(1.1, 1.03, c12, -0.25, 4.0, 1.2) << "\\n";
  }
}
''')
            subprocess.run(["c++", "-std=c++17", "-I", str(SOURCE/"src"),
                            "-I", str(SOURCE/"libs/eigen"), str(source), "-o", str(root/"reference")], check=True)
            actual = np.fromstring(subprocess.check_output([str(root/"reference")], text=True), sep="\n")
            expected = [ContiEnergy.energy_from_reduced_C_components(1.1, 1.03, c12,
                        noise=1.2, zeroReference=False) for c12 in (0, .1, .3)]
            np.testing.assert_allclose(actual, expected, rtol=1e-14)


if __name__ == "__main__":
    unittest.main()
