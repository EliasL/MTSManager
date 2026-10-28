"""The CLI and the remote JSON endpoint share the same Python operations."""

import argparse
from contextlib import redirect_stdout
from dataclasses import replace
import json
from pathlib import Path
import sys

from .cluster import across, call, deploy, fetch, load_sites, paths, serve, submit, sync_journals
from .campaign import campaign_entries
from .config import Config, sweep
from .files import read_json, write_json
from .journal import Journal
from .runner import execute, launch, prepare, run_many
from .status import render, watch


def assignment(text):
    key, value = text.split("=", 1)
    try:
        value = json.loads(value)
    except json.JSONDecodeError:
        pass
    return key, value


def assignments(texts):
    result = {}
    for text in texts:
        key, value = assignment(text)
        if key in result:
            raise ValueError(f"Repeated parameter: {key}")
        result[key] = value
    return result


def parser():
    p = argparse.ArgumentParser(description="Current-format MTS2D simulations")
    p.add_argument("--sites", type=Path, default=Path.cwd() / "sites.toml")
    p.add_argument("--state-dir", type=Path, help="Local merged journal directory")
    commands = p.add_subparsers(dest="command", required=True)
    c = commands.add_parser("config", help="Write a complete hashed configuration")
    c.add_argument("path", type=Path)
    c.add_argument("--set", action="append", default=[])
    c.add_argument("--label")
    c = commands.add_parser("make-campaign", help="Write a small explicit parameter sweep")
    c.add_argument("path", type=Path)
    c.add_argument("--name", required=True)
    c.add_argument("--base", type=Path, required=True)
    c.add_argument("--vary", action="append", required=True, help="e.g. 'seed=[0,1,2]'")
    for name in ("build", "deploy", "inventory", "inventory-cache", "inventory-update", "reindex", "info"):
        c = commands.add_parser(name)
        c.add_argument("--site", default="local")
        if name == "build":
            c.add_argument("--jobs", type=int, default=2)
        elif name == "deploy":
            c.add_argument("--source", type=Path, help="Also upload a dedicated current MTS2D snapshot")
        elif name in {"inventory", "inventory-update"}:
            if name == "inventory":
                c.add_argument("--hash")
            c.add_argument("--refresh-sizes", action="store_true")
            c.add_argument("--hard-refresh", action="store_true",
                           help="Rebuild the index and reread completed runs")
    for name in ("run", "campaign"):
        c = commands.add_parser(name)
        c.add_argument("path", type=Path)
        c.add_argument("--site", default="local")
        c.add_argument("--mode", choices=["foreground", "detached", "slurm"])
        c.add_argument("--chain", type=int, default=1,
                       help="Total sequential Slurm jobs per run; later jobs resume the latest dump")
        if name == "run":
            c.add_argument("--campaign", default="AdHoc")
            c.add_argument("--label")
            c.add_argument("--checkpoint", help="Checkpoint path on the execution host, or 'latest'")
        else:
            c.add_argument("--workers", type=int, default=2)
    for name in ("stop", "logs"):
        c = commands.add_parser(name)
        c.add_argument("attempt", help="Attempt directory on the execution host")
        c.add_argument("--site", default="local")
        if name == "logs":
            c.add_argument("--follow", action="store_true")
    c = commands.add_parser("fetch")
    c.add_argument("hash")
    c.add_argument("destination", type=Path)
    c.add_argument("--site", required=True)
    c.add_argument("--kind", choices=["macro", "full"], default="macro")
    c = commands.add_parser("sync-log")
    c.add_argument("--site", action="append")
    c = commands.add_parser("find", help="Find a configuration across configured hosts")
    c.add_argument("hash")
    c.add_argument("--site", action="append")
    commands.add_parser("history")
    c = commands.add_parser("status")
    c.add_argument("view", choices=["overview", "servers", "jobs", "data", "history"])
    c.add_argument("--site", action="append")
    c.add_argument("--watch", action="store_true")
    c.add_argument("--interval", type=float, default=15)
    c.add_argument("--refresh-sizes", action="store_true")
    commands.add_parser("_serve", help=argparse.SUPPRESS)
    c = commands.add_parser("_execute", help=argparse.SUPPRESS)
    c.add_argument("attempt")
    return p



def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "_serve":
        payload = json.load(sys.stdin)
        if "reader_timeout" in payload:
            import signal
            signal.alarm(payload.pop("reader_timeout"))
        # Builds and third-party helpers may print; only the response is stdout.
        with redirect_stdout(sys.stderr):
            result = serve(payload)
        print(json.dumps(result, allow_nan=False))
        return
    if args.command == "_execute":
        execute(args.attempt)
        return
    if args.command == "config":
        if args.path.exists():
            raise FileExistsError(args.path)
        config = Config(**assignments(args.set))
        config.write(args.path, args.label)
        print(config.folder_name(args.label))
        return
    if args.command == "make-campaign":
        if args.path.exists():
            raise FileExistsError(args.path)
        axes = assignments(args.vary)
        if any(not isinstance(v, list) or not v for v in axes.values()):
            raise ValueError("Each --vary value must be a nonempty JSON array")
        configs = sweep(Config.read(args.base), **axes)
        # Include rectangular dimensions too; identity always uses every setting.
        entries = [{"label": f"{args.name}L{c['rows']}" +
                   (f"x{c['cols']}" if c['rows'] != c['cols'] else "") + f"s{c['seed']}",
                    "settings": c.as_dict()} for c in configs]
        for c, entry in zip(configs, entries):
            c.folder_name(entry["label"])
        write_json(args.path, {"name": args.name, "runs": entries})
        print(f"Wrote {len(entries)} configurations to {args.path}")
        return
    sites = load_sites(args.sites)
    state_dir = (args.state_dir.resolve() if args.state_dir else
                 Path(paths(sites["local"])["state_dir"]) if "local" in sites else
                 args.sites.resolve().parent / ".state")
    if args.command in {"status", "sync-log", "find"}:
        names = args.site or list(sites)
        selected = [sites[name] for name in names]
        if args.command == "find":
            print(json.dumps(across(selected, "inventory", config_hash=args.hash), indent=2))
        elif args.command == "sync-log":
            result = sync_journals(selected, state_dir)
            print(json.dumps({name: {"events": len(r["data"]["records"])} if "data" in r else r
                              for name, r in result.items()}, indent=2))
        elif args.watch:
            watch(selected, args.view, interval=args.interval,
                  refresh_sizes=args.refresh_sizes, state_dir=state_dir)
        else:
            print(render(selected, args.view, refresh_sizes=args.refresh_sizes, state_dir=state_dir))
        return
    if args.command == "history":
        for record in Journal(state_dir).records():
            print(json.dumps(record))
        return
    site = sites[args.site]
    if args.command in {"run", "campaign"}:
        if args.mode:
            site = replace(site, mode=args.mode)
        if args.chain < 1 or (args.chain > 1 and site.mode != "slurm"):
            raise ValueError("--chain requires a positive count and Slurm for multiple jobs")
        campaign, entries = ((args.campaign, [(Config.read(args.path), args.label)])
                             if args.command == "run" else campaign_entries(args.path))
        if not site.ssh and site.mode == "foreground":
            p = paths(site)
            attempts = []
            try:
                for config, label in entries:
                    attempts.append(prepare(config, executable=p["executable"], output_root=p["output_root"],
                        state_dir=p["state_dir"], campaign=campaign, label=label,
                        checkpoint=args.checkpoint if args.command == "run" else None, site=site.name,
                        environment=site.environment))
            except BaseException:
                from .runner import set_status
                for attempt in attempts:
                    set_status(attempt, "stopped", reason="Batch preparation failed")
                raise
            if len(attempts) == 1:
                result = launch(attempts[0])
            else:
                run_many(attempts, args.workers)
                result = {"attempts": list(map(str, attempts))}
        else:
            result = [submit(site, config, state_dir=state_dir, campaign=campaign, label=label,
                             checkpoint=args.checkpoint if args.command == "run" else None,
                             chain=args.chain)
                      for config, label in entries]
    elif args.command == "deploy":
        result = deploy(site, include_source=args.source is not None, local_source=args.source)
    elif args.command == "build":
        result = call(site, "build", timeout=3600, jobs=args.jobs)
    elif args.command == "inventory":
        result = call(site, "inventory", config_hash=args.hash, refresh_sizes=args.refresh_sizes,
                      **({"hard_refresh": True} if args.hard_refresh else {}))
    elif args.command == "inventory-cache":
        result = call(site, "inventory_cached")
    elif args.command == "inventory-update":
        result = call(site, "inventory_update", refresh_sizes=args.refresh_sizes, hard_refresh=args.hard_refresh)
    elif args.command == "reindex":
        result = call(site, "reindex")
    elif args.command == "info":
        result = call(site, "info")
    elif args.command in {"logs", "stop"}:
        if args.command == "logs" and args.follow:
            # tail uses native local/SSH streaming, independent of JSON polling.
            from .cluster import shell_path
            import subprocess
            log = str(Path(args.attempt) / "stdout.log")
            if site.ssh:
                subprocess.run(["ssh", "-T", site.ssh, "tail -n 80 -F -- " + shell_path(log)], check=True)
            else:
                subprocess.run(["tail", "-n", "80", "-F", "--", log], check=True)
            return
        result = call(site, args.command, attempt=args.attempt)
    elif args.command == "fetch":
        result = fetch(site, args.hash, args.destination, kind=args.kind)
    else:
        raise ValueError(args.command)
    print(result if isinstance(result, str) else json.dumps(result, indent=2))
