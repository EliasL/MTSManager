# MTSManager

A standalone manager for **MTS2D**. A helper package for submitting and managing simulations.

The core uses Python **3.11+**, OpenSSH, and rsync.

## Start here

From this directory, use a Python 3.11+ environment:

```sh
python -m pip install -e '.[energy,mesh]'
python -m mtscripts --help
```

Installation is optional for the management commands: `python -m mtscripts`
works directly from this directory with the standard library alone.
Use a Python environment with the optional dependencies installed to run the
complete test suite. There is no runtime dependency on the legacy project.

`sites.toml` is the version-controlled configuration for local execution and
all cluster profiles. Each remote site uses its SSH alias from `~/.ssh/config`;
keep credentials and private keys there, outside this repository. Keep
site-specific paths, interpreters, scheduler resources, and module setup out of
simulation configurations. Verify the Schwartz and Jean Zay profiles before use.

```sh
# Build main once into this project's isolated build directory.
python -m mtscripts build --site local

# Run a tiny supplied configuration with live [SmokeL4s0:HASH] output.
python -m mtscripts run examples/smoke.conf --campaign Smoke --label SmokeL4s0

# A single live overview in any terminal; Ctrl-C exits.
python status.py
# Or choose a view/site, without watching:
python -m mtscripts status overview --site local
python -m mtscripts status jobs --site pascal --watch
python -m mtscripts status data --site local --refresh-sizes
```

`status.py` runs in the terminal where you invoke it. Separate views are
`servers`, `jobs`, `data`, and `history`. The default overview combines the first
three; it refreshes every 15 seconds and reports unavailable hosts explicitly.
CPU/load/RAM describe the queried host (the login host on a Slurm system),
not an estimate of the entire cluster's free compute capacity.

## Configurations and campaigns

```sh
python -m mtscripts config small.conf --label Example \
  --set rows=10 --set cols=10 --set boundary=periodic --set totalLoad=0.01

python -m mtscripts make-campaign campaign.json --name SizeScaling \
  --base small.conf --vary 'L=[100,150,200]' --vary 'seed=[0,1,2]'

# Local concurrency is explicit and bounded; builds are not repeated per run.
python -m mtscripts campaign campaign.json --site local --workers 2
python -m mtscripts campaign campaign.json --site pascal
```

The supplied defaults reproduce the simulator's configuration defaults; they
are not a recommended production/scientific protocol. Inspect and choose your
settings before launching a campaign. `L` is a sweep convenience setting both
`rows` and `cols`; it is never written as an MTS2D parameter.

Python also exposes `Config`, `sweep`, and `run`:

```python
from mtscripts import Config, sweep, run

base = Config(rows=10, cols=10, boundary="periodic", totalLoad=0.01)
configs = sweep(base, L=[100, 200], seed=[0, 1])
run(base, executable="build/MTS2D", output_root="output", state_dir=".state",
    campaign="Example", label="ExampleL10s0")
```

Every generated configuration writes all 73 supported settings explicitly,
including values equal to defaults. Saved configs and campaigns must be complete.
Unknown keys, duplicate config keys, invalid types, nonfinite parameters, and
invalid protocol combinations raise errors. Old aliases are not accepted.
`defaults.json` is the reviewed schema/default snapshot. Build preparation
checks the current C++ parser's key set and fails if it changes. Updating the
simulator's schema requires an intentional update here; no runtime C++ parsing
is used to invent missing settings.

## Identity and output layout

For the MAGI benchmark, use `epsR=1e-6`; keep the competing LBFGS/CG tolerances
at `1e-20` rather than setting every ALGLIB stopping option to zero (which
activates its automatic EpsX default). FIRE's former `FIREEpsx` displacement-step criterion has been removed.
FIRE's `epsR` bounds the largest absolute
force component, whereas `eps` bounds the L2 force norm and `epsRel` bounds
that norm relative to the displacement norm.

Current MTS2D initialization uses a common single-threaded, unpreconditioned
LBFGS relaxation: `epsR=1e-6`, 10 corrections, competing tolerances `1e-20`,
and no iteration limit. The requested settings are restored before saving the
initial output/checkpoint. Physical settings, seed, noise and reconnection
choice still define the initial problem. Report initialization separately
from the subsequent loading steps in benchmark timings.

```text
output/
  SizeScalingL100s2[8f3a19c7d205e64b91ac]/
    config.conf
    manifest.json
    macroData.csv
    data/...
    dumps/...
    attempts/<attempt-id>/
      request.json
      status.json
      stdout.log
      launcher.log       # detached/Slurm jobs
      submit.sh          # Slurm jobs
```

The brackets contain the first 20 hex digits (80 bits) of SHA-256. The full
digest is stored and checked. Hash input is a versioned, sorted, compact ASCII
JSON representation of **all typed settings**, excluding only the generated
`name`. The algorithm is documented Python serialization, not RFC 8785.
Numeric floats normalize according to the schema; reordering keys or comments
does not change identity. Labels/campaigns, hosts, paths, and queue resources
do not enter the configuration hash. All simulator settings, including logging
and thread settings, do enter it.

A separate run identity includes the executable checksum and checkpoint checksum
when resuming. Every execution attempt has a UUID. The build record contains the
main-branch revision, source digest, executable checksum, build options, and CMake
cache checksum. Each attempt records the scripts checksum, exact command, host,
site, campaign, input checksum, timing, process/scheduler identifiers and outcome.
The initial simulator config is immutable; no automatic checkpoint selection,
settings overrides, or executable replacement is performed.

An indexed lookup uses only the hash, so human labels are not needed to find a run:

```sh
python -m mtscripts find 8f3a19c7d205e64b91ac
python -m mtscripts inventory --site pascal --hash 8f3a19c7d205e64b91ac
python -m mtscripts reindex --site pascal
```

Use a complete 20-character displayed hash or the full hash. `reindex` rebuilds
the disposable index from manifests after copying managed run folders. Conflicting
configurations, ambiguous hashes, renamed directories whose saved `name` differs,
and duplicate configuration folders raise errors.

The same configuration with a different executable must use a different output
root, preventing accidental mixture of simulator revisions. After an actual run,
an existing output can only be resumed explicitly; a fresh rerun uses a separate
output root. A failed/cancelled submission that never started can be retried.
Live tags include an eight-character hash suffix so configurations with the
same readable label can still be distinguished.

## Cluster workflow

`native_arch` is a boolean site setting, defaulting to `false`. Every build
explicitly passes `ENABLE_NATIVE_ARCH=ON` or `OFF` to CMake, overriding an old
cache value. The local profile enables it; leave it disabled on MAGI, where
the frontend and compute nodes may support different CPU instructions. The
selected value is recorded in the build manifest's `build_options`.

OpenSSH reads hostnames, users, ports, keys, and jump hosts from `~/.ssh/config`.
The site profiles add execution paths, build settings, and scheduler resources;
there is no separate Python server registry or legacy connection dependency.

```sh
# Upload this package and an isolated, committed MTS2D main source snapshot.
python -m mtscripts deploy --site pascal --source ../MTS2D
python -m mtscripts info --site pascal
python -m mtscripts build --site pascal
python -m mtscripts run examples/smoke.conf --site pascal --campaign Smoke
```

The remote package runs locally on that host and returns structured JSON over
SSH. Build output goes to stderr. No remote Python management dependencies need
installing. Configure a compiler/module environment with the site's `setup`
string if required; it must leave stdout quiet. Use a new source snapshot path
for each MTS2D revision. Deployment refuses the shared `~/simulation/MTS2D`
path and refuses to overwrite unmanaged or different-version source snapshots.
It uses rsync without deletion and excludes builds, environments, state and data.
Deployment creates a native `sites.toml` if absent, so after logging into the
cluster you can use the same commands there with `--site local`. It preserves
an existing native site file; update it deliberately if paths/settings change.
The journal's host field always identifies the actual machine even when the
site alias is `local`.
Changing scripts while work is queued causes an explicit checksum error at
execution; finish pending work before deploying a changed package.

Slurm jobs record the scheduler ID and retain their submission script. CPU
allocation is checked against `nrThreads`. The worker is the same tagged Python
runner used locally. Site mode `detached` runs on an ordinary SSH host; local
mode `foreground` streams directly. You can explicitly select `--mode detached`
or `--mode slurm`. Detached/queued work survives the initiating SSH connection.

The run/submit command prints its **attempt directory**. Use that path on the
execution host for inspection and control:

```sh
python -m mtscripts logs '/absolute/run/attempts/ID' --site pascal --follow
python -m mtscripts stop '/absolute/run/attempts/ID' --site pascal
python -m mtscripts run original.conf --site pascal \
  --checkpoint '/absolute/run/dumps/checkpoint.xml.gz'
```

Resume requires an unchanged configuration, the original executable, and a
checkpoint inside that managed output folder. The saved input is checksummed
again immediately before execution. Stop uses `scancel` or a verified process
identity, never broad name matching.

Slurm queue state and worker state are displayed separately. A hard-killed worker
or unreachable host cannot reliably record its final event: the overview shows
a missing process/queue entry rather than declaring success. Inspect its logs
and scheduler accounting; `stop` can clear a stale active attempt. No automatic
resubmission or silent failure recovery runs in the background.

## The job log

**`.state/job-log.jsonl` is the append-only job log.** Each line is a standalone
JSON record with a unique event ID and ISO date/time including UTC offset.
Requests, queued/submitted jobs, actual starts, finishes, failures, and stops are
recorded. Entries carry configuration/run/attempt identities, campaign, site,
host, output location and relevant process/scheduler identifiers.

Each execution host keeps its own journal even if the client disconnects. The
local journal also records submission attempts and uncertain network outcomes.
Never blindly retry after a submission connection error: the remote request may
already have succeeded. Duplicate active attempts are rejected.

```sh
python -m mtscripts sync-log
python -m mtscripts history
python -m mtscripts status history
```

`status jobs` and `status overview` automatically merge new events from selected
hosts. Byte cursors avoid transferring old records; event IDs prevent duplicate
imports. Cursors advance only after a durable merge. Advisory file locks and
fsync protect concurrent writers. Keep the journal and manifests with your data;
they are not disposable caches. Truncated/malformed history is an error, not
silently repaired. There is no always-running synchronization daemon.

## Data and fast storage estimates

```sh
python -m mtscripts inventory --site pascal --refresh-sizes
python -m mtscripts fetch 8f3a19c7d205e64b91ac ./download --site pascal
python -m mtscripts fetch 8f3a19c7d205e64b91ac ./download --site pascal --kind full
```

The default fetch copies `macroData.csv`, `termination.json` when present,
and its configuration and manifest
into a directory bearing the original hashed run name; full downloads are
explicit. An existing download must have the exact same run manifest.
Progress reads at most a header and 64 KiB from the CSV tail, ignoring the last
row while it is being written. Current MTS2D uses an increasing path coordinate,
so progress is `(load - startLoad) / totalLoad`, including cyclic/directional
protocols. A successful early-stop protocol may finish below 100%; the worker's
outcome remains separate from that loading-budget fraction.

Ordinary status refreshes use cached sizes and never run recursive `du`.
`--refresh-sizes` estimates file bytes using counts and up to five file-size
samples per directory/type, including nested outputs. Each run traversal is
limited to 10,000 entries and a 0.2-second budget, checked between filesystem
calls. A blocked filesystem call itself cannot be preempted by that budget.
Symlinks are not followed. Truncated scans are labelled **partial traversal**;
their estimate covers only visited files and must not be interpreted as a total
or lower confidence bound. These are logical file sizes, not allocated disk space.

`mtscripts.data.read_macros` reads complete current CSVs. `read_vtu` provides
`MeshData.T` (total) and `MeshData.T_p` (plastic), using the exact current fields.
VTUs saved at minimal field levels may lack matrices and raise a missing-field
error. There are no aliases for historical VTUs or reconstructed rotations.

## Energy mathematics

`mtscripts.energy` retains symbolic/numeric potentials, derivatives, stress,
forces, tangents, acoustic tensors, stability, elastic moduli and shear helpers.
`mtscripts.reduction` contains only the needed Lagrange reduction. No plotting
libraries are imported. Positive determinants, finite metrics and reduction
convergence are checked explicitly. Energy subclasses have independent caches.
Zero-reference energies subtract the identity/noise=1 density, matching the
current simulator; CSV `avg_energy` is energy per element, not energy density.

Use the **full total `T`** as the deformation argument for reconnecting data.
`stretch_from_C` returns the symmetric stretch, not a physical deformation with
its lost rotation restored. Methods using only C for fixed-coordinate stress
represent that stretch, as documented in the module. `energy_from_simple_shear`
now applies reduction, and the `energy_from_C` methods do not mutate their inputs.

## Verification and moving the project

```sh
python -m unittest discover -s tests -v
```

Tests cover deterministic identity, strict configs, tagged streams, journals,
duplicate guards, batch execution, detached stop, Slurm startup races/failures,
bounded estimates, current CSV reading and energy derivatives. If the isolated
`build/MTS2D` exists, real-run/checkpoint smoke tests run as well. With the current
source alongside this checkout, a compiled C++ formula check also runs. Scheduler
tests use fixtures and do not submit production jobs.

When moving this into its own project, keep the package, examples and tests;
adjust `sites.toml` (particularly the local MTS2D source path). Recreate any virtual
environment and build in a fresh build directory because CMake caches contain
absolute paths. Move real outputs and the journal deliberately: manifests and
execution records preserve their original absolute paths. The legacy project
does not need to be importable or installed.

## MAGI pilot and affinity

`sites.magi` defaults to `OMP_PLACES=cores`, `OMP_PROC_BIND=close` and
`OMP_DYNAMIC=FALSE`. The worker sets `OMP_NUM_THREADS` from `nrThreads`.
The MAGI CPU reservation is `slurm_cpus_per_thread=2` times `nrThreads`,
accounting for the two hardware threads per physical core in COMPUTE.
Site environment values are saved in each request; effective OpenMP settings
and the allowed Linux CPU/core topology are saved with the running event.
If core placement is requested but fewer physical cores than workers are
available, launch fails before starting the simulator. Pilot logs also include
OpenMP's actual thread-affinity diagnostics. CPU IDs are never hardcoded.

`campaigns/magi-pilot-L50.json` contains eight seed-0 runs: 1/2 workers,
none/exactTangent curvature, and none/edgeFlip reconnection. They start at
load 0.15 with initial noise 0.05, `epsR=1e-6`, and increment `1e-5`.
They stop at 20 plastic steps or 20 completed loading steps (`stopWhen=any`),
excluding initialization; the strain budget is 0.001 as an additional cap.
Routine VTUs, dumps and detailed minimizer logging are disabled.
