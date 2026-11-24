# MTSManager automation API

Use the `mts` CLI from a shell or import the Python functions from an installed
`MTS_Management` package. The MTS GUI uses the same submission functions.
Both interfaces use the site profiles in `sites.toml` and the same native run
folders, configuration identities and journals.

## Inspect existing work

Run from the workspace root with the MTS environment active:

```sh
mts --sites MTSManager/sites.toml inventory-cache --site local
mts --sites MTSManager/sites.toml inventory --site local
```

The first command reads the saved snapshot. The second refreshes the inventory.
Use an enabled remote profile name in place of `local` to inspect that site.
`mts status` provides a human-readable table. For scripts, use `inventory` or
`inventory-cache` for JSON and use full configuration hashes for comparisons.

| Operation | CLI | Python |
| --- | --- | --- |
| Create a configuration | `mts config PATH --set rows=10 --set cols=10` | `MTS_Management.Config` and `Config.write` |
| Validate a saved campaign | `mts validate-campaign PATH` | `MTS_Management.campaign.campaign_entries` |
| Submit one run | `mts run PATH --site NAME` | `MTS_Management.submission.submit_run` |
| Submit a saved campaign | `mts campaign PATH --site NAME` | `MTS_Management.submission.submit_campaign` |
| Read or refresh inventory | `mts inventory-cache --site NAME` or `mts inventory --site NAME` | `MTS_Management.cluster.call(site, "inventory_cached")` or `call(site, "inventory")` |
| Retrieve a run collection | `mts fetch HASH DESTINATION --site NAME` | `MTS_Management.cluster.fetch_many` |
| Verify a local run folder | `mts inspect-run FOLDER` | `MTS_Management.run_records.read_run` |

The global `--sites` and `--state-dir` switches precede the subcommand.
`mts COMMAND --help` lists command-specific options. The Python functions accept
a `Site` from `load_sites`. The submission functions require a local
`state_dir` for receipts. Use `submission_state_dir` to select the same
default as the CLI.

`validate-campaign` reports the verified members and planned folder names
without registering the campaign or submitting jobs. `inspect-run` reports a
verified manifest and the effective settings for analysis. Both commands read
local files and return JSON.

For an interactive remote refresh, use
`call(site, "inventory_refresh", known_revision=revision)`. Pass the revision
of the last snapshot collected by the client, or omit it on the first call.
The response state is `started`, `running`, `ready` or `failed`. A ready
response includes a complete `snapshot` with `revision`, `rows` and
`updated_at` (Unix seconds). Call again to collect the snapshot after a scan
starts. A running scan reports its phase and, during run inspection, its
checked and total run counts. Polling is driven by the caller.

Pass `config_hashes` for a campaign selection, and keep each selection's
revision independently. Pass `hard_refresh=True` to reread finished records
and rebuild the index for a full-site selection. A failed response includes an
error and a revision identifying the failed attempt. Pass that revision as
`known_failure` to request a retry. `minimum_scan_start` optionally limits
collection to snapshots whose scan began at or after a Unix timestamp.

## Local output locations

Use `MTS_Management.local_output.plan_local_output(site_file, site, destination,
move=True)` to review a move. It returns the resolved source and destination,
active attempts with their checkpoint loads, and any blockers. Pass the reviewed
`source` and the approved `attempt` paths as `approved_attempts` to
`change_local_output(site_file, site, destination, move=True, ...)`. The manager
checks these identities again before signalling any process. The result contains
the effective `output_root`, newly submitted resume jobs in `resumed`, and any
per-run or move `errors`. Use `move=False, new_site=NAME` to create a separate
local output site. Reload the site profiles after either change.

## Submit explicitly

After reviewing the configuration and site, pass a validated `Config` to
`submit_run`. For a campaign, pass the path to its `simulations.json`. The
campaign submission uses companion `resources.json` and `metadata.json` when
present and installs the definition at the execution site.

```python
from pathlib import Path

from MTS_Management.cluster import load_sites
from MTS_Management.config import Config
from MTS_Management.submission import submission_state_dir, submit_run

site_file = Path("MTSManager/sites.toml")
sites = load_sites(site_file)
config = Config.read(Path("MTSManager/examples/smoke.conf"))
receipt = submit_run(sites["local"], config, campaign="Smoke",
                     state_dir=submission_state_dir(site_file, sites))
```

| Response | Fields |
| --- | --- |
| Single run | `config_hash` and `jobs` |
| Campaign | `campaign`, `jobs`, `skipped` and `conflicts` |
| Job | `attempt`, `attempt_id`, `output_folder`, `executable` and `build`, plus the foreground result, detached PID or Slurm ID |
| Campaign member | `label` and `config_hash`; conflicts also provide `conflict` text |

Each member with a submitted Slurm chain can have several jobs. Completed
members in `skipped` and members in `conflicts` receive no new attempt.
A job receipt identifies the attempt. Use inventory and `termination.json`
to inspect process state and scientific completion. A transport error can
leave a submission outcome unknown. Check the journal and site inventory
before retrying.

Successful `run` and `campaign` commands emit a JSON object on stdout.
Expected corrective failures exit with status 78 and write a message to
stderr. Set `MTS_EXPECTED_ERROR_FORMAT=json` to receive
`{"expected_error": "..."}` on stdout for those failures. Python callers
receive `MTS_Management.errors.ExpectedError`. Unexpected failures retain
diagnostics and a nonzero exit.

## Read saved results

`read_run(folder)` returns the configuration and manifest after checking the
folder name, configuration hash and recorded original settings.
`config.as_dict()` gives identity-bearing settings. Use `config[key]` for
the effective interpretation. `Config.read_saved(path)` reads a standalone
recorded configuration. `saved_campaign_entries(path)` reads a recorded
campaign for analysis. Use `Config.read` and `campaign_entries` for
submission. See the [simulation process](../docs/TheSimulationProcess.md) for
checkpoint recovery and run records.
