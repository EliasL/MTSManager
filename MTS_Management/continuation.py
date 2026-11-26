"""Prepare independent runs from a stopped run's accepted scientific history."""

from contextlib import contextmanager
import csv
from decimal import Decimal, DecimalException
from functools import lru_cache
import gzip
import hashlib
import math
from pathlib import Path
import re
import shutil
from xml.etree import ElementTree as ET

from .config import Config, CONTI_PARAMETERS, DEFAULTS, INDENTATION_SETTINGS, RECTANGLE_SETTINGS
from .data import last_macro_row, mesh_snapshots
from .errors import ExpectedError
from .files import digest, file_digest, locked, read_json
from .initial_mesh import MESH_FILE, verify_mesh


TARGETS = {
    "stopAfterSteps": "completed_steps", "stopAfterPlasticSteps": "plastic_steps",
    "stopAfterElasticSteps": "elastic_steps", "stopAfterEnergyDrops": "energy_drops",
    "stopAfterEnergyIncreases": "energy_increases",
}
FIXED_SETTINGS = {"rows", "cols", "seed", "QDSD", "meshDiagonal", "energyFunction",
                  "bulkModulus", "boundary", "deformation", "startLoad", "loading",
                  "reconnectionMethod", "initialMeshSha256"} | RECTANGLE_SETTINGS | INDENTATION_SETTINGS | CONTI_PARAMETERS
DUMP = re.compile(r"(?:checkpoint_.*|dump_l.*)\.xml\.gz")
NAMED_LOAD = re.compile(r"dump_l([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\.xml\.gz")
VTU_STEP = re.compile(r".*\.(\d+)\.vtu")
STEP_FOLDER = re.compile(r"step(\d+)")
POSITION = re.compile(rb"<load>\s*([+\-0-9.eE]+)\s*</load>\s*<loadSteps>\s*(\d+)\s*</loadSteps>")
COPY_BLOCK = 4 * 1024 * 1024


def checkpoint_revision(path):
    info = path.stat()
    return digest([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])


@lru_cache(maxsize=256)
def checkpoint_location(path, revision):
    # Native mesh checkpoints write these two adjacent scalars after the
    # node and element arrays. Find them without building or visiting an
    # XML element for every node of every candidate.
    try:
        with gzip.open(path, "rb") as stream:
            previous = b""
            while block := stream.read(1024 * 1024):
                data = previous + block
                if match := POSITION.search(data):
                    step, load = int(match[2]), float(match[1])
                    if step >= 1 and math.isfinite(load):
                        return step, load
                    break
                previous = data[-256:]
    except (OSError, EOFError, ValueError) as error:
        raise ExpectedError(f"Cannot read checkpoint position: {path.name}") from error
    raise ExpectedError(f"Missing or invalid checkpoint position: {path.name}")


def checkpoint_state(path):
    with gzip.open(path, "rb") as stream:
        simulation = ET.parse(stream).getroot().find("Simulation")
    return tuple(ET.tostring(simulation.find(name)) for name in ("mesh", "termination", "config"))


def checkpoint_files(folder):
    dumps = folder / "dumps"
    if dumps.is_symlink():
        raise ExpectedError("The checkpoint directory must be a physical directory.")
    files = [(path, checkpoint_revision(path)) for path in sorted(dumps.glob("*.xml.gz"))
             if DUMP.fullmatch(path.name) and path.is_file() and not path.is_symlink()]
    if not files:
        raise ExpectedError("This run has no saved checkpoint.")
    return files


def catalog_revision(files):
    return digest([(path.name, revision) for path, revision in files])


def named_load_bounds(path):
    match = NAMED_LOAD.fullmatch(path.name)
    if not match:
        return None
    try:
        number = Decimal(match[1])
        load = float(number)
        unit = float(Decimal(1).scaleb(number.as_tuple().exponent))
    except (DecimalException, OverflowError):
        return None
    if not math.isfinite(load) or not math.isfinite(unit):
        return None
    # Native ordinary dumps round to the last decimal digit in the name.
    # Allow floating-point rounding in the native multiply/divide as well.
    padding = unit / 2 + 8 * math.ulp(load)
    return load - padding, load + padding


def latest_checkpoint(files, last_load):
    candidates = [(path, revision, named_load_bounds(path)) for path, revision in files]

    def priority(entry):
        bounds = entry[2]
        if bounds is None:
            return 1, 0
        return (0 if bounds[0] <= last_load <= bounds[1] else 2), -bounds[1]

    checkpoints, skipped = [], []
    best = None
    names_match = True
    for path, revision, bounds in sorted(candidates, key=priority):
        if names_match and best is not None and bounds is not None and bounds[1] < best[1]:
            skipped.append((path, revision))
            continue
        step, load = checkpoint_location(path, revision)
        checkpoints.append((step, load, path, revision))
        if bounds is not None and not bounds[0] <= load <= bounds[1]:
            names_match = False
        if best is None or step > best[0]:
            best = step, load

    # The path coordinate increases even when the physical loading reverses.
    # If the decoded candidates disagree with that contract or their names,
    # rank every file by the saved step.
    ordered = sorted((step, load) for step, load, _, _ in checkpoints)
    monotone = all(right_load > left_load if right_step > left_step else right_load == left_load
                   for (left_step, left_load), (right_step, right_load) in zip(ordered, ordered[1:]))
    if not names_match or not monotone:
        checkpoints.extend((*checkpoint_location(path, revision), path, revision) for path, revision in skipped)
        skipped.clear()

    step, load, path, revision = max(checkpoints, key=lambda entry: entry[0])
    tied = [other for other_step, _, other, _ in checkpoints if other_step == step and other != path]
    if tied:
        state = checkpoint_state(path)
        if any(checkpoint_state(other) != state for other in tied):
            raise ExpectedError("The latest checkpoints contain conflicting states.")
    earlier = [other for other_step, _, other, _ in checkpoints if other_step < step]
    earlier.extend(other for other, _ in skipped)
    return {"name": path.name, "step": step, "load": load, "revision": revision}, earlier, len(checkpoints)


@contextmanager
def source_run(root, selection, *, verify_inactive=False):
    from .archive import check_inactive

    folder = Path(selection["folder"])
    manifest = folder / "manifest.json"
    if (folder.parent != root or folder.is_symlink() or
            not manifest.is_file() or manifest.is_symlink()):
        raise ExpectedError("Select the original managed run on its execution site.")
    try:
        with locked(folder / ".run.lock", blocking=False):
            if file_digest(manifest) != selection["manifest_sha256"]:
                raise ExpectedError("The source run changed. Refresh Monitor data and select it again.")
            config_path = folder / "config.conf"
            if config_path.is_symlink():
                raise ExpectedError("The source run configuration must be a physical file.")
            from .run_records import read_run_for_execution
            config, _ = read_run_for_execution(folder)
            if config.config_hash != selection["config_hash"]:
                raise ExpectedError("The source run has an inconsistent configuration identity.")
            if verify_inactive:
                check_inactive(folder)
            yield folder, config
    except BlockingIOError as error:
        raise ExpectedError("Wait until the source run stops.") from error


def plan(output_root, selection):
    root = Path(output_root).resolve()
    with locked(root / ".prepare.lock"), source_run(root, selection) as (folder, config):
        revision = catalog_revision(checkpoint_files(folder))
        settings = Config(**{key: config[key] for key in DEFAULTS}).as_dict()
        settings.update(dict.fromkeys(TARGETS, 0))
        settings["totalLoad"] += 1
        if config["deformation"] == "sequentialSlip":
            settings["directions"] += "," + settings["directions"].split(",")[-1]
        label = folder.name.split("[", 1)[0]
        return {"checkpoint_catalog": revision, "settings": settings, "label": label[:111] + "-further",
                "original_total_load": config["totalLoad"], "source_label": label,
                "fixed_settings": sorted(FIXED_SETTINGS)}


def validate_settings(config, original, checkpoint):
    changed = sorted(key for key in FIXED_SETTINGS if config[key] != original[key])
    if changed:
        raise ExpectedError("Keep these checkpoint settings unchanged: " + ", ".join(changed))
    if config["startLoad"] + config["totalLoad"] <= checkpoint["load"]:
        raise ExpectedError("Set a final load above the checkpoint load.")
    if config["deformation"] == "sequentialSlip":
        original_sequence = "".join(original["directions"].split()).split(",")
        used = math.ceil(checkpoint["load"])
        sequence = "".join(config["directions"].split()).split(",")
        if sequence[:used] != original_sequence[:used]:
            raise ExpectedError("Keep the slip entries already applied at the checkpoint.")
        reverse = lambda entries: {token[1] for token in entries if token.startswith("-")}
        if reverse(sequence) != reverse(original_sequence):
            raise ExpectedError("Keep the slip sequence's anchored edges unchanged.")


def copy_macro(source, target, checkpoint, config):
    last_step = 0
    with source.open(newline="") as original, target.open("w", newline="") as output:
        header = next(original, "")
        columns = next(csv.reader([header]), [])
        if "load_step" not in columns or "load" not in columns or len(columns) != len(set(columns)):
            raise ExpectedError("The source macro CSV has an invalid header.")
        step_column, load_column = columns.index("load_step"), columns.index("load")
        output.write(header)
        for line in original:
            row = next(csv.reader([line]))
            if len(row) != len(columns) or not line.endswith("\n"):
                raise ExpectedError("The source macro CSV has an incomplete step.")
            step = int(row[step_column])
            if step != last_step + 1:
                raise ExpectedError("The source macro CSV has an incomplete or repeated step.")
            output.write(line)
            last_step = step
            if step == checkpoint["step"]:
                if not math.isclose(float(row[load_column]), checkpoint["load"], rel_tol=1e-10, abs_tol=1e-12):
                    raise ExpectedError("The source macro CSV does not match the checkpoint load.")
                reached = [int(row[columns.index(column)]) >= config[key]
                           for key, column in TARGETS.items() if config[key]]
                if reached and (all(reached) if config["stopWhen"] == "all" else any(reached)):
                    raise ExpectedError("Increase or clear the stopping targets already met at the checkpoint.")
                break
        if last_step != checkpoint["step"]:
            raise ExpectedError("The source macro CSV does not reach the checkpoint step.")


def mesh_history(source, last_step):
    data = source / "data"
    if data.is_symlink():
        raise ExpectedError("The source mesh data must use physical directories.")
    snapshots = mesh_snapshots(source, through_step=last_step)
    copies, collections = [], []
    for path in data.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(source)
        if any((source / parent).is_symlink() for parent in relative.parents):
            continue
        if path.suffix == ".pvd":
            collections.append(relative)
            continue
        frame = VTU_STEP.fullmatch(path.name)
        diagnostic = next((match for part in relative.parts if (match := STEP_FOLDER.fullmatch(part))), None)
        end = int(diagnostic[1]) if diagnostic else int(frame[1]) if frame else None
        if end is not None and end <= last_step:
            copies.append(relative)
    if (source / "collection.pvd").is_file() and not (source / "collection.pvd").is_symlink():
        collections.append(Path("collection.pvd"))
    return copies, collections, len(snapshots)


def write_collections(source, target, collections):
    for relative in collections:
        tree = ET.parse(source / relative)
        collection = tree.getroot().find("Collection")
        destination = target / relative
        for entry in list(collection):
            frame = (destination.parent / entry.attrib["file"]).resolve()
            if not frame.is_relative_to(target) or not frame.is_file():
                collection.remove(entry)
        if len(collection):
            tree.write(destination, encoding="utf-8", xml_declaration=True)


def copy_history_files(files, origin, log):
    """Report byte progress and record the exact copied checkpoint."""
    total = sum(path.stat().st_size for path, _ in files)
    done = last_reported = 0
    checkpoint_hash = hashlib.sha256()
    log("Copying original simulation… 0%")
    for source, destination in files:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as reader, destination.open("wb") as writer:
            while block := reader.read(COPY_BLOCK):
                writer.write(block)
                if destination == origin:
                    checkpoint_hash.update(block)
                done += len(block)
                percent = 100 * done // total if total else 100
                if percent >= last_reported + 5 and percent < 100:
                    log(f"Copying original simulation… {percent}%")
                    last_reported = percent
        shutil.copystat(source, destination)
    if done != total:
        raise ExpectedError("The source history changed size during copying.")
    log("Copying original simulation… 100%")
    return checkpoint_hash.hexdigest()


def stage(root, destination, config, selection, *, log):
    with source_run(root, selection, verify_inactive=True) as (source, original):
        candidates = checkpoint_files(source)
        reviewed = selection.get("checkpoint")
        catalog = selection.get("checkpoint_catalog")
        if catalog is not None and catalog != catalog_revision(candidates):
            raise ExpectedError("The saved checkpoints changed. Select Load further again.")
        if catalog is None and not (reviewed and ("revision" in reviewed or "sha256" in reviewed)):
            raise ExpectedError("Review the checkpoint with Load further before running.")
        macro = source / "macroData.csv"
        if not macro.is_file() or macro.is_symlink():
            raise ExpectedError("The source run requires its macro CSV to continue.")
        last_row = last_macro_row(macro)
        if last_row is None or not math.isfinite(last_load := float(last_row["load"])):
            raise ExpectedError("The source macro CSV has no complete row with a finite load.")
        log(f"Selecting the latest saved checkpoint near CSV load {last_load:.12g}.")
        checkpoint, earlier, checked = latest_checkpoint(candidates, last_load)
        log(f"Checked {checked} of {len(candidates)} checkpoint files.")
        if reviewed and (any(checkpoint[key] != reviewed.get(key) for key in ("name", "step", "load")) or
                         ("revision" in reviewed and checkpoint["revision"] != reviewed["revision"])):
            raise ExpectedError("A different checkpoint is now latest. Select Load further again.")
        validate_settings(config, original, checkpoint)
        log(f"Preparing history through step {checkpoint['step']} (load {checkpoint['load']:.12g}).")
        copy_macro(macro, destination / macro.name, checkpoint, config)
        history, collections, count = mesh_history(source, checkpoint["step"])
        files = [(source / relative, destination / relative) for relative in history]
        provenance = destination / "source-history"
        earlier_dumps = provenance / "dumps"
        files.extend((path, earlier_dumps / path.name) for path in earlier)
        # Preserve the origin independently of later checkpoint rotation.
        # Seed the slot the simulator would write at this step so a retry
        # before the first accepted step cannot select conflicting states.
        saved = provenance / "checkpoint.xml.gz"
        original_dump = source / "dumps" / checkpoint["name"]
        slot = (checkpoint["step"] // max(1, config["checkpointInterval"])) % 2
        files.extend([(original_dump, saved),
                      (original_dump, destination / "dumps" / f"checkpoint_{slot}.xml.gz")])
        for name in ("config.conf", "manifest.json"):
            files.append((source / name, provenance / name))
        if config["initialMeshSha256"]:
            verify_mesh(source, original)
            files.append((source / MESH_FILE, destination / MESH_FILE))
        if reviewed and "revision" not in reviewed and file_digest(original_dump) != reviewed["sha256"]:
            raise ExpectedError("The reviewed checkpoint has changed. Select Load further again.")
        checksum = copy_history_files(files, saved, log)
        verify_mesh(destination, config)
        if checkpoint_revision(original_dump) != checkpoint["revision"]:
            raise ExpectedError("The reviewed checkpoint changed during copying.")
        write_collections(source, destination, collections)
        log(f"Copied {count} mesh snapshots and the restart checkpoint.")
        checkpoint["sha256"] = checksum
        return {key: selection[key] for key in ("folder", "config_hash", "manifest_sha256")} | {
            "checkpoint": checkpoint, "saved_checkpoint": str(saved.relative_to(destination)),
        }
