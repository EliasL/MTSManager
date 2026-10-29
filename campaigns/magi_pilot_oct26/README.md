# MTS2D execution pilot on MAGI, October 2026

This small campaign checks simulation execution and CPU affinity on the MAGI
computing cluster before running larger performance studies. `simulations.json`
contains eight configurations at square size 50, varying 1/2 threads,
none/exactTangent preconditioning and none/edgeFlip reconnection with seed 0.

`chain-smoke.conf` provides a checkpoint-enabled configuration for checking
continuation across sequential Slurm allocations. Configure the `magi-pilot` site
in [sites.toml](../../sites.toml) before using these inputs. See the
[manager guide](../../README.md) for campaign submission and checkpoint commands.
