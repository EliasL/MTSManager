# MTS2D performance study on MAGI, October 2026

This campaign measures how MTS2D minimization costs vary with system size, CPU
thread count, preconditioning and mesh reconnection. MAGI is the computing cluster
used to run the study; its connection and scheduler settings are in the `magi`
profile of [sites.toml](../../sites.toml).

The saved matrix contains 2,000 timing configurations: square sizes
50/100/200/400/800, 1/2/4/8/16 threads, none/exactTangent preconditioning,
none/edgeFlip reconnection, and seeds 0–19. One additional run records detailed
minimizer diagnostics and is excluded from timing comparisons.

Runs begin at load 0.15 with increments of 1e-5 and stop after 20 plastic steps,
with a load cap of 1.0. The force tolerance is epsR=1e-6; competing LBFGS/CG
stopping tolerances are 1e-20. Initial relaxation is single-threaded and should be
reported separately from loading-step timings. Routine mesh output and
checkpoints are disabled for the timing runs.

## Files

- `simulations.json`: full simulation configurations and labels.
- `resources.json`: campaign checksum, simulator revision and resource requests.
- `base.conf`: base simulation settings.
- `launch.py`: submission controller with persistent receipts for interrupted submissions.

The resource plan requests 4 GiB for sizes 50/100, 8 GiB for 200, 16 GiB for 400,
and 56 GiB for 800. Runs use Slurm's COMPUTE partition with core placement and
close binding. The diagnostic run is submitted first.

## Validate and submit

From MTSManager/, validate the configuration and resource plan without submitting:

```sh
python -m campaigns.magi_benchmark_oct26.launch \
  campaigns/magi_benchmark_oct26/simulations.json --dry-run
```

To submit, use a dedicated cluster deployment with the specified simulator build
and a configured native Slurm profile. From that deployment's MTSManager directory:

```sh
python -u -m campaigns.magi_benchmark_oct26.launch \
  campaigns/magi_benchmark_oct26/simulations.json --site local
```

Submission receipts are stored under `.state/MAGI-Benchmark-20261023/`. After an
interruption, inspect these receipts and the scheduler, then use `--resume` to
reuse confirmed submissions. Uncertain submissions require investigation rather
than automatic resubmission. Keep deployed code fixed while jobs are active.

For downloading completed results and producing timing figures, see the
[MTSPlot campaign example](../../../MTSPlot/README.md#example-an-overview-of-a-performance-campaign).
