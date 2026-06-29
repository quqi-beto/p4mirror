# P4Mirror

**Incremental Perforce to GitHub Migration Framework**

P4Mirror continuously synchronises one Perforce depot path to one GitHub
repository. Every Perforce changelist becomes exactly one Git commit,
preserving author, timestamp, and commit message.

## Architecture

```
Developer
     │
     ▼
Perforce Submit
     │
     ▼
Jenkins Trigger
     │
     ▼
P4Mirror
     │
     ▼
GitHub
```

**Key design principles**

- One Jenkins freestyle job = one GitHub repository.
- Jenkins is responsible only for triggering/scheduling — all migration
  logic lives inside P4Mirror.
- 1 Perforce changelist → 1 Git commit.
- Only configured depot paths are synchronised.
- Migration is incremental, resumable, and safe to rerun.

## Prerequisites

- **Python 3.10+** and **[uv](https://docs.astral.sh/uv/)** package manager.
- **Perforce CLI** (`p4.exe`) on `PATH`.
- **Git CLI** on `PATH`.
- A **Perforce workspace** (client) configured for the depot path being
  mirrored.
- **GitHub credentials** (e.g. GitHub App token) available to the Git CLI
  (configured via credential binding in Jenkins or a Git credential helper).

## Setup

```bash
# Clone or copy the P4Mirror directory
cd D:\Jenkins\ApplicationA

# Install dependencies (creates .venv automatically)
uv sync
```

## Configuration

### `config/repository.json`

```json
{
    "repository_name": "ApplicationA",
    "p4_port": "perforce.company.com:1666",
    "p4_user": "jenkins",
    "p4_client": "jenkins-AppA-mirror",
    "workspace_root": "D:/Jenkins/ApplicationA",
    "github_url": "https://github.com/company/ApplicationA.git",
    "default_branch": "main",
    "sparse_checkout": true,
    "path_mappings": [
        {
            "p4_path": "//RFB/AppA/...",
            "git_path": "AppA"
        },
        {
            "p4_path": "//RFB/AppC/...",
            "git_path": "AppC"
        }
    ]
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `repository_name` | Yes | Logical name for the repository (used in state/logs) |
| `p4_port` | Yes | Perforce server address (`host:port`) |
| `p4_user` | Yes | Perforce username |
| `p4_client` | Yes | Perforce workspace (client) name |
| `workspace_root` | Yes | Local filesystem path for the workspace |
| `github_url` | Yes | Git remote URL |
| `default_branch` | Yes | Branch to push to (e.g. `main`) |
| `sparse_checkout` | No | Enable Git sparse checkout (default: `false`) |
| `path_mappings` | Yes | Array of `{p4_path, git_path}` mappings |

### `config/users.json`

Maps Perforce usernames to Git author identities.

```json
{
    "john": {
        "name": "John Smith",
        "email": "john.smith@company.com"
    },
    "mary": {
        "name": "Mary Jones",
        "email": "mary.jones@company.com"
    }
}
```

Unmapped users fall back to their Perforce username and an email fetched
via `p4 user -o`.

## Usage

```bash
# Basic run
uv run python migrate.py

# With custom config and build number
uv run python migrate.py --config config/my_repo.json --build-number 1234
```

## Jenkins Job Setup

**Job type:** Freestyle project

**Build trigger:** Poll SCM or Perforce trigger

**Build step** — Execute Windows batch command:

```batch
@echo off
cd /d D:\Jenkins\ApplicationA
uv run python migrate.py
```

No Pipeline script required.

## How It Works

Every execution follows this workflow:

1. Load configuration and user mappings.
2. Validate the workspace directory exists.
3. Initialise Git repository (if first run).
4. Set up sparse checkout (if enabled).
5. Read the last migrated changelist from `state/state.json`.
6. Fetch and pull latest Git changes (`git pull --ff-only`).
7. Query Perforce for newer changelists affecting configured paths.
8. For each changelist (oldest first):
   - Sync the Perforce workspace to that specific changelist.
   - Stage all changes in Git (`git add -A`).
   - Create a Git commit with the original author, date, and message.
9. Push all commits to GitHub.
10. Update `state/state.json` with the latest migrated changelist.

## State File

```json
{
    "last_migrated_cl": 58321,
    "repository": "ApplicationA",
    "branch": "main",
    "last_run": "2026-06-25T10:15:30+00:00"
}
```

- Stored at `state/state.json`.
- If the file is missing, empty, or contains an invalid changelist number,
  P4Mirror falls back to scanning the Git commit history for the last
  Perforce changelist (by looking for the ``[git-p4: ... change = N]``
  marker in commits that touched the configured sparse-checkout paths).
  If found, the state is reconstructed automatically.  If no matching
  commit exists, migration stops with an error.
- On success, the state is updated so the next run resumes from the next
  changelist.

## Error Recovery

| Scenario | Behaviour |
|----------|-----------|
| Migration fails mid-changelist | Stop immediately. State **not** updated. Completed commits remain. Next run resumes from last saved CL. |
| State file missing or invalid | Fall back to scanning Git history for the last P4 changelist (``[git-p4: ... change = N]``). If found, state is auto-reconstructed. If not, stop with an error. |
| Push fails | Commits exist locally. Next run will attempt push again (after fetching). |

## Adding a New Repository

1. Create a new Perforce workspace for the depot path.
2. Create a new Git repository on GitHub.
3. Copy the P4Mirror directory to a new Jenkins workspace root.
4. Edit `config/repository.json` with the new settings.
5. Set up the `state/state.json` with the starting changelist number.
6. Create a new Jenkins freestyle job pointing to this directory.
7. No Python code changes required.

## Project Layout

```
P4Mirror/
├── migrate.py                # Entry point
├── config.py                 # Configuration loader
├── config/
│   ├── repository.json       # Repository settings
│   └── users.json            # P4 → Git author mapping
├── core/
│   ├── __init__.py
│   ├── changelist.py         # Changelist data model
│   ├── git_client.py         # Git CLI wrapper
│   ├── logger.py             # Timestamped logging
│   ├── migration.py          # Orchestration logic
│   ├── p4_client.py          # Perforce CLI wrapper
│   ├── state_manager.py      # State file read/write
│   └── workspace.py          # Workspace operations
├── state/
│   └── state.json            # Migration state (auto-generated)
├── logs/                     # Run log files (auto-generated)
├── temp/
├── pyproject.toml
└── README.md
```
