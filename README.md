# MTSManager

MTSManager runs and tracks simulations produced by MTS2D, a C++ mesoscopic
tensorial simulator. It supports local execution, remote hosts over SSH, and
Slurm clusters. Each run keeps its configuration, executable identity, logs and
execution history together.

The Python package is named `MTS_Management`; its command-line tool is `mts`.

## Installation and a first run

Use Python 3.11 or newer and place the MTS2D source repository beside this one:

```text
MTS/
  MTS2D/
  MTSManager/
```

Run these commands from `MTSManager/`:

```sh
python3.11 -m venv ../.venv
source ../.venv/bin/activate
python -m pip install --config-settings editable_mode=compat -e '.[mesh]'
mts build --site local
mts run examples/smoke.conf --campaign Smoke
mts status overview --site local
```

The build requires CMake, a C++17 compiler, OpenMP and zlib. The simulator source
must use committed build inputs on `main` or a pinned commit verified to belong
to `origin/main` (as in a Git submodule). The supplied smoke
configuration runs a small system to check the installation.

Local paths are configured in [sites.toml](sites.toml). Its default local profile
uses `../MTS2D` as the source, `../MTS2D/build/release/MTS2D` as the executable,
and `output/` for results. The GUI, installer and managed builds share this Release executable.
If `executable` is omitted, it defaults to `build/release/MTS2D` inside the
configured `source` directory. An explicit path can select an existing deployment.
Paths in a local profile are relative to the site file. Recreate CMake
build directories and editable installations if you move the repositories.

Use `mts --help` or `mts COMMAND --help` for command options.
`python -m MTS_Management` is equivalent to `mts`.

## Configurations and parameter sweeps

A configuration describes one simulation. A campaign is a named collection of
configurations, often varying system size, random seed or solver settings.
Store each campaign in its own folder; a suffix such as `oct26` records the
launch month and year.

For example, create a size-and-seed sweep:

```sh
mkdir -p campaigns/size_scaling_oct26
mts config campaigns/size_scaling_oct26/base.conf --label Example \
  --set rows=10 --set cols=10 --set boundary=periodic --set totalLoad=0.01
mts make-campaign campaigns/size_scaling_oct26/simulations.json --name SizeScaling \
  --base campaigns/size_scaling_oct26/base.conf \
  --vary 'L=[10,20,40]' --vary 'seed=[0,1,2]'
mts campaign campaigns/size_scaling_oct26/simulations.json --site local --workers 2
```

`L` sets both `rows` and `cols`. `--workers` limits simultaneous local runs;
`nrThreads` controls threads within each simulation. Choose convergence tolerances
and stopping targets for your experiment before launching a large campaign.

Configurations save every supported simulator setting explicitly. Unknown keys,
incomplete files and invalid parameter combinations raise errors. The settings
and their defaults are listed in
[MTS_Management/defaults.json](MTS_Management/defaults.json).

Python scripts can use the same configuration and execution functions:

```python
from MTS_Management import Config, sweep, run

base = Config(rows=10, cols=10, boundary="periodic", totalLoad=0.01)
configs = sweep(base, L=[10, 20], seed=[0, 1])
run(configs[0], executable="../MTS2D/build/release/MTS2D", output_root="output",
    state_dir=".state", campaign="Example")
```

`MTS_Management.campaign.campaign_entries` reads and validates saved campaigns.

## Results and run identity

Each run has a readable label followed by a configuration hash:

```text
output/
  Example[configuration-hash]/
    config.conf
    manifest.json
    macroData.csv
    termination.json
    data/                         # Mesh output, when enabled
    dumps/                        # Checkpoints, when enabled
    attempts/<attempt-id>/
      request.json
      status.json
      stdout.log
```

The displayed hash contains the first 20 hexadecimal digits of a SHA-256 digest;
the full digest is recorded in the manifest. It includes all simulator settings,
but excludes labels and site paths. The executable and checkpoint have separate
identities, so changing either cannot silently replace an existing run.

Use `mts inventory --site local` to list runs. `mts find HASH` locates a run by
its displayed or full hash. Use a separate output root to repeat a completed run
or to compare different simulator builds. Renaming a managed run folder breaks
its saved identity checks.

Each output root has a shared `.inventory.sqlite3` database. Normal inventory
refreshes reread unfinished runs and reuse finished records without opening their
run files. Preparing a new attempt invalidates its cached record before the
attempt is created. This policy assumes that runs are restarted through
MTSManager. Concurrent refreshes are serialized; lifecycle revision checks
prevent a refresh from restoring a finished record invalidated by a restart.
Only the last successful full-update time is stored.

Read an existing database without scanning run files with
`mts inventory-cache --site cluster`. Invalidated records appear in its `pending`
list until refreshed. Use `mts inventory-update --site cluster` to refresh the
database and atomically export `.inventory.json`; its response gives the export
path, successful-update time and record counts. Transfer this JSON export with
rsync rather than copying a live SQLite database and its journal. Other clients
can read the shared database immediately, with its recorded update time.

`mts inventory --site cluster --hard-refresh` rebuilds the run index and rereads
all configurations, manifests, attempts and progress, including finished runs.
Use it after changes made outside MTSManager. Directory-size estimation remains
a separate `--refresh-sizes` option. These commands require the updated manager
on the execution host.

The desktop application's Update MTS on servers action transfers the manager,
campaign directories and verified simulator source. The shared package identity
is checked before recording profile locations in `.state/manager-deployments.json`.
Source compilation remains separate. Retained binaries and scientific output
are preserved.

Submission records and `.state/job-log.jsonl` identify requests, accepted jobs
and administrative actions. Each attempt saves the original command, environment
and script. Direct jobs use those files without importing the manager at runtime.
Scheduler/process records, attempt logs and scientific termination summaries
provide execution outcomes. The October 2026 benchmark retains its historical
worker commands and installation.

## Remote execution

Remote execution requires OpenSSH, rsync and Python 3.11+ on the remote host.
Configure an SSH alias in `~/.ssh/config`, then add a matching site profile to
`sites.toml`. For example:

```toml
[sites.cluster]
ssh = "my-cluster"
directory = "~/simulation/MTS"
source = "MTS2D"
executable = "build/current/MTS2D"
output_root = "~/MTS2D_output"
mode = "slurm"
native_arch = false

[sites.cluster.slurm]
time = "01:00:00"
mem = "4G"
```

Replace the alias, paths and scheduler resources with values for your cluster.
Use `mode = "detached"` on hosts without Slurm. A site's `setup` string can load
compiler and scheduler modules; it must leave standard output quiet. Set
`native_arch = false` when build and execution machines may have different CPUs.
The standard remote workspace is `~/simulation/MTS`, containing one active
`MTS2D` source tree. Source uploads verify the transferred snapshot and replace
its tracked build inputs in place. Builds and source installation share a lock.
Existing `~/simulation/MTS2D` and SimulationScripts installations remain separate.

Every prepared job uses a retained copy of its executable and original build
manifest. Working binaries are also retained before and after rebuilding.
Retained copies are stored under `executables/<build-identity>/MTS2D` and cannot
be rebuilt in place. Set a profile's `executable` to a retained path to select an
older compiled version; checkpoint and configuration checks still apply.

Retention applies when each run is submitted. Folder-interface builds execute
through a saved shell command pointing directly to the retained binary and run
folder. MTS2D owns checkpoint selection and the run lock. Older saved worker
commands remain supported for the October 2026 benchmark. Resume preparation
uses the binary recorded for that run, even if the site's working build changes.
Campaign `metadata.json` records binary paths and local archival copies.

Builds are compiled on the execution host; local binaries and CMake caches are
not uploaded. The supplied remote profiles describe specific installations and
need reviewing before use. `management_module` must match the module installed on that host;
new deployments use `MTS_Management`.
Configurations explicitly record `tangentBackend`; the current default is
`cholmod`, with `eigen` available in CHOLMOD-enabled builds. Configuration identity
includes this choice. Saved campaign plans retain their recorded identities for
monitoring; absent settings in existing runs are interpreted using campaign metadata without changing their hashes.

After configuring a site:

```sh
mts deploy --site cluster --source ../MTS2D
mts build --site cluster
mts run examples/smoke.conf --site cluster --campaign Smoke
mts status jobs --site cluster --watch
```

Use dedicated deployment and source directories. Keep the
configuration and retained executable available while jobs are queued or running. Queued and
detached jobs continue after the client disconnects. Scheduler state and worker
state are shown separately; a missing worker is not reported as a successful run.

## Logs, stopping and continuation

A run command prints its attempt directory. Substitute that path in commands such
as:

```sh
mts logs /absolute/path/to/run/attempts/ATTEMPT_ID --site cluster --follow
mts stop /absolute/path/to/run/attempts/ATTEMPT_ID --site cluster
mts run original.conf --site cluster --checkpoint latest
```

Continuation requires the original configuration and executable and a checkpoint
inside the managed run folder. `latest` requests simulator-owned checkpoint discovery for folder-interface builds; historical benchmark workers retain their recorded selection path.
To queue several sequential Slurm allocations, use `--chain N`; this requires
`writeDumps=true`. Periodic `checkpointInterval` saves limit work lost to timeouts.
Missing checkpoints for existing progress raise errors rather than restarting
from scratch.

Use `mts sync-log --site cluster` to merge remote history locally and `mts history`
to read it. If a submission connection fails, inspect history and scheduler state
before retrying: the job may already have been accepted.

## Downloading and reading data

Replace `HASH` below with a hash from `mts inventory --site cluster`:

```sh
mts fetch HASH ./download --site cluster
```

The default download includes configuration, manifest, macro CSV and termination
summary when present. Add `--kind full` to include meshes, dumps and logs.
`MTS_Management.cluster.fetch_many` downloads multiple configurations using one
inventory request and one rsync transfer. Both interfaces check destination
identities and record a `snapshot.json` receipt beside downloaded runs.

`MTS_Management.data.read_macros` reads complete macro tables and preserves extra
named columns. `read_vtu` reads mesh output; deformation fields `T` and `T_p`
represent total and plastic deformation. Incomplete or inconsistent data raise
errors. Physical fixed-coordinate stress requires total `T`, including rotation.

For storage estimates, run `mts status data --site cluster --refresh-sizes`.
Estimates use bounded sampling; partial traversals are labelled explicitly.

## Plotting and examples

[MTSPlot](../MTSPlot/README.md) provides response plots, timing comparisons and
model mathematics. For an example of a larger performance campaign, see
[campaigns/magi_benchmark_oct26](campaigns/magi_benchmark_oct26/README.md).

## Development checks

```sh
python -m unittest discover -s tests -v
```

The suite covers run identity, execution history, process control, scheduler
continuation and data integrity. Small real-simulator checks run when the isolated
`../MTS2D/build/release/MTS2D` executable is available. Scheduler tests do not
submit cluster jobs.


Campaign directories contain `simulations.json` (original labelled settings),
`resources.json` (submission resources) and editable `metadata.json` (missing
settings and binary references). Supplements fill only absent keys. They do not
change the original configuration or its hash. Update MTS on servers transfers
manager code, campaign files and a verified MTS2D source snapshot; compilation
is a separate operation. Scientific output and retained binaries are preserved.
Each attempt saves its exact command, environment and script under `attempts/`.
