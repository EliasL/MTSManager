# Current simulator only

Write README files for first-time users with no access to chat history. Introduce
the project's purpose, prerequisites and terminology before giving examples.
Do not recount implementation decisions, completed requests, migrations or past
conversations. Keep the README concise and task-oriented; explain specialized
campaigns in their own documentation rather than assuming the reader knows them.

Only add high-value tests for plausible, consequential failures, such as corrupted
run identity, duplicate jobs, checkpoint mistakes, lost history or incorrect data.
Do not test trivial helpers, routine forwarding or every validation branch merely
to increase coverage. Small changes usually need no new test. Remove redundant
existing tests; add focused regressions when real bugs reveal a useful gap.

This is a standalone project for MTS2D `main`. Do not import legacy
SimulationScripts, accept old configuration names, or add historical reader
fallbacks. Raise errors for unexpected data. Prefer a small shared implementation
for local and remote operations.

- Keep every supported simulator setting explicit in saved configurations.
  Review `MTS_Management/defaults.json` against the C++ parser when its schema changes.
- Configuration identity excludes the generated name; executable and checkpoint
  identities are recorded separately. Never silently overwrite/resume another run.
- All launch paths must record lifecycle events in the job journal. Preserve
  per-attempt logs and readable live `[tags]`.
- Use dedicated current source/build/output directories on clusters. Never
  replace the shared legacy `~/simulation/MTS2D` checkout.
- Keep inventory bounded and cached. Do not introduce exact recursive directory
  sizes into routine status refreshes.
- Current VTUs use `T` for total deformation and `T_p` for plastic deformation.
  Use total `T` for physical fixed-coordinate stress, preserving its rotation.
- Energy functions return densities. Current macro `avg_energy` is mean energy
  per triangle; account for reference area when comparing the two.
- Use `python -m unittest discover -s tests -v`; real-simulator tests run when
  this project's isolated build exists. Do not submit production jobs for tests.
