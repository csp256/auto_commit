#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "auto_commit"
HEARTBEAT_DIRNAME = ".auto_commit"
HEARTBEAT_FILENAME = "heartbeat.json"
LOG_FILENAME = "auto_commit.log"
LOCK_FILENAME = "auto_commit.lock"


@dataclass
class Job:
    name: str
    source: Path
    git_dir: Path
    repo_url: str
    branch: str = "main"
    git_author_name: str = "auto_commit"
    git_author_email: str = "auto_commit@localhost"
    heartbeat_always_commit: bool = True
    missing_source_fatal: bool = False


@dataclass
class JobResult:
    name: str
    ok: bool
    partial: bool
    committed: bool
    pushed: bool
    message: str
    warnings: list[str]
    errors: list[str]
    commit_sha: str | None = None


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def iso_now() -> str:
    return now_local().isoformat(timespec="seconds")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def append_log(log_path: Path, message: str) -> None:
    ensure_dir(log_path.parent)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{iso_now()}] {message}\n")


def run_cmd(
    args: list[str],
    *,
    check: bool = False,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        text=True,
        capture_output=True,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
    )


def git_env(job: Job) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_DIR"] = str(job.git_dir)
    env["GIT_WORK_TREE"] = str(job.source)
    return env


def git(
    job: Job,
    *args: str,
    check: bool = False,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_cmd(
        ["git", *args],
        check=check,
        env=git_env(job),
        cwd=cwd if cwd is not None else job.source,
    )


def set_windows_hidden(path: Path) -> None:
    if os.name != "nt":
        return
    run_cmd(["attrib", "+h", str(path)])


def ensure_hidden_heartbeat_dir(source: Path) -> Path:
    hb_dir = source / HEARTBEAT_DIRNAME
    ensure_dir(hb_dir)
    set_windows_hidden(hb_dir)
    return hb_dir


def load_jobs(config_path: Path) -> list[Job]:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    jobs: list[Job] = []
    for item in raw["jobs"]:
        jobs.append(
            Job(
                name=item["name"],
                source=Path(item["source"]),
                git_dir=Path(item["git_dir"]),
                repo_url=item["repo_url"],
                branch=item.get("branch", "main"),
                git_author_name=item.get("git_author_name", "auto_commit"),
                git_author_email=item.get("git_author_email", "auto_commit@localhost"),
                heartbeat_always_commit=item.get("heartbeat_always_commit", True),
                missing_source_fatal=item.get("missing_source_fatal", False),
            )
        )
    return jobs


def acquire_lock(lock_path: Path) -> None:
    ensure_dir(lock_path.parent)
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(
            {
                "pid": os.getpid(),
                "timestamp": iso_now(),
                "hostname": socket.gethostname(),
            },
            f,
            indent=2,
        )


def release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass


def repo_exists(git_dir: Path) -> bool:
    return (git_dir / "HEAD").exists() and (git_dir / "objects").exists()


def ensure_repo(job: Job, log_path: Path) -> None:
    ensure_dir(job.git_dir)

    if not repo_exists(job.git_dir):
        append_log(log_path, f"Initializing git dir: {job.git_dir}")
        git(job, "init", "--initial-branch", job.branch, check=True)

    remote = git(job, "remote", "get-url", "origin")
    if remote.returncode != 0:
        git(job, "remote", "add", "origin", job.repo_url, check=True)
    else:
        current = remote.stdout.strip()
        if current != job.repo_url:
            git(job, "remote", "set-url", "origin", job.repo_url, check=True)

    sw = git(job, "switch", "-C", job.branch)
    if sw.returncode != 0:
        raise RuntimeError(f"Failed to switch to branch {job.branch}: {sw.stderr.strip()}")


def write_heartbeat(job: Job, warnings: list[str], errors: list[str]) -> Path:
    hb_dir = ensure_hidden_heartbeat_dir(job.source)
    hb_path = hb_dir / HEARTBEAT_FILENAME
    payload = {
        "app": APP_NAME,
        "timestamp": iso_now(),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "job": job.name,
        "source": str(job.source),
        "git_dir": str(job.git_dir),
        "repo_url": job.repo_url,
        "branch": job.branch,
        "status": "partial" if warnings or errors else "ok",
        "warnings": warnings,
        "errors": errors,
    }
    hb_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    set_windows_hidden(hb_path)
    return hb_path


def stage_everything(job: Job, log_path: Path) -> tuple[bool, list[str], list[str]]:
    warnings: list[str] = []
    errors: list[str] = []

    cp = git(job, "add", "-A", "--ignore-errors", ".")
    append_log(log_path, f"git add rc={cp.returncode}")
    if cp.stdout.strip():
        append_log(log_path, f"git add stdout:\n{cp.stdout}")
    if cp.stderr.strip():
        append_log(log_path, f"git add stderr:\n{cp.stderr}")

    partial = False
    if cp.returncode != 0:
        partial = True
        warnings.append("git add reported indexing errors; some files may have been skipped")

    return partial, warnings, errors


def has_staged_changes(job: Job) -> bool:
    cp = git(job, "diff", "--cached", "--quiet")
    return cp.returncode == 1


def commit(job: Job, message: str, log_path: Path) -> tuple[bool, str | None]:
    env = git_env(job)
    env["GIT_AUTHOR_NAME"] = job.git_author_name
    env["GIT_AUTHOR_EMAIL"] = job.git_author_email
    env["GIT_COMMITTER_NAME"] = job.git_author_name
    env["GIT_COMMITTER_EMAIL"] = job.git_author_email

    cp = run_cmd(["git", "commit", "-m", message], env=env, cwd=job.source)
    append_log(log_path, f"git commit rc={cp.returncode}")
    if cp.stdout.strip():
        append_log(log_path, f"git commit stdout:\n{cp.stdout}")
    if cp.stderr.strip():
        append_log(log_path, f"git commit stderr:\n{cp.stderr}")

    if cp.returncode != 0:
        return False, None

    sha = git(job, "rev-parse", "HEAD", check=True).stdout.strip()
    return True, sha


def push(job: Job, log_path: Path) -> bool:
    cp = git(job, "push", "-u", "origin", job.branch)
    append_log(log_path, f"git push rc={cp.returncode}")
    if cp.stdout.strip():
        append_log(log_path, f"git push stdout:\n{cp.stdout}")
    if cp.stderr.strip():
        append_log(log_path, f"git push stderr:\n{cp.stderr}")
    return cp.returncode == 0


def make_commit_message(job: Job) -> str:
    host = socket.gethostname()
    return f"auto_commit(job={job.name},host={host},ts={iso_now()})"


def process_job(job: Job) -> JobResult:
    log_path = job.git_dir / LOG_FILENAME
    lock_path = job.git_dir / LOCK_FILENAME
    warnings: list[str] = []
    errors: list[str] = []

    append_log(log_path, f"Starting job {job.name}")

    try:
        acquire_lock(lock_path)

        if not job.source.exists():
            msg = f"Source path does not exist: {job.source}"
            append_log(log_path, msg)
            if job.missing_source_fatal:
                return JobResult(
                    name=job.name,
                    ok=False,
                    partial=False,
                    committed=False,
                    pushed=False,
                    message=msg,
                    warnings=warnings,
                    errors=[msg],
                )
            warnings.append(msg)
            return JobResult(
                name=job.name,
                ok=True,
                partial=True,
                committed=False,
                pushed=False,
                message=msg,
                warnings=warnings,
                errors=errors,
            )

        ensure_repo(job, log_path)

        hb_path = write_heartbeat(job, warnings, errors)
        append_log(log_path, f"Wrote heartbeat: {hb_path}")

        partial, add_warnings, add_errors = stage_everything(job, log_path)
        warnings.extend(add_warnings)
        errors.extend(add_errors)

        if not has_staged_changes(job):
            msg = "No staged changes detected"
            append_log(log_path, msg)
            return JobResult(
                name=job.name,
                ok=True,
                partial=partial,
                committed=False,
                pushed=False,
                message=msg,
                warnings=warnings,
                errors=errors,
            )

        committed, sha = commit(job, make_commit_message(job), log_path)
        if not committed:
            msg = "Commit failed"
            append_log(log_path, msg)
            return JobResult(
                name=job.name,
                ok=False,
                partial=partial,
                committed=False,
                pushed=False,
                message=msg,
                warnings=warnings,
                errors=errors or [msg],
            )

        pushed = push(job, log_path)
        msg = "Backup completed" if pushed else "Commit succeeded but push failed"
        append_log(log_path, msg)

        return JobResult(
            name=job.name,
            ok=pushed,
            partial=partial,
            committed=True,
            pushed=pushed,
            message=msg,
            warnings=warnings,
            errors=errors if pushed else (errors + ["Push failed"]),
            commit_sha=sha,
        )

    except FileExistsError:
        msg = f"Another run appears active; lock exists at {lock_path}"
        append_log(log_path, msg)
        return JobResult(
            name=job.name,
            ok=False,
            partial=False,
            committed=False,
            pushed=False,
            message=msg,
            warnings=warnings,
            errors=[msg],
        )
    except Exception as exc:
        msg = f"Unhandled error: {exc}"
        append_log(log_path, msg)
        return JobResult(
            name=job.name,
            ok=False,
            partial=False,
            committed=False,
            pushed=False,
            message=msg,
            warnings=warnings,
            errors=[msg],
        )
    finally:
        release_lock(lock_path)
        append_log(log_path, f"Finished job {job.name}")


def print_summary(results: list[JobResult]) -> int:
    any_fail = False
    for r in results:
        status = "OK"
        if not r.ok:
            status = "FAIL"
            any_fail = True
        elif r.partial:
            status = "PARTIAL"

        print(f"[{status}] {r.name}: {r.message}")
        if r.commit_sha:
            print(f"  commit: {r.commit_sha}")
        for w in r.warnings:
            print(f"  warning: {w}")
        for e in r.errors:
            print(f"  error: {e}")

    return 1 if any_fail else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()

    jobs = load_jobs(args.config)
    results = [process_job(job) for job in jobs]
    return print_summary(results)


if __name__ == "__main__":
    raise SystemExit(main())