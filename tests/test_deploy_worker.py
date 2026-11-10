import fcntl
import gzip
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

RELEASE = "a" * 40 + "-123-1"


@pytest.fixture
def deploy(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "backend_deploy_worker", Path(__file__).parents[1] / "scripts/be-test-deploy-worker.py"
    )
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    root = tmp_path / "app"
    root.mkdir()
    (root / ".env").write_text("private configuration")
    (root / "manage.py").write_text("old code")
    (root / "passenger_wsgi.py").write_text("old entry point")
    (root / "media").mkdir()
    (root / "media/photo.jpg").write_text("keep media")
    state = tmp_path / "private-state"
    worker.process_queue(root, state)
    queue = root / "tmp/nfv-deploy"
    events = []

    def backup(root, state, release, **kwargs):
        assert (root / "manage.py").read_text() == "old code"
        events.append("backup")

    def command(args, root, timeout, **kwargs):
        assert (root / "manage.py").read_text() == "new code"
        events.append(" ".join(args))

    monkeypatch.setattr(worker, "backup_release", backup)
    monkeypatch.setattr(worker, "run_command", command)
    return worker, root, state, queue, events


def bundle(queue, extra=None, digest=None, release=RELEASE):
    path = queue / (release + ".zip")
    files = {
        "manage.py": "new code",
        "passenger_wsgi.py": "new entry point",
        "pyproject.toml": "[project]",
        "config/settings.py": "settings",
        "identity/models.py": "identity",
        "content/models.py": "content",
        ".env.example": "DB_ENGINE=sqlite",
    }
    files.update(extra or {})
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    checksum = digest or hashlib.sha256(path.read_bytes()).hexdigest()
    (queue / (release + ".ready.json")).write_text(
        json.dumps({"release": release, "sha256": checksum})
    )


def receipt(queue, release=RELEASE):
    return json.loads((queue / (release + ".result.json")).read_text())


def test_backend_deploy_backs_up_before_writes_and_restarts_after_all_commands(deploy):
    worker, root, state, queue, events = deploy
    bundle(queue)
    worker.process_queue(root, state)
    assert events[0] == "backup"
    assert "pip install" in events[1]
    stages = [line.split("manage.py ")[-1] for line in events[2:]]
    assert stages == [
        "check --deploy",
        "migrate --plan",
        "migrate --noinput",
        "collectstatic --noinput",
        "showmigrations",
        "migrate --check",
    ]
    assert (root / "tmp/restart.txt").read_text() == RELEASE
    assert (root / ".env").read_text() == "private configuration"
    assert (root / "media/photo.jpg").read_text() == "keep media"
    assert receipt(queue)["status"] == "deployed"
    assert not (queue / (RELEASE + ".zip")).exists()


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/absolute",
        ".env",
        "media/photo.jpg",
        "tmp/restart.txt",
        "config/.env",
        "config/.htaccess",
    ],
)
def test_bad_backend_zip_cannot_write_or_start_commands(deploy, path):
    worker, root, state, queue, events = deploy
    bundle(queue, {path: "bad"})
    worker.process_queue(root, state)
    assert receipt(queue)["status"] == "failed"
    assert (root / "manage.py").read_text() == "old code"
    assert events == []
    assert not (root / "tmp/restart.txt").exists()


def test_wrong_checksum_is_rejected_before_backup(deploy):
    worker, root, state, queue, events = deploy
    bundle(queue, digest="0" * 64)
    worker.process_queue(root, state)
    assert receipt(queue)["status"] == "failed"
    assert not events


def test_deleted_source_is_removed_but_runtime_data_is_preserved(deploy):
    worker, root, state, queue, events = deploy
    old_sources = ["config/removed_module.py", "templates/old.html", "static/old.css"]
    runtime = ["private/contract.pdf", "staticfiles/old.css", "media/keep.jpg"]
    for name in old_sources + runtime:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("existing")
    bundle(queue)
    worker.process_queue(root, state)
    assert receipt(queue)["status"] == "deployed"
    assert events[0] == "backup"
    assert all(not (root / name).exists() for name in old_sources)
    assert all((root / name).read_text() == "existing" for name in runtime)


def test_command_keeps_lock_when_cron_worker_is_killed(deploy, tmp_path):
    worker, root, state, queue, events = deploy
    bundle(queue)
    started = tmp_path / "started"
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\n"
        f"Path({str(started)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n"
    )
    fake_python.chmod(0o700)
    script = (
        "import importlib.util;from pathlib import Path;"
        f"s=importlib.util.spec_from_file_location('w',{worker.__file__!r});"
        "w=importlib.util.module_from_spec(s);s.loader.exec_module(w);"
        "w.backup_release=lambda *a,**kw:None;"
        f"w.PYTHON={str(fake_python)!r};"
        f"w.process_queue(Path({str(root)!r}),Path({str(state)!r}))"
    )
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.DEVNULL)
    child = None
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started.exists(), "Deploy command did not start"
        child = int(started.read_text())
        parent.kill()
        parent.wait(timeout=5)
        os.kill(child, 0)
        with (state / "worker.lock").open("a") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if child is not None:
            try:
                os.kill(child, signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_real_git_archive_is_accepted(deploy, tmp_path):
    worker, root, state, queue, events = deploy
    repo = Path(__file__).parents[1]
    path = tmp_path / "committed.zip"
    with path.open("wb") as output:
        subprocess.run(
            ["git", "archive", "--format=zip", "HEAD"], cwd=repo, stdout=output, check=True
        )
    with zipfile.ZipFile(path) as archive:
        assert any(n.startswith("templates/") for n in archive.namelist())
        assert any(n.startswith("static/") for n in archive.namelist())
        worker.validate_archive(archive, root)


def test_failed_backup_does_not_replace_source(deploy, monkeypatch):
    worker, root, state, queue, events = deploy
    bundle(queue)

    def failed(*args, **kwargs):
        raise RuntimeError("Backup failed")

    monkeypatch.setattr(worker, "backup_release", failed)
    worker.process_queue(root, state)
    assert receipt(queue)["status"] == "failed"
    assert (root / "manage.py").read_text() == "old code"
    assert not events


def test_failed_migration_does_not_restart_or_collect_static(deploy, monkeypatch):
    worker, root, state, queue, events = deploy
    bundle(queue)

    def command(args, root, timeout, **kwargs):
        events.append(" ".join(args))
        if args[-2:] == ["migrate", "--noinput"]:
            raise RuntimeError("Migration failed")

    monkeypatch.setattr(worker, "run_command", command)
    worker.process_queue(root, state)
    assert receipt(queue)["status"] == "failed"
    assert not (root / "tmp/restart.txt").exists()
    assert not any("collectstatic" in event for event in events)


def test_stale_backend_release_cannot_roll_back_newer_release(deploy):
    worker, root, state, queue, events = deploy
    (root / "tmp/restart.txt").write_text("f" * 40 + "-124-1")
    bundle(queue)
    worker.process_queue(root, state)
    assert receipt(queue)["status"] == "failed"
    assert not events


def test_partial_upload_is_not_deployed(deploy):
    worker, root, state, queue, events = deploy
    (queue / "upload.zip.part").write_text("partial")
    worker.process_queue(root, state)
    assert not events
    assert not (root / "tmp/restart.txt").exists()
    assert json.loads((queue / "worker.json").read_text())["app"] == "backend-test"


@pytest.mark.parametrize(
    "change",
    [
        {"db": {"NAME": "forv3510_production"}},
        {"hosts": ["auth.naturalfarmingvietnam.com"]},
        {"origins": ["https://naturalfarmingvietnam.com"]},
        {"debug": True},
    ],
)
def test_database_guard_refuses_other_database_or_origins(deploy, monkeypatch, change):
    worker, root, state, queue, events = deploy
    config = {
        "db": {"NAME": "forv3510_nfv_test", "ENGINE": "config.db.backends.mysql_connector"},
        "hosts": ["auth-test.naturalfarmingvietnam.com"],
        "origins": ["https://test.naturalfarmingvietnam.com"],
        "debug": False,
    }
    if "db" in change:
        config["db"].update(change["db"])
    else:
        config.update(change)
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=json.dumps(config)),
    )
    with pytest.raises(ValueError, match="TEST database"):
        worker.database_settings(root)


def test_backup_is_compressed_and_credentials_are_not_command_arguments(deploy, monkeypatch):
    worker, root, state, queue, events = deploy
    monkeypatch.undo()
    password = 'secret"with\\symbols'
    monkeypatch.setattr(
        worker,
        "database_settings",
        lambda root, **kwargs: {
            "NAME": "forv3510_nfv_test",
            "USER": "test",
            "PASSWORD": password,
            "HOST": "localhost",
            "PORT": "3306",
        },
    )

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert password not in " ".join(args)
        config = Path(args[1].split("=", 1)[1])
        assert config.stat().st_mode & 0o777 == 0o600
        assert 'password="secret\\"with\\\\symbols"' in config.read_text()
        kwargs["stdout"].write(b"CREATE TABLE test (id int);\n")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(worker.subprocess, "run", run)
    worker.backup_release(root, state, RELEASE)
    backup = state / "backups" / (RELEASE + ".sql.gz")
    assert gzip.decompress(backup.read_bytes()).startswith(b"CREATE TABLE")
    assert backup.stat().st_mode & 0o777 == 0o600
    assert not list((state / "backups").glob("*.cnf"))
    source_backup = state / "backups" / (RELEASE + ".source.zip")
    previous = source_backup.read_bytes()
    (root / "manage.py").write_text("partially deployed code")
    worker.backup_release(root, state, RELEASE)
    assert source_backup.read_bytes() == previous
    assert len(calls) == 1
