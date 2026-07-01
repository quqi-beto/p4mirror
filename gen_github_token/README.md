# gen-github-token

Generate a GitHub App **installation access token** (`ghs_...`) for testing
[p4mirror](https://github.com/your-org/p4mirror) locally, without Jenkins.

## Prerequisites

- Python 3.10+ and **[uv](https://docs.astral.sh/uv/)** package manager.
- A **GitHub App** with:
  - **App ID** — numeric ID found in your GitHub App settings.
  - **Private key** (`.pem` file) — generated in your GitHub App settings.
  - **Installation ID** — the ID of the org/repo installation.

## Setup

```bash
cd gen_github_token
uv sync
```

## Usage

### CLI flags

```bash
uv run python gen_github_token.py ^
    --app-id 123456 ^
    --private-key app.private-key.pem ^
    --installation-id 789012
```

### Environment variables

```bash
set GITHUB_APP_ID=123456
set GITHUB_PRIVATE_KEY_PATH=app.private-key.pem
set GITHUB_INSTALLATION_ID=789012
uv run python gen_github_token.py
```

The script prints **only the token** to stdout — ready to capture or pipe:

```bash
for /f "tokens=*" %i in ('uv run python gen_github_token.py --app-id 123456 --private-key app.private-key.pem --installation-id 789012') do set GITHUB_TOKEN=%i
```

## Use with p4mirror

```bash
set GITHUB_TOKEN=ghs_xxxxxxxxxxxx
uv run python p4mirror/migrate.py init
# or
uv run python p4mirror/migrate.py migrate
```

## Cleanup

Once you've integrated p4mirror with Jenkins, delete this folder — it's no
longer needed.

```bash
rmdir /s gen_github_token
```
