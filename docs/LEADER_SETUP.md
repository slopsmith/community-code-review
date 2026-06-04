# Leader Setup Guide

This guide walks through everything the **organization admin** needs to do to get the system running.

## Prerequisites

- GitHub organization admin access
- [Git](https://git-scm.com/downloads) installed (provides Git Bash on Windows)
- [Docker Desktop](https://docs.docker.com/get-docker/) installed
- [Tailscale](https://tailscale.com/download) installed and logged in

## Run the setup script

Open **Git Bash** (on Windows) or a terminal (on macOS/Linux) in the project root and run the setup script:

```bash
./setup.sh
```

The script will:

1. Check that Docker and Tailscale are installed
2. **Generate a volunteer secret** automatically (shared with volunteers later)
3. Ask for your **GitHub organization name** and walk you through creating a PAT with `admin:org` scope
4. Create the `.env` file
5. Start the coordinator and runner with `docker compose up -d`
6. Ask you to enable MagicDNS and HTTPS Certificates in your Tailscale admin console (one-time per account)
7. Run `tailscale funnel 8080` to expose the coordinator to the internet
8. Ask if you want to run a smoke test — builds the volunteer image and verifies the coordinator can see it
9. Print the **Coordinator URL** and **Volunteer Secret** — share these with volunteers

## After the script

### 1. Create a PAT for the deploy workflow

The workflow that distributes code review configuration to repositories needs its own PAT with `repo` and `workflow` scopes (the default `GITHUB_TOKEN` can't access other repos). Create one now:

1. Go to [GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)](https://github.com/settings/tokens)
2. Click **Generate new token (classic)** and give it a name like "deploy-ocr-workflow"
3. Under **Scope**, select:
   - **`repo`** (all) — allows pushing to repositories and creating PRs
   - **`workflow`** — allows updating workflow files in target repos
4. Click **Generate token** and copy it
5. Go to **GitHub → Your Organization → Settings → Secrets and variables → Actions**
6. Add an **organization secret**:

   | Secret | Value |
   |--------|-------|
   | `PAT_WITH_REPO_SCOPE` | The token you just generated |

### 2. Trigger deployment to repositories

Go to the **Actions** tab in this repository and run the **Deploy OCR to Repositories** workflow:

- Leave **repo_name** empty to create PRs for _all_ repos in the org
- Or enter a single repo name (e.g. `community-code-review`) to target just that one

The workflow will create a PR in each target repository adding `.github/workflows/ocr-review.yml`. Merge the PR to enable reviews.

> To test it out, run the workflow with `repo_name` set to `community-code-review` — it will create a PR adding the workflow to this very repository.

### 3. Send instructions to volunteers

Share the link to [`VOLUNTEER_SETUP.md`](./VOLUNTEER_SETUP.md) along with:

- The **Coordinator URL** (shown at the end of the setup script — your Tailscale Funnel URL)
- The **Volunteer Secret** (also shown at the end of the setup script)

## Stopping everything

To stop the coordinator, runner, and Tailscale Funnel:

```bash
./teardown.sh
```

> On Windows, run this in **Git Bash**, not PowerShell or CMD.
