import unittest
import numpy as np

from mtscripts.energy import ContiEnergy, PieceWiseQuadratic, rotation, stretch_from_C
from mtscripts.reduction import lagrange_reduction


class EnergyTests(unittest.TestCase):
    def test_gradient_tangent_and_rotation(self):
        F = np.array([[1.03, .12], [.02, .98]])
        h = 1e-6
        P = ContiEnergy.P_from_F(F)
        A = ContiEnergy.elasticity_tensor(F, eulerian=False)
        for k, l in np.ndindex(2, 2):
            perturbation = np.zeros((2, 2))
            perturbation[k, l] = h
            derivative = (ContiEnergy.energy_from_F(F+perturbation)-
                          ContiEnergy.energy_from_F(F-perturbation))/(2*h)
            self.assertAlmostEqual(derivative, P[k, l], places=7)
            derivative_P = (ContiEnergy.P_from_F(F+perturbation)-
                            ContiEnergy.P_from_F(F-perturbation))/(2*h)
            np.testing.assert_allclose(derivative_P, A[:, :, k, l], atol=2e-7)
        R = rotation(.43)
        np.testing.assert_allclose(ContiEnergy.cauchy_from_F(R@F),
                                   R@ContiEnergy.cauchy_from_F(F)@R.T, atol=1e-10)

    def test_reduction_and_returned_metric(self):
        F = np.array([[1., 2.1], [0., 1.]])
        C = F.T@F
        before = C.copy()
        reduced, M = lagrange_reduction(C)
        np.testing.assert_allclose(C, before)
        np.testing.assert_allclose(reduced, M.T@C@M)
        energy, returned = ContiEnergy.energy_from_F(F, returnReducedC=True)
        np.testing.assert_allclose(returned, reduced)
        self.assertAlmostEqual(energy, ContiEnergy.energy_from_simple_shear(.1), places=12)
        self.assertAlmostEqual(energy, ContiEnergy.energy_from_simple_shear(2.1), places=12)
        with self.assertRaises(RuntimeError):
            lagrange_reduction(C, loops=1)

    def test_noise_reference_subclasses_and_invalid_inputs(self):
        raw = ContiEnergy.energy_from_reduced_C(np.eye(2), noise=1.2, zeroReference=False)
        self.assertAlmostEqual(ContiEnergy.energy_from_F(np.eye(2), noise=1.2),
                               raw-ContiEnergy.ground_state_energy())
        # Initializing one energy must not contaminate another subclass's caches.
        ContiEnergy.numeric_potential()
        F = np.array([[1., .1], [0., 1.]])
        self.assertAlmostEqual(PieceWiseQuadratic.energy_from_F(F), .00505, places=12)
        self.assertAlmostEqual(float(ContiEnergy.moduli_at_F(np.eye(2)).mu), 6.075757575757576)
        for F in (np.zeros((2, 2)), np.array([[-1., 0], [0, 1.]]), np.full((2, 2), np.nan)):
            with self.assertRaises(ValueError):
                ContiEnergy.cauchy_from_F(F)
        with self.assertRaises(ValueError):
            stretch_from_C(np.diag([-1., 1.]))


if __name__ == "__main__":
    unittest.main()
