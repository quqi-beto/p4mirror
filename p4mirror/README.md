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
If something about cert went wrong try this: set UV_INSECURE_HOST=github.com

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
        },
        {
            "p4_path": "//REPOSITORY/...",
            "git_path": "REPOSITORY",
            "dynamic": true,
            "baseline_cl": 0
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

Each `path_mappings` entry supports two optional per-mapping fields:

| Field | Default | Description |
|-------|---------|-------------|
| `dynamic` | `false` | Treat the depot path as a *dynamic depot* — watch the whole root, migrate every change with no filter, exclude it from sparse checkout, and commit files **without writing them to the local workspace** (no-checkout staging). Use for depots whose sub-paths are unpredictable (e.g. build artifacts committed under unique paths). |
| `baseline_cl` | `0` | Optional initial changelist baseline used to seed state when no `git-p4` marker is found. Lets you adopt a dynamic depot without backfilling its entire history (set to the current P4 change number at adoption time). `0` means start from the beginning. Only meaningful for `dynamic` mappings. |

### Dynamic depot mappings

A `dynamic` mapping watches a depot **root as a whole** (e.g. `//REPOSITORY/...`) and
migrates *every* incremental change under it with no filtering — no need to
enumerate the sub-paths. This is designed for depots where paths are created
unpredictably (an automated build server committing each artifact under a unique
path, manually added 3rd-party jars, etc.).

How it behaves:

- **Discovery** queries the whole root (`p4 changes //REPOSITORY/...@>{baseline}`),
  so new builds under any path are picked up automatically.
- **Git layout mirrors P4**: a depot file `//REPOSITORY/org/apache/logging/log4j/
  log4j-core/2.1.1/log4j-core-2.1.1.jar` lands at `REPOSITORY/org/apache/logging/
  log4j/log4j-core/2.1.1/log4j-core-2.1.1.jar`, so each build/artifact/version is
  naturally separated in the Git tree.
- **Root mirroring**: set `git_path` to `"/"` to mirror the whole depot to the
  repository root (no prefix folder) — e.g. `//NDPro/Dev/...` → `Dev/...`, and
  any other top-level folder in the depot mirrors alongside it.
- **No local storage cost**: files are fetched via `p4 print`, hashed into Git's
  object database, and staged with `git update-index` — they are **never written
  to the local workspace** and are marked `skip-worktree` so `git checkout`/
  `git reset` can't materialize them either.
- **One commit per changelist**: a CL touching both a regular path and a dynamic
  path still produces exactly one Git commit.
- **Single baseline per mapping**: the whole dynamic root tracks one
  `last_migrated_cl` in the state file.

> **Note:** `sparse_checkout` must stay enabled (`true`) for the no-materialization
> guarantee to hold. Dynamic paths are automatically excluded from the sparse cone.

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

## GitHub Authentication

P4Mirror authenticates with GitHub by embedding a token into the Git remote
URL (``https://x-access-token:{token}@github.com/...``).

**Fresh tokens are always used — cached/stale tokens are never reused:**

- A new installation access token (``ghs_...``) is minted at the start of
  **every** ``init`` and ``migrate`` run.
- A **second fresh token** is minted immediately before the final
  ``git push``, because processing many changelists can take longer than
  the 1-hour lifetime of an installation token — the token minted at the
  start of the run would otherwise be invalid by push time.

### Recommended: GitHub App credentials (fresh token per run)

Supply the GitHub App **App ID**, **installation ID** and the private key
PEM path — each can come from a CLI argument **or** an environment
variable (CLI wins, exactly like ``--github-token`` / ``GH_TOKEN``):

| Credential | CLI flag | Environment variable |
|------------|----------|----------------------|
| App ID | ``--app-id`` | ``GITHUB_APP_ID`` |
| Installation ID | ``--installation-id`` | ``GITHUB_INSTALLATION_ID`` |
| Private key path | ``--private-key`` | ``GITHUB_PRIVATE_KEY_PATH`` |

```bash
# Via CLI arguments (typical for Jenkins)
uv run python migrate.py init \
    --app-id 123456 \
    --installation-id 789012 \
    --private-key "%PRIVATE_KEY_PATH%"

# Via environment variables
set GITHUB_APP_ID=123456
set GITHUB_INSTALLATION_ID=789012
set GITHUB_PRIVATE_KEY_PATH=app.private-key.pem
uv run python migrate.py migrate
```

When the private key is available (via ``--private-key`` or
``GITHUB_PRIVATE_KEY_PATH``), a **fresh** token is minted for every run
(and again before the push); the ``--github-token`` flag and the
``GITHUB_TOKEN`` / ``GH_TOKEN`` environment variables are ignored.

### Alternative: a pre-generated token (backward compatible)

```bash
# Via CLI flag
uv run python migrate.py init --github-token "ghs_xxxxxxxxxxxx"

# Via environment variable
set GITHUB_TOKEN=ghs_xxxxxxxxxxxx
uv run python migrate.py init
```

The token is required whenever P4Mirror needs to fetch from or push to
GitHub — i.e. both ``init`` and ``migrate`` commands.

If **no** credential source is configured (no private key, no
``--github-token``, and no ``GH_TOKEN`` / ``GITHUB_TOKEN``), the app fails
fast with a clear ``Configuration error: no GitHub credential source
configured ...`` message instead of dying later at a git/GitHub call.

## Usage

### Step 1 — One-time workspace initialisation

```bash
uv run python migrate.py init
uv run python migrate.py init --config config/my_repo.json
```

This clones the GitHub repository (with sparse checkout for the mapped
paths), scans git-p4 markers to find the last Perforce changelist already
in GitHub, and writes a per-repository state file (``state/state_<repo_name>.json``).  After this step, the workspace
is ready for incremental migration.

### Step 2 — Incremental migration (Jenkins)

```bash
uv run python migrate.py migrate
uv run python migrate.py migrate --config config/my_repo.json --build-number 1234
```

For backward compatibility, running ``python migrate.py`` without a
subcommand is treated as ``python migrate.py migrate``.

**Batch processing** — pass ``--max-cls N`` to process only the first *N*
pending changelists in this run (``0`` or omitted = all of them).  Useful
for spreading a large backlog over several days:

```bash
uv run python migrate.py --max-cls 5      # today: first 5
uv run python migrate.py --max-cls 5      # tomorrow: next 5
```

Each limited run still commits, pushes, and updates the state file, so the
next run resumes automatically from the last processed changelist.

## Jenkins Job Setup

**Job type:** Freestyle project

**Build trigger:** Poll SCM or Perforce trigger

**GitHub App private key:** add the private key PEM as a **secret file**
credential (e.g. `github-app-key`).

**Build step** — Execute Windows batch command. Bind the secret file
credential to an environment variable (e.g. `PRIVATE_KEY_PATH`) and the
App ID / installation ID to environment variables (e.g. from Jenkins build
parameters), so a **fresh** token is minted on every run.

With env vars set, no flags are needed:

```batch
@echo off
cd /d D:\Jenkins\ApplicationA
set GITHUB_APP_ID=%GITHUB_APP_ID%
set GITHUB_INSTALLATION_ID=%GITHUB_INSTALLATION_ID%
set GITHUB_PRIVATE_KEY_PATH=%PRIVATE_KEY_PATH%
uv run python migrate.py migrate
```

Or pass everything explicitly:

```batch
@echo off
cd /d D:\Jenkins\ApplicationA
uv run python migrate.py migrate ^
    --app-id %GITHUB_APP_ID% ^
    --installation-id %GITHUB_INSTALLATION_ID% ^
    --private-key "%PRIVATE_KEY_PATH%"
```

For the one-time initialisation, use `init` instead of `migrate` (same
credentials).

No Pipeline script required.

## Workspace Initialisation (`init`)

On a fresh setup, the workspace has no Git history and no per-repository state file.
The ``init`` command bootstraps it:

1. Create the workspace directory (if missing).
2. Initialise a Git repository with ``origin`` pointing to GitHub.
3. Configure sparse checkout for the mapped paths (e.g. ``AppA``, ``AppC``).
4. Fetch the default branch from GitHub using a **partial clone**
   (``--filter=blob:none``) — only blob objects for sparse paths are
   downloaded, keeping the clone fast and small.
5. Checkout the branch.
6. Scan Git history for ``[git-p4: ... change = N]`` markers to find the
   last Perforce changelist already in GitHub — **per gitPath**.  Each
   path is scanned independently so different paths can have different
   baselines (e.g. AppA at CL 1003, AppC at CL 1001).  Paths without a
   git-p4 marker start from CL 0.
7. Write ``state/state_<repo_name>.json`` with per-path baselines.

After ``init`` completes, the workspace is ready for incremental
migration.  No manual editing of the state file is required.

## Incremental Migration (`migrate`)

Every execution follows this workflow:

1. Load configuration and user mappings.
2. Validate the workspace directory exists.
3. Initialise Git repository (if first run).
4. Set up sparse checkout (if enabled).
5. Read per-path baselines from the state file
   (e.g. `state/state_ApplicationA.json`).  Each gitPath tracks its own
   `last_migrated_cl` so paths can progress independently.
6. Force-sync local workspace to remote origin (`git fetch origin && git reset --hard origin/{branch} && git clean -fd`).
7. Query Perforce for newer changelists — **each gitPath queries from its
   own baseline** (e.g. AppA queries `//RFB/AppA/...@>{baseline}`, AppC
   queries `//RFB/AppC/...@>{baseline}`).  Results are unioned and sorted
   oldest-first.
8. For each changelist (oldest first):
   - Fetch changelist details to determine which gitPaths are affected.
   - **Sync only the affected depot paths** to that changelist
     (e.g. `p4 sync //RFB/AppA/...@{cl}`).
   - Stage all changes in Git (`git add -A`).
   - Create a Git commit with the original author, date, and message.
   - Track per-path progress (which gitPath reached which CL).
9. Push all commits to GitHub.
10. Update the per-repository state file — each gitPath's `last_migrated_cl`
    is updated independently based on the changelists that affected it.

## State File

Each gitPath within a repository tracks its own `last_migrated_cl`, stored
in a single per-repository JSON file.

```json
{
    "paths": {
        "AppA": { "last_migrated_cl": 58321 },
        "AppC": { "last_migrated_cl": 58100 }
    },
    "repository": "ApplicationA",
    "branch": "main",
    "last_run": "2026-07-10T10:15:30+00:00"
}
```

- Stored per-repository at `state/state_<repository_name>.json` (e.g. `state/state_ApplicationA.json`).
- If the file is missing, empty, or contains an invalid changelist number,
  P4Mirror falls back to scanning the Git commit history for the last
  Perforce changelist (by looking for the ``[git-p4: ... change = N]``
  marker in commits that touched the configured sparse-checkout paths).
  If found, the state is reconstructed **per-path**.  If no matching
  commit exists, migration stops with an error.
- On success, each gitPath's baseline is updated independently so the
  next run resumes from the correct point for each path.
- **Legacy format** (single `last_migrated_cl` field) is auto-converted
  on read and used as a shared baseline across all paths.

## Error Recovery

| Scenario | Behaviour |
|----------|-----------|
| Migration fails mid-changelist | Stop immediately. State **not** updated. Completed commits remain. Next run resumes from last saved per-path CLs. |
| State file missing or invalid | Fall back to scanning Git history for the last P4 changelist (``[git-p4: ... change = N]``) per gitPath. If found, per-path state is auto-reconstructed. If not, stop with an error. |
| Push fails | Commits exist locally. Next run will attempt push again (after fetching). |

## Adding a New Repository

1. Create a new Perforce workspace for the depot path.
2. Create a new Git repository on GitHub (the GitHub team will populate it
   with git-p4 markers in the commit history).
3. Copy the P4Mirror directory to a new Jenkins workspace root.
4. Edit `config/repository.json` with the new settings.
5. Run ``uv run python migrate.py init`` to bootstrap the workspace
   (clone, discover baseline CL, write per-repository state file).
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
│   ├── github_auth.py        # Fresh GitHub App token minting
│   ├── initializer.py        # One-time workspace init
│   ├── logger.py             # Timestamped logging
│   ├── migration.py          # Orchestration logic
│   ├── p4_client.py          # Perforce CLI wrapper
│   ├── state_manager.py      # State file read/write
│   └── workspace.py          # Workspace operations
├── state/
│   └── state_<repo>.json     # Per-repo migration state (auto-generated)
├── logs/                     # Run log files (auto-generated)
├── temp/
├── pyproject.toml
└── README.md
```
