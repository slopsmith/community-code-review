# Community Code Review

> Community-powered AI code reviews for this GitHub organization.

## How It Works

```mermaid
flowchart TB
    subgraph GitHub
        GHOrg["Your GitHub Organization"]
        PR["Pull Request"]
        Runner["Self-Hosted GitHub Runner<br/>(leader's machine)"]
        WF["GitHub Actions Workflow<br/>alibaba/open-code-review"]
    end

    subgraph LeaderMachine ["Leader's Machine"]
        Runner
        Coordinator["Coordinator Container<br/>(HTTP API + WebSocket)"]
    end

    subgraph VolunteerNetwork ["Volunteers (community machines)"]
        V1["Volunteer Container<br/>llama-server + WebSocket Agent"]
        V2["Volunteer Container<br/>llama-server + WebSocket Agent"]
        V3["... more volunteers"]
    end

    PR -->|triggers| WF
    WF -->|runs on| Runner
    Runner -->|ocr review --format json| Coordinator
    Coordinator -->|sends work through WebSocket tunnel| V1
    Coordinator -->|sends work through WebSocket tunnel| V2
    Coordinator -->|sends work through WebSocket tunnel| V3
    V1 -->|results back through WebSocket| Coordinator
    V2 -->|results back through WebSocket| Coordinator
    V3 -->|results back through WebSocket| Coordinator
    Coordinator -->|PR review comments| GHOrg
```

1. A PR is opened in any organization repository.
2. The GitHub Actions workflow (running on the **self-hosted runner**) invokes `ocr review`.
3. `ocr` sends the diff to the **Coordinator** (OpenAI-compatible API endpoint).
4. The Coordinator sends the inference request through a **volunteer's persistent outbound WebSocket tunnel**.
5. The Volunteer runs the model (Qwen3-30B-A3B GGUF) via `llama-server`, returns results through the same tunnel.
6. The Coordinator sends the review back to `ocr`, which posts inline PR comments.

## Repository Structure

```
community-code-review/
├── README.md                    ← This file
├── ARCHITECTURE.md              ← Full architecture & design decisions
├── setup.sh                    ← One-command setup (Git Bash / Linux / macOS)
├── teardown.sh                 ← Cleanup script (Git Bash / Linux / macOS)
├── coordinator/                 ← Coordinator Docker image (relay server)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── server.py
├── volunteer/                   ← Volunteer Docker image (llama-server + agent)
│   ├── Dockerfile
│   ├── entrypoint.sh
│   └── requirements.txt
├── docs/
│   ├── LEADER_SETUP.md          ← How the leader sets everything up
│   ├── VOLUNTEER_SETUP.md       ← How volunteers join the network
│   └── GITHUB_ACTIONS_SETUP.md  ← How to configure the workflow per repo
├── .env.example                ← Template for environment variables
└── docker-compose.yml          ← Orchestrates coordinator + runner
```

## Quick Links

- [Architecture Overview](ARCHITECTURE.md)
- [Leader Setup Guide](docs/LEADER_SETUP.md)
- [Volunteer Setup Guide](docs/VOLUNTEER_SETUP.md)
- [GitHub Actions Configuration](docs/GITHUB_ACTIONS_SETUP.md)

## License

MIT — for the community code review infrastructure.





<!-- test trigger --> ZagatoZee Woz 'ere
