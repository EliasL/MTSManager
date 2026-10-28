"""Explicit build preparation; runners never silently rebuild a simulator."""

from pathlib import Path
import os
import io
import tarfile
import shutil
import tempfile
import re
import subprocess
import sys

from .config import DEFAULTS
from .files import digest, file_digest, read_json, write_json, now, locked


def command(args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def source_files(source):
    source = Path(source)
    if (source / ".git").exists():
        data = subprocess.check_output(["git", "-C", str(source), "ls-files", "-z", "--",
                                        "CMakeLists.txt", "cmake", "src", "libs"])
        paths = sorted(p.decode() for p in data.split(b"\0") if p)
        result = []
        for path in paths:
            entry = source / path
            if entry.is_file():
                result.append(path)
            elif entry.is_dir() and not any(entry.iterdir()):
                # An unpopulated Git link contributes no build input files.
                continue
            else:
                raise ValueError(f"Missing file or unsupported populated Git submodule: {entry}")
        return result
    return read_json(source / "SOURCE_FILES.json")


def source_hash(source):
    source = Path(source)
    return digest({p: file_digest(source / p) for p in source_files(source)})


def source_identity(source):
    source = Path(source).resolve()
    if (source / ".git").exists():
        revision = command(["git", "-C", str(source), "rev-parse", "HEAD"])
        branch = command(["git", "-C", str(source), "branch", "--show-current"])
        if not branch:
            # Submodules check out pinned commits. Verify mainline ancestry instead
            # of switching branches or changing the user's selected source revision.
            mainline = subprocess.run(["git", "-C", str(source), "merge-base", "--is-ancestor",
                                       revision, "refs/remotes/origin/main"], capture_output=True)
            if mainline.returncode == 0:
                branch = "main"
        if branch != "main":
            raise ValueError(f"New simulations require MTS2D main; found {branch!r}")
        if command(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no",
                    "--", "CMakeLists.txt", "cmake", "src", "libs"]):
            raise ValueError("Commit tracked MTS2D changes before preparing a build")
        tracked = set(source_files(source))
        actual = {str(p.relative_to(source)) for folder in ("src", "cmake")
                  for p in (source / folder).rglob("*") if p.is_file()}
        if actual - tracked:
            raise ValueError(f"Untracked source inputs: {sorted(actual - tracked)}")
        return {"revision": revision, "branch": branch, "source_sha256": source_hash(source)}
    record = read_json(source / "SOURCE.json")
    if record["branch"] != "main" or record["source_sha256"] != source_hash(source):
        raise ValueError("Uploaded source snapshot does not match SOURCE.json")
    return record


def verify_schema(source):
    parser = (Path(source) / "src/Data/param_parser.cpp").read_text()
    keys = set(re.findall(r"GET_VALUE\(config\.(\w+)\)", parser)) - {"name"}
    if keys != DEFAULTS.keys():
        raise ValueError(f"MTS2D configuration schema changed: {sorted(keys ^ DEFAULTS.keys())}")


def scripts_identity():
    root = Path(__file__).parent
    return {"version": "0.1.0", "sha256": digest({p.name: file_digest(p)
            for p in sorted(root.iterdir()) if p.suffix in {".py", ".json"}})}


def retain_executable(executable, archive_dir):
    """Retain verified executable bytes and their original build record."""
    executable = Path(executable).resolve()
    record = _verified_build(executable)
    if (executable.parent / ".retained").exists():
        return executable
    archive_dir = Path(archive_dir).resolve()
    destination = archive_dir / digest(record)
    with locked(archive_dir / ".retain.lock"):
        if destination.exists():
            if _verified_build(destination / "MTS2D") != record:
                raise ValueError("Retained executable does not match its build record")
            return destination / "MTS2D"
        temporary = Path(tempfile.mkdtemp(prefix=".retain-", dir=archive_dir))
        try:
            shutil.copy2(executable, temporary / "MTS2D")
            write_json(temporary / "build-manifest.json", record)
            if _verified_build(temporary / "MTS2D") != record:
                raise RuntimeError("Executable changed while being retained")
            (temporary / ".retained").write_text("Retained executable and build manifest.\n")
            (temporary / "MTS2D").chmod(0o555)
            (temporary / "build-manifest.json").chmod(0o444)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return destination / "MTS2D"


def source_archive(source, destination):
    """Package verified build inputs as one compressed transfer."""
    source = Path(source).resolve()
    identity = source_identity(source)
    files = source_files(source)
    from .files import canonical
    with tarfile.open(destination, "w:gz", dereference=True) as archive:
        for name in files:
            archive.add(source / name, arcname=name, recursive=False)
        for name, value in (("SOURCE.json", identity), ("SOURCE_FILES.json", files)):
            data = canonical(value)
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))
    if source_identity(source) != identity:
        raise RuntimeError("Source changed while preparing its upload")
    return identity


def install_source(source, staging, expected):
    """Update one managed source snapshot while preserving external build artifacts."""
    source, staging = Path(source).resolve(), Path(staging).resolve()
    if staging.parent != source.parent or not staging.name.startswith(".mts-source-upload-"):
        raise ValueError("Source staging directory is outside the configured source workspace")
    if (staging / "source.tar.gz").exists():
        with tarfile.open(staging / "source.tar.gz", "r:gz") as archive:
            members = archive.getmembers()
            if any(not (member.isfile() or member.isdir()) or
                   not (staging / member.name).resolve().is_relative_to(staging) for member in members):
                raise ValueError("Uploaded source archive contains an unsafe path or link")
            archive.extractall(staging, members=members)
    if source_identity(staging) != expected:
        raise ValueError("Uploaded source does not match the requested source identity")
    new_files = source_files(staging)
    new_names = set(new_files)
    with locked(source.parent / ".source.lock"):
        if (source / ".git").exists():
            raise ValueError("Update Git checkouts with Git; source uploads require a managed snapshot")
        old_files = []
        if source.exists():
            source_identity(source)
            old_files = source_files(source)
        for name in set(old_files) | new_names:
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Source manifest contains an unsafe path")
            if not (source / relative).resolve().is_relative_to(source):
                raise ValueError("Source manifest follows a symlink outside the source tree")
            if name in new_names and not (staging / relative).resolve().is_relative_to(staging):
                raise ValueError("Uploaded source follows a symlink outside its staging directory")
        source.mkdir(parents=True, exist_ok=True)
        for name in new_files:
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staging / name, destination)
        for name in set(old_files) - new_names:
            (source / name).unlink()
        write_json(source / "SOURCE_FILES.json", new_files)
        write_json(source / "SOURCE.json", expected)
        if source_identity(source) != expected:
            raise RuntimeError("Source verification failed after installation")
    shutil.rmtree(staging)
    return expected


def build(source, build_dir, *, jobs=2, native_arch=False, archive_dir=None):
    with locked(Path(source).resolve().parent / ".source.lock"):
        return _build(source, build_dir, jobs=jobs, native_arch=native_arch, archive_dir=archive_dir)


def _build(source, build_dir, *, jobs=2, native_arch=False, archive_dir=None):
    source, build_dir = Path(source).resolve(), Path(build_dir).resolve()
    archive_dir = Path(archive_dir) if archive_dir else build_dir.parent / "executables"
    if jobs < 1:
        raise ValueError("Build jobs must be positive")
    if type(native_arch) is not bool:
        raise TypeError("native_arch must be a boolean")
    if (build_dir / ".retained").exists():
        raise ValueError("Retained executables cannot be rebuilt; select a working build directory")
    cache_path = build_dir / "CMakeCache.txt"
    if cache_path.exists():
        paths = dict(re.findall(r"^(CMAKE_HOME_DIRECTORY|CMAKE_CACHEFILE_DIR):INTERNAL=(.*)$",
                                cache_path.read_text(), re.MULTILINE))
        if any(Path(paths[key]) != expected for key, expected in
               (("CMAKE_HOME_DIRECTORY", source), ("CMAKE_CACHEFILE_DIR", build_dir)) if key in paths):
            raise ValueError("CMake build was moved; preserve the old build directory and build in a fresh directory")
    if (build_dir / "MTS2D").exists():
        retain_executable(build_dir / "MTS2D", archive_dir)
    identity = source_identity(source)
    verify_schema(source)
    subprocess.run(["cmake", "-S", str(source), "-B", str(build_dir),
                    "-DCMAKE_BUILD_TYPE=Release",
                    "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
                    f"-DENABLE_NATIVE_ARCH={'ON' if native_arch else 'OFF'}"],
                   check=True, stdout=sys.stderr)
    subprocess.run(["cmake", "--build", str(build_dir), "--target", "MTS2D",
                    "--parallel", str(jobs)], check=True, stdout=sys.stderr)
    if source_identity(source) != identity:
        raise RuntimeError("MTS2D source changed during the build")
    executable = build_dir / "MTS2D"
    cache = (build_dir / "CMakeCache.txt").read_text()
    options = dict(re.findall(r"^(CMAKE_CXX_COMPILER|CMAKE_C_COMPILER|ENABLE_\w+|MTS_\w+|IDE_LIGHTWEIGHT):[^=]+=(.*)$",
                              cache, re.MULTILINE))
    record = identity | {"executable": str(executable), "executable_sha256": file_digest(executable),
                         "configuration_keys_sha256": digest(sorted(DEFAULTS)),
                         "built_at": now(), "build_type": "Release", "build_options": options,
                         "cmake_cache_sha256": file_digest(build_dir / "CMakeCache.txt")}
    write_json(build_dir / "build-manifest.json", record)
    retain_executable(executable, archive_dir)
    return record


def _verified_build(executable):
    executable = Path(executable).resolve()
    record = read_json(executable.parent / "build-manifest.json")
    if record["branch"] != "main" or record["executable_sha256"] != file_digest(executable):
        raise ValueError("Executable does not match its main-branch build manifest; run build first")
    return record


def executable_identity(executable):
    record = _verified_build(executable)
    if record.get("configuration_keys_sha256") != digest(sorted(DEFAULTS)):
        raise ValueError("Executable configuration schema differs; rebuild with the current package")
    return record
