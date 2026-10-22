"""A live terminal overview, with no terminal-specific automation."""

from pathlib import Path
import sys
import time

from .cluster import across, synchronized_snapshot
from .files import now
from .journal import Journal


def table(headers, rows):
    rows = [[str(x) for x in headers]] + [[str(x) for x in row] for row in rows]
    widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    return "\n".join("  ".join(value.ljust(width) for value, width in zip(row, widths))
                     for row in rows)


def size_text(value):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024


def render(sites, view, *, refresh_sizes=False, state_dir=None):
    if view == "history":
        records = Journal(state_dir).records()[-40:]
        return table(["Time", "Event", "Site", "Campaign", "Output / attempt"],
                     [(r["timestamp"], r["event"], r.get("site", ""), r.get("campaign", ""),
                       r.get("output_folder", r.get("attempt", ""))) for r in records])
    if view in {"overview", "jobs"}:
        snapshots = (synchronized_snapshot(sites, state_dir, refresh_sizes=refresh_sizes)
                     if state_dir else across(sites, "snapshot", refresh_sizes=refresh_sizes))
        parts = (("SERVERS", "servers"), ("JOBS", "jobs"), ("STORED DATA", "data"))
        sections = []
        for title, part in parts if view == "overview" else [("JOBS", "jobs")]:
            responses = {}
            for name, response in snapshots.items():
                if "error" in response:
                    responses[name] = response
                    continue
                data = response["data"]
                value = data["resources"] if part == "servers" else data["inventory"]
                if part == "jobs" and data["resources"]["scheduler"] is not None:
                    jobs = {j["job_id"]: j for j in data["resources"]["scheduler"]}
                    for item in value:
                        scheduler_id = item["status"].get("scheduler_id")
                        if scheduler_id in jobs:
                            item["status"]["scheduler_state"] = jobs[scheduler_id]["state"]
                        elif scheduler_id and item["status"]["state"] in {"queued", "running", "stopping"}:
                            item["status"]["scheduler_state"] = "absent from queue; inspect accounting/logs"
                responses[name] = {"data": value}
            sections.append(title + "\n" + render_view(responses, part))
        return "\n\n".join(sections)
    responses = across(sites, "resources" if view == "servers" else "inventory",
                       **({} if view == "servers" else {"refresh_sizes": refresh_sizes}))
    return render_view(responses, view)


def render_view(responses, view):
    rows, errors = [], []
    for site, response in sorted(responses.items()):
        if "error" in response:
            errors.append(f"{site}: {response['error']} {response.get('stderr') or ''}")
            continue
        data = response["data"]
        if view == "servers":
            rows.append((site, data["cpus"], f"{data['load_average'][0]:.2f}",
                         size_text(data["memory"]["MemAvailable"]) if "MemAvailable" in data["memory"] else "—",
                         size_text(data["disk_free_bytes"]),
                         len(data["scheduler"]) if data["scheduler"] is not None else "—"))
        else:
            for item in data:
                label = Path(item["folder"]).name
                if view == "data":
                    estimate = item["size"]
                    size = "not sampled"
                    if estimate:
                        size = "~" + size_text(estimate["estimated_bytes"])
                        if not estimate["coverage_complete"]:
                            size += " (partial traversal)"
                    rows.append((site, label, size, estimate["estimated_at"] if estimate else "—"))
                else:
                    state = item["status"]["state"]
                    if item.get("termination"):
                        state += " / " + item["termination"]["stopReason"]
                    if item["status"].get("process_alive") is False:
                        state += " (process missing)"
                    if item["status"].get("scheduler_state"):
                        state += " / " + item["status"]["scheduler_state"]
                    fraction = item["progress"]["fraction"]
                    rows.append((site, label, state, f"{100*fraction:.1f}%" if fraction is not None else "—",
                                 item["status"].get("scheduler_id", "—"), item["progress"].get("load")))
    headers = {"servers": ["Site", "CPUs", "Load(1m)", "RAM available", "Disk free", "Your Slurm jobs"],
               "jobs": ["Site", "Run", "State", "Progress", "Slurm ID", "Load"],
               "data": ["Site", "Run", "Estimated size", "Estimate timestamp"]}[view]
    output = table(headers, rows)
    if errors:
        output += "\n\nUnavailable / errors:\n" + "\n".join(errors)
    return output


def watch(sites, view, *, interval=15, refresh_sizes=False, state_dir=None):
    if interval < 2:
        raise ValueError("Refresh interval must be at least two seconds")
    try:
        while True:
            output = render(sites, view, refresh_sizes=refresh_sizes, state_dir=state_dir)
            if sys.stdout.isatty():
                print("\033[2J\033[H", end="")
            print(f"MTS2D {view} — {now()}\n\n{output}", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        return

