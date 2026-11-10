#!/usr/bin/env python3
"""Standalone worker installed outside the backend test application root."""

import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

APP_ROOT = Path("/home/forv3510/apps/nfv-core-auth-test")
PYTHON = "/home/forv3510/virtualenv/apps/nfv-core-auth-test/3.11/bin/python"
MYSQLDUMP = "/bin/mysqldump"
EXPECTED_DATABASE = "forv3510_nfv_test"
RELEASE_PATTERN = re.compile(r"[a-f0-9]{40}-[0-9]+-[0-9]+")
SOURCE_DIRS = {
    "config",
    "identity",
    "content",
    "commerce",
    "farms",
    "members",
    "memberships",
    "referrals",
    "assets",
    "locale",
    "scripts",
    "tests",
    "static",
    "templates",
    ".github",
}
SOURCE_FILES = {
    "manage.py",
    "passenger_wsgi.py",
    "pyproject.toml",
    "README.md",
    ".env.example",
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
}


def atomic_write(path, content):
    fd, temporary = tempfile.mkstemp(prefix=".nfv-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def source_path(name):
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        return False
    if path.parts[0] not in SOURCE_DIRS | SOURCE_FILES:
        return False
    if any(p in {".htaccess", "__pycache__", ".git"} for p in path.parts):
        return False
    return not any(p.startswith(".env") for p in path.parts) or name == ".env.example"


def validate_archive(archive, root):
    names = set()
    total = 0
    if len(archive.infolist()) > 50000:
        raise ValueError("Too many ZIP entries")
    for entry in archive.infolist():
        if (
            not source_path(entry.filename)
            or entry.filename in names
            or stat.S_ISLNK(entry.external_attr >> 16)
        ):
            raise ValueError("Unsafe or duplicate ZIP entry")
        if not (root / entry.filename).resolve().is_relative_to(root.resolve()):
            raise ValueError("Destination escapes backend test root")
        names.add(entry.filename)
        total += entry.file_size
        if total > 1024**3:
            raise ValueError("ZIP exceeds expanded size limit")
    required = {
        "manage.py",
        "passenger_wsgi.py",
        "pyproject.toml",
        "config/settings.py",
        "identity/models.py",
        "content/models.py",
    }
    if not required.issubset(names) or archive.testzip() is not None:
        raise ValueError("Invalid backend source archive")


def database_settings(root, lock_fd=None):
    # Load current settings before changing source; capture credentials, never log them.
    code = (
        "import json;from config import settings as s;"
        "print(json.dumps({'db': s.DATABASES['default'],"
        "'hosts':s.ALLOWED_HOSTS,'origins':s.PUBLIC_SITE_ORIGINS,'debug':s.DEBUG}))"
    )
    result = subprocess.run(
        [PYTHON, "-c", code],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        pass_fds=() if lock_fd is None else (lock_fd,),
    )
    if result.returncode:
        raise RuntimeError("Cannot read backend test database settings")
    config = json.loads(result.stdout)
    db = config["db"]
    if (
        db.get("ENGINE") != "config.db.backends.mysql_connector"
        or db.get("NAME") != EXPECTED_DATABASE
        or config["debug"]
        or set(config["hosts"]) != {"auth-test.naturalfarmingvietnam.com"}
        or set(config["origins"]) != {"https://test.naturalfarmingvietnam.com"}
    ):
        raise ValueError("Refusing to deploy outside the configured backend TEST database/origins")
    return db


def option_value(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def backup_release(root, state, release, lock_fd=None):
    db = database_settings(root, lock_fd=lock_fd)
    backups = state / "backups"
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    backups.chmod(0o700)
    backup = backups / (release + ".sql.gz")
    source_backup = backups / (release + ".source.zip")
    complete = backups / (release + ".backup.json")
    if complete.exists():
        checksums = json.loads(complete.read_text())
        if checksums.get("sql") != file_sha256(backup) or checksums.get("source") != file_sha256(
            source_backup
        ):
            raise RuntimeError("Existing pre-deploy backup is corrupt; manual review required")
        print(f"Reusing original pre-deploy backup for {release}", flush=True)
        return
    if backup.exists() or source_backup.exists():
        raise RuntimeError("Incomplete pre-deploy backup exists; manual review required")
    config_fd, config_name = tempfile.mkstemp(suffix=".cnf", dir=backups)
    try:
        with os.fdopen(config_fd, "w") as stream:
            stream.write("[client]\nprotocol=TCP\n")
            for option, key in [
                ("user", "USER"),
                ("password", "PASSWORD"),
                ("host", "HOST"),
                ("port", "PORT"),
            ]:
                stream.write(f'{option}="{option_value(db[key])}"\n')
        # Write raw SQL to a private temporary file so timeout covers the full dump.
        with tempfile.TemporaryFile(dir=backups) as raw:
            result = subprocess.run(
                [
                    MYSQLDUMP,
                    "--defaults-file=" + config_name,
                    "--single-transaction",
                    "--default-character-set=utf8mb4",
                    "--quick",
                    "--skip-lock-tables",
                    "--no-tablespaces",
                    db["NAME"],
                ],
                stdout=raw,
                stderr=subprocess.PIPE,
                timeout=180,
                check=False,
                pass_fds=() if lock_fd is None else (lock_fd,),
            )
            if result.returncode:
                error = (result.stderr or b"").decode(errors="replace")
                if db["PASSWORD"]:
                    error = error.replace(db["PASSWORD"], "***")
                atomic_write(backups / (release + ".backup-error.log"), error)
                raise RuntimeError("Database backup failed; application source was not changed")
            if raw.tell() == 0:
                raise RuntimeError("Database backup was empty; application source was not changed")
            raw.seek(0)
            with backup.open("xb") as output:
                backup.chmod(0o600)
                with gzip.GzipFile(fileobj=output, mode="wb") as compressed:
                    shutil.copyfileobj(raw, compressed)
        with zipfile.ZipFile(source_backup, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            source_backup.chmod(0o600)
            for source in managed_files(root):
                archive.write(source, source.relative_to(root).as_posix())
    finally:
        Path(config_name).unlink(missing_ok=True)
    atomic_write(
        complete, json.dumps({"sql": file_sha256(backup), "source": file_sha256(source_backup)})
    )
    print(f"Backup completed for {release} (private deploy-tools state)", flush=True)


def file_sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run_command(args, root, timeout, lock_fd=None):
    print("Running: " + " ".join(args[1:]), flush=True)
    # Keep flock alive in the actual command even if its cron parent dies.
    process = subprocess.Popen(
        args, cwd=root, start_new_session=True, pass_fds=() if lock_fd is None else (lock_fd,)
    )
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise RuntimeError("Deployment command timed out; inspect worker log") from None
    if code:
        raise RuntimeError("Deployment command failed; inspect worker log")


def install_release(root, queue, state, release, request, lock_fd=None):
    manifest = json.loads(request.read_text())
    if manifest.get("release") != release:
        raise ValueError("Manifest release mismatch")
    bundle = queue / (release + ".zip")
    if bundle.is_symlink():
        raise ValueError("ZIP must not be a symlink")
    with bundle.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if manifest.get("sha256") != digest:
        raise ValueError("ZIP SHA-256 mismatch")
    if not (root / ".env").is_file():
        raise ValueError("Backend TEST .env is missing")
    with tempfile.TemporaryDirectory(prefix="extract-", dir=queue) as stage:
        stage = Path(stage)
        with zipfile.ZipFile(bundle) as archive:
            validate_archive(archive, root)
            archive.extractall(stage)
        backup_release(root, state, release, lock_fd=lock_fd)
        incoming = sorted(p for p in stage.rglob("*") if p.is_file())
        incoming_names = {p.relative_to(stage).as_posix() for p in incoming}
        obsolete = [
            p for p in managed_files(root) if p.relative_to(root).as_posix() not in incoming_names
        ]
        for source in obsolete:
            source.unlink()
        print(f"Removed {len(obsolete)} obsolete managed source file(s)", flush=True)
        for source in incoming:
            target = root / source.relative_to(stage)
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_file(source, target)
        run_command(
            [
                PYTHON,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--no-cache-dir",
                "-e",
                ".[mysql]",
            ],
            root,
            240,
            lock_fd=lock_fd,
        )
        for arguments, timeout in [
            (["check", "--deploy"], 60),
            (["migrate", "--plan"], 60),
            (["migrate", "--noinput"], 300),
            (["collectstatic", "--noinput"], 180),
            (["showmigrations"], 60),
            (["migrate", "--check"], 60),
        ]:
            run_command([PYTHON, "manage.py", *arguments], root, timeout, lock_fd=lock_fd)
    atomic_write(root / "tmp/restart.txt", release)
    return digest


def managed_files(root):
    for top in sorted(SOURCE_DIRS | SOURCE_FILES):
        path = root / top
        candidates = path.rglob("*") if path.is_dir() and not path.is_symlink() else [path]
        for source in candidates:
            relative = source.relative_to(root).as_posix()
            if source_path(relative) and source.is_file() and not source.is_symlink():
                if not source.resolve().is_relative_to(root.resolve()):
                    raise ValueError("Managed source escapes backend test root")
                yield source


def copy_file(source, target):
    fd, temporary = tempfile.mkstemp(prefix=".nfv-", dir=target.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def release_order(release):
    _, run_id, attempt = release.rsplit("-", 2)
    return int(run_id), int(attempt)


def process_queue(root=APP_ROOT, state=None):
    root = Path(root)
    if not (root / "manage.py").is_file():
        raise ValueError("Initialize the backend test app before installing this worker")
    state = Path(state) if state is not None else Path(__file__).resolve().parent / "state"
    if state.resolve().is_relative_to(root.resolve()):
        raise ValueError("Worker state must be outside the backend app root")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    queue = root / "tmp/nfv-deploy"
    if not queue.resolve().is_relative_to(root.resolve()):
        raise ValueError("Queue escapes backend test root")
    queue.mkdir(parents=True, exist_ok=True)
    atomic_write(queue / ".htaccess", "Require all denied\n")
    with (state / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        atomic_write(
            queue / "worker.json",
            json.dumps(
                {
                    "protocol": 1,
                    "app": "backend-test",
                    "time": time.time(),
                }
            ),
        )
        pending = [
            p
            for p in queue.glob("*.ready.json")
            if RELEASE_PATTERN.fullmatch(p.name.removesuffix(".ready.json")) and not p.is_symlink()
        ]
        for request in sorted(
            pending, key=lambda p: release_order(p.name.removesuffix(".ready.json"))
        ):
            release = request.name.removesuffix(".ready.json")
            result = queue / (release + ".result.json")
            if result.exists():
                request.unlink()
                continue
            receipt = {"release": release}
            try:
                restart = root / "tmp/restart.txt"
                installed = restart.read_text().strip() if restart.exists() else ""
                if RELEASE_PATTERN.fullmatch(installed) and release_order(release) < release_order(
                    installed
                ):
                    raise ValueError("A newer release has already been installed")
                receipt["sha256"] = install_release(
                    root, queue, state, release, request, lock_fd=lock.fileno()
                )
                receipt["status"] = "deployed"
            except Exception as error:
                receipt.update(status="failed", error=str(error))
            atomic_write(result, json.dumps(receipt))
            request.unlink()
            if receipt["status"] == "deployed":
                (queue / (release + ".zip")).unlink(missing_ok=True)
            print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    process_queue()
