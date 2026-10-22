"""Vectorized 2D Lagrange reduction with explicit convergence errors."""

import numpy as np


def lagrange_reduction_components(C11, C22, C12, loops=1000):
    a, b, c = (np.array(x, dtype=float, copy=True) for x in (C11, C22, C12))
    if a.shape != b.shape or a.shape != c.shape:
        raise ValueError("Metric components must have equal shapes")
    if not np.all(np.isfinite([a, b, c])) or np.any(a <= 0) or np.any(a*b-c*c <= 0):
        raise ValueError("Expected finite positive-definite metrics")
    M = np.broadcast_to(np.eye(2), a.shape + (2, 2)).copy()
    flip = np.array([[1, 0], [0, -1]])
    swap = np.array([[0, 1], [1, 0]])
    shear = np.array([[1, -1], [0, 1]])
    for _ in range(loops):
        negative = c < 0
        c[negative] *= -1
        M[negative] = M[negative] @ flip
        exchange = b < a
        a[exchange], b[exchange] = b[exchange].copy(), a[exchange].copy()
        M[exchange] = M[exchange] @ swap
        shift = 2*c > a
        if not np.any(negative | exchange | shift):
            return a, b, c, M
        b[shift] += a[shift] - 2*c[shift]
        c[shift] -= a[shift]
        M[shift] = M[shift] @ shear
    raise RuntimeError(f"Lagrange reduction did not converge in {loops} iterations")


def lagrange_reduction(C, loops=1000):
    C = np.asarray(C, dtype=float)
    if C.shape[-2:] != (2, 2) or not np.allclose(C, C.swapaxes(-1, -2)):
        raise ValueError("Expected symmetric (..., 2, 2) matrices")
    a, b, c, M = lagrange_reduction_components(C[..., 0, 0], C[..., 1, 1], C[..., 0, 1], loops)
    result = C.copy()
    result[..., 0, 0], result[..., 1, 1] = a, b
    result[..., 0, 1] = result[..., 1, 0] = c
    return result, M
