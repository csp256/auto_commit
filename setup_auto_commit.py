#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path


APP_NAME = "auto_commit"
WINDOWS_TASK_NAME = "auto_commit"
MACOS_LABEL = "com.csp256.auto_commit"
DEFAULT_HOUR = 19
DEFAULT_MINUTE = 0


def run(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=check)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def app_root() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise RuntimeError("LOCALAPPDATA is not set")
        return Path(local) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / ".config" / APP_NAME
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def config_default_path() -> Path:
    return app_root() / "config.json"


def git_dir_default(job_name: str) -> Path:
    return app_root() / "git" / job_name


def logs_dir() -> Path:
    return app_root()


def default_config() -> dict:
    user = (
        os.environ.get("USERNAME")
        if sys.platform == "win32"
        else os.environ.get("USER")
    ) or "user"

    work_source = r"C:\work" if sys.platform == "win32" else str(Path.home() / "work")

    return {
        "schedule": {
            "mode": "daily",
            "hour": DEFAULT_HOUR,
            "minute": DEFAULT_MINUTE
        },
        "jobs": [
            {
                "name": "work",
                "source": work_source,
                "git_dir": str(git_dir_default("work")),
                "repo_url": f"git@gitlab.example.com:{user}/work.git",
                "branch": "main",
                "git_author_name": "auto_commit",
                "git_author_email": "auto_commit@localhost",
                "heartbeat_always_commit": True,
                "missing_source_fatal": False
            }
        ]
    }


def write_default_config(config_path: Path) -> None:
    ensure_dir(config_path.parent)
    if not config_path.exists():
        config_path.write_text(json.dumps(default_config(), indent=2) + "\n", encoding="utf-8")


def git_env(git_dir: Path, work_tree: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_DIR"] = str(git_dir)
    env["GIT_WORK_TREE"] = str(work_tree)
    return env


def git(git_dir: Path, work_tree: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        text=True,
        capture_output=True,
        check=check,
        env=git_env(git_dir, work_tree),
        cwd=str(work_tree),
    )


def repo_exists(git_dir: Path) -> bool:
    return (git_dir / "HEAD").exists() and (git_dir / "objects").exists()


def ensure_repo(job: dict) -> None:
    source = Path(job["source"])
    git_dir = Path(job["git_dir"])
    branch = job.get("branch", "main")
    repo_url = job["repo_url"]

    ensure_dir(source)
    ensure_dir(git_dir)

    if not repo_exists(git_dir):
        cp = git(git_dir, source, "init", "--initial-branch", branch)
        if cp.returncode != 0:
            raise RuntimeError(f"git init failed for {job['name']}:\n{cp.stderr}")

    cp = git(git_dir, source, "remote", "get-url", "origin")
    if cp.returncode != 0:
        cp = git(git_dir, source, "remote", "add", "origin", repo_url)
        if cp.returncode != 0:
            raise RuntimeError(f"git remote add failed for {job['name']}:\n{cp.stderr}")
    elif cp.stdout.strip() != repo_url:
        cp = git(git_dir, source, "remote", "set-url", "origin", repo_url)
        if cp.returncode != 0:
            raise RuntimeError(f"git remote set-url failed for {job['name']}:\n{cp.stderr}")

    cp = git(git_dir, source, "switch", "-C", branch)
    if cp.returncode != 0:
        raise RuntimeError(f"git switch failed for {job['name']}:\n{cp.stderr}")

    git(git_dir, source, "config", "lfs.locksverify", "false")


def load_config(config_path: Path) -> dict:
    return json.loads(config_path.read_text(encoding="utf-8"))


def normalize_schedule(raw: dict) -> dict:
    schedule = raw.get("schedule", {})
    mode = schedule.get("mode", "daily")

    if mode not in {"daily", "debug"}:
        raise RuntimeError(f"Unsupported schedule.mode: {mode}")

    if mode == "daily":
        hour = int(schedule.get("hour", DEFAULT_HOUR))
        minute = int(schedule.get("minute", DEFAULT_MINUTE))
        if not (0 <= hour <= 23):
            raise RuntimeError(f"Invalid daily hour: {hour}")
        if not (0 <= minute <= 59):
            raise RuntimeError(f"Invalid daily minute: {minute}")
        return {"mode": "daily", "hour": hour, "minute": minute}

    every_minutes = int(schedule.get("every_minutes", 5))
    if every_minutes <= 0 or every_minutes > 60:
        raise RuntimeError(f"Invalid debug every_minutes: {every_minutes}")

    return {"mode": "debug", "every_minutes": every_minutes}


def write_windows_runner(project_dir: Path, config_path: Path) -> Path:
    runner_path = app_root() / "run_auto_commit.ps1"
    content = textwrap.dedent(
        f"""\
        Set-StrictMode -Version Latest
        $ErrorActionPreference = "Stop"

        $uvDir = Join-Path $env:USERPROFILE ".local\\bin"
        if (Test-Path $uvDir) {{
            $env:Path = "$uvDir;$env:Path"
        }}

        uv run "{project_dir / "auto_commit.py"}" --config "{config_path} --automatic"
        exit $LASTEXITCODE
        """
    )
    ensure_dir(runner_path.parent)
    runner_path.write_text(content, encoding="utf-8")
    return runner_path


def install_windows_task(runner_path: Path, schedule: dict, task_name: str) -> None:
    tr = f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{runner_path}"'

    if schedule["mode"] == "daily":
        st = f'{schedule["hour"]:02d}:{schedule["minute"]:02d}'
        args = [
            "schtasks", "/Create", "/F",
            "/SC", "DAILY",
            "/ST", st,
            "/TN", task_name,
            "/TR", tr,
        ]
    else:
        args = [
            "schtasks", "/Create", "/F",
            "/SC", "MINUTE",
            "/MO", str(schedule["every_minutes"]),
            "/TN", task_name,
            "/TR", tr,
        ]

    cp = run(args)
    if cp.returncode != 0:
        raise RuntimeError(f"schtasks failed:\nSTDOUT:\n{cp.stdout}\nSTDERR:\n{cp.stderr}")


def find_uv() -> str:
    uv = shutil.which("uv")
    if uv:
        return uv

    candidate = Path.home() / ".local" / "bin" / "uv"
    if candidate.exists():
        return str(candidate)

    raise RuntimeError("Could not find uv on PATH or at ~/.local/bin/uv")


def copy_auto_commit_script(project_dir: Path) -> Path:
    src = project_dir / "auto_commit.py"
    dst = app_root() / "auto_commit.py"
    ensure_dir(dst.parent)
    shutil.copy2(src, dst)
    return dst


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"


def make_start_calendar_array(every_minutes: int) -> str:
    entries: list[str] = []
    for minute in range(0, 60, every_minutes):
        entries.append(f'      <dict><key>Minute</key><integer>{minute}</integer></dict>')
    return "<array>\n" + "\n".join(entries) + "\n    </array>"


def write_launch_agent(auto_commit_script: Path, config_path: Path, schedule: dict) -> Path:
    plist_path = launch_agent_path()
    stdout_log = logs_dir() / "auto_commit.out.log"
    stderr_log = logs_dir() / "auto_commit.err.log"
    uv_path = find_uv()

    ensure_dir(plist_path.parent)
    ensure_dir(stdout_log.parent)

    if schedule["mode"] == "daily":
        # Use the same array style that worked in debug mode.
        schedule_block = textwrap.dedent(
            f"""\
            <key>StartCalendarInterval</key>
            <array>
              <dict>
                <key>Hour</key>
                <integer>{schedule["hour"]}</integer>
                <key>Minute</key>
                <integer>{schedule["minute"]}</integer>
              </dict>
            </array>
            """
        )
        run_at_load = "false"
    else:
        schedule_block = (
            "<key>StartCalendarInterval</key>\n"
            + make_start_calendar_array(schedule["every_minutes"])
        )
        run_at_load = "true"

    plist = textwrap.dedent(
        f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
         "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
          <dict>
            <key>Label</key>
            <string>{MACOS_LABEL}</string>

            <key>ProgramArguments</key>
            <array>
            <string>{uv_path}</string>
            <string>run</string>
            <string>{auto_commit_script}</string>
            <string>--config</string>
            <string>{config_path}</string>
            <string>--automatic</string>
            </array>

            <key>WorkingDirectory</key>
            <string>{app_root()}</string>

            <key>EnvironmentVariables</key>
            <dict>
              <key>PATH</key>
              <string>{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
            </dict>

            {schedule_block}
            <key>StandardOutPath</key>
            <string>{stdout_log}</string>

            <key>StandardErrorPath</key>
            <string>{stderr_log}</string>

            <key>RunAtLoad</key>
            <{run_at_load}/>
          </dict>
        </plist>
        """
    )
    plist_path.write_text(plist, encoding="utf-8")
    plist_path.chmod(0o644)
    return plist_path


def lint_plist(plist_path: Path) -> None:
    cp = run(["plutil", "-lint", str(plist_path)])
    if cp.returncode != 0:
        raise RuntimeError(f"plutil lint failed:\nSTDOUT:\n{cp.stdout}\nSTDERR:\n{cp.stderr}")


def install_launch_agent(plist_path: Path, schedule: dict) -> None:
    uid = str(os.getuid())
    service = f"gui/{uid}/{MACOS_LABEL}"
    domain = f"gui/{uid}"

    lint_plist(plist_path)

    # Remove old loaded instance if present, but do not disable the service.
    run(["launchctl", "bootout", service])
    run(["launchctl", "bootout", domain, str(plist_path)])

    # Make sure the service is runnable.
    run(["launchctl", "enable", service])

    cp = run(["launchctl", "bootstrap", domain, str(plist_path)])
    if cp.returncode != 0:
        raise RuntimeError(
            f"launchctl bootstrap failed:\nSTDOUT:\n{cp.stdout}\nSTDERR:\n{cp.stderr}"
        )

    if schedule["mode"] == "debug":
        cp = run(["launchctl", "kickstart", "-k", service])
        if cp.returncode != 0:
            raise RuntimeError(
                f"launchctl kickstart failed:\nSTDOUT:\n{cp.stdout}\nSTDERR:\n{cp.stderr}"
            )

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=config_default_path())
    parser.add_argument("--task-name", default=WINDOWS_TASK_NAME)
    parser.add_argument("--write-default-config", action="store_true")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--project-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()

    if args.write_default_config:
        write_default_config(args.config)
        print(f"Wrote default config: {args.config}")
        print("Edit the config, then:")
        print("\tuv run ./setup_auto_commit.py --install")
        return 0

    if not args.install:
        raise RuntimeError("Use --write-default-config or --install")

    if not args.config.exists():
        raise RuntimeError(
            f"Config does not exist: {args.config}\n"
            "Run once with --write-default-config first."
        )

    raw = load_config(args.config)
    jobs = raw["jobs"]
    schedule = normalize_schedule(raw)

    for job in jobs:
        ensure_repo(job)

    ensure_dir(logs_dir())

    if sys.platform == "win32":
        runner = write_windows_runner(args.project_dir, args.config)
        install_windows_task(runner, schedule, args.task_name)
        print(f"Installed Windows scheduled task '{args.task_name}'")
    elif sys.platform == "darwin":
        copied_script = copy_auto_commit_script(args.project_dir)
        plist = write_launch_agent(copied_script, args.config, schedule)
        install_launch_agent(plist, schedule)
        print(f"Installed macOS LaunchAgent '{MACOS_LABEL}'")
        print(f"script: {copied_script}")
        print(f"plist: {plist}")
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")

    print(f"Config: {args.config}")
    print(f"Jobs: {len(jobs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())