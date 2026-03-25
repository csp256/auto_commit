# auto_commit

Automated remote backups of folders to git

## Requirements

Assumes that you either have `auto_lfs` installed, or you will not be attempting to backup any files that require `git lfs`.

## Installation (Windows)

1. Run:

> .\bootstrap_windows.ps1

2. Edit:

> %LOCALAPPDATA%\auto_commit\config.json

3. Run:

> uv run .\setup_auto_commit.py

## Installation (MacOS)

1. Run:

> ./bootstrap_macos.sh

2. Edit:

> ~/.config/auto_commit/config.json

3. Run:

> uv run ./setup_auto_commit.py