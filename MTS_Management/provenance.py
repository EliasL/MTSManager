"""Explicit build preparation; runners never silently rebuild a simulator."""

from pathlib import Path
import re
import subprocess
import sys

from .config import DEFAULTS
from .files import digest, file_digest, read_json, write_json, now


def command(args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def source_files(source):
    source = Path(source)
    if (source / ".git").exists():
        data = subprocess.check_output(["git", "-C", str(source), "ls-files", "-z", "--",
                                        "CMakeLists.txt", "src", "libs"])
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
        if branch != "main":
            raise ValueError(f"New simulations require MTS2D main; found {branch!r}")
        if command(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no",
                    "--", "CMakeLists.txt", "src", "libs"]):
            raise ValueError("Commit tracked MTS2D changes before preparing a build")
        tracked = set(source_files(source))
        actual = {str(p.relative_to(source)) for p in (source / "src").rglob("*") if p.is_file()}
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


def build(source, build_dir, *, jobs=2, native_arch=False):
    source, build_dir = Path(source).resolve(), Path(build_dir).resolve()
    if jobs < 1:
        raise ValueError("Build jobs must be positive")
    if type(native_arch) is not bool:
        raise TypeError("native_arch must be a boolean")
    cache_path = build_dir / "CMakeCache.txt"
    if cache_path.exists():
        paths = dict(re.findall(r"^(CMAKE_HOME_DIRECTORY|CMAKE_CACHEFILE_DIR):INTERNAL=(.*)$",
                                cache_path.read_text(), re.MULTILINE))
        if any(Path(paths[key]) != expected for key, expected in
               (("CMAKE_HOME_DIRECTORY", source), ("CMAKE_CACHEFILE_DIR", build_dir)) if key in paths):
            raise ValueError("CMake build was moved; preserve the old build directory and build in a fresh directory")
    identity = source_identity(source)
    verify_schema(source)
    subprocess.run(["cmake", "-S", str(source), "-B", str(build_dir),
                    "-DCMAKE_BUILD_TYPE=Release",
                    f"-DENABLE_NATIVE_ARCH={'ON' if native_arch else 'OFF'}"],
                   check=True, stdout=sys.stderr)
    subprocess.run(["cmake", "--build", str(build_dir), "--target", "MTS2D",
                    "--parallel", str(jobs)], check=True, stdout=sys.stderr)
    if source_identity(source) != identity:
        raise RuntimeError("MTS2D source changed during the build")
    executable = build_dir / "MTS2D"
    cache = (build_dir / "CMakeCache.txt").read_text()
    options = dict(re.findall(r"^(CMAKE_CXX_COMPILER|CMAKE_C_COMPILER|ENABLE_\w+|IDE_LIGHTWEIGHT):[^=]+=(.*)$",
                              cache, re.MULTILINE))
    record = identity | {"executable": str(executable), "executable_sha256": file_digest(executable),
                         "configuration_keys_sha256": digest(sorted(DEFAULTS)),
                         "built_at": now(), "build_type": "Release", "build_options": options,
                         "cmake_cache_sha256": file_digest(build_dir / "CMakeCache.txt")}
    write_json(build_dir / "build-manifest.json", record)
    return record


def executable_identity(executable):
    executable = Path(executable).resolve()
    record = read_json(executable.parent / "build-manifest.json")
    if record.get("configuration_keys_sha256") != digest(sorted(DEFAULTS)):
        raise ValueError("Executable configuration schema differs; rebuild with the current package")
    if record["branch"] != "main" or record["executable_sha256"] != file_digest(executable):
        raise ValueError("Executable does not match its main-branch build manifest; run build first")
    return record
