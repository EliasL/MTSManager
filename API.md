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
the effective interpretation. Analysis and retrieval can use
`allow_legacy_vtu=True` for native MTS runs recorded before the snapshot rate
was introduced. Use normal configuration validation when preparing a fresh
execution. See the [simulation process](../docs/TheSimulationProcess.md) for
checkpoint recovery and run records.
