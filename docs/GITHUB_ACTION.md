# GitHub Action Setup

## Overview

We need to add a GitHub Actions workflow to **each repository** in your organization that should get automated code reviews.

The workflow:

1. Triggers on PRs (opened, synchronized, reopened)
2. Checks out code with full git history
3. Installs `ocr` (Alibaba OpenCodeReview)
4. Configures it to point at the coordinator
5. Runs the review
6. Posts inline comments on the PR

## Prerequisites (Leader Must Complete First)

Before this workflow works, the leader must have:

- [ ] **Self-hosted runner** registered at the organization level with labels `self-hosted, linux, x64`
- [ ] **Coordinator** running as part of the `docker compose` stack

That's it. The workflow hardcodes the coordinator URL (`http://coordinator:8080`) and model (`qwen3-30b-a3b`). If you need a different model, add an organization secret `OCR_LLM_MODEL`.

## Workflow File

The source workflow file is at [`workflows/ocr-review.yml`](../workflows/ocr-review.yml) in this repository — that's the canonical version. It's deployed to target repos via the **Deploy OCR to Repositories** workflow (see [Leader Setup](LEADER_SETUP.md)).

It triggers on PRs (opened, synchronized, reopened) and on comments containing `/open-code-review` or `@open-code-review`.
