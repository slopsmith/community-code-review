# Volunteer Setup Guide

Thank you for allowing the use of your hardware by our community code review network!

You'll run a **Docker container** on your machine that uses your GPU to help review pull requests. The container:

- ✅ Has access to **only** your GPU and a folder you choose
- ✅ Makes **only outbound** connections to the code review coordinator
- ✅ **Downloads and runs the needed model, and joins our hardware pool automatically**
- ✅ **Works behind NAT, home routers, and firewalls** — no port forwarding needed
- ❌ **Cannot** access your files, network, or system
- ❌ **Never** accepts inbound connections from the internet

## Prerequisites

| Requirement | How to Check | Details |
|-------------|--------------|---------|
| Windows OS (default) | You know what OS you're running, right? | (Mac/Linux users: Follow the instructions for Terminal below) |
| Docker Desktop | Look for the Docker whale icon in your system tray (taskbar) | Install from [docker.com](https://docs.docker.com/get-docker/) — enable **WSL 2 backend** during setup on Windows |
| NVIDIA GPU | Open **Task Manager → Performance** tab — look for "GPU" | Install latest NVIDIA drivers from [nvidia.com/drivers](https://www.nvidia.com/drivers) |
| 13GB+ free disk | Check **This PC** in File Explorer | For the model file (~7GB) + Docker image |
| GPU VRAM: 8GB+ dedicated or 16GB+ shared | Task Manager → Performance → GPU "Dedicated/Shared GPU memory" | Required for smooth performance |


## Quick Start

### Step 1: Create a folder for the model

In File Explorer, create a folder called `code-review-models` — for example at `C:\code-review-models`. The drive needs about 12GB of free space for the model.

### Step 2: Open Docker Desktop's terminal and pull the image

1. Open **Docker Desktop** (double-click the whale icon in your system tray)
2. At the bottom-right of the Docker Desktop window, click the **terminal icon**
3. In the terminal that opens, paste this command and press Enter:

```
docker pull ghcr.io/slopsmith/volunteer:latest
```

Wait for the download to finish — you'll see progress in the terminal.

### Step 3: Create and run the container (Docker Desktop GUI)

1. In Docker Desktop, go to the **Images** tab
2. Find `ghcr.io/slopsmith/volunteer:latest` and click the **Run** button (the play icon)
3. A dialog titled **"Run a new container"** will open. Click **Optional Settings** to expand it
4. Fill in the dialog:

   | Field | Value |
   |-------|-------|
   | **Container name** | `code-review-volunteer` |
   | **Ports** | leave empty |
   | **Volumes** | click **Add Volume** → set Host Path to the directory you created in Step 1 and Container Path to `/models` |
   | **Env Variables** | add each one below |

   | Variable | Value |
   |----------|-------|
   | `COORDINATOR_URL` | `<coordinator-url>` |
   | `VOLUNTEER_ID` | `<your-name>` |
   | `VOLUNTEER_SECRET` | `<coordinator-secret>` |

5. Click **Run**

The container will start and appear in the **Containers** tab with a green "Running" indicator.

> **No GPU?** Leave the GPU settings as-is. The container detects your GPU automatically. If you don't have one, add `GPU_DEVICES` with value `none` in the environment variables section.

## What You'll See

After running the container, go to **Docker Desktop → Containers** tab and click on `code-review-volunteer`. The logs will show something like:

```text
╔══════════════════════════════════════════════════════════════╗
║         Community Code Review Volunteer                      ║
╚══════════════════════════════════════════════════════════════╝

  Volunteer ID:     alice-pc-12345
  Coordinator:      <coordinator-url>
  Model:            Qwen3-30B-A3B-Q4_K_M.gguf
  Context Size:     32768

  GPU(s) detected:
    ◦ NVIDIA GeForce RTX 4090, 24564 MiB
  → Auto-selected GPU 0 (default). (Set GPU_DEVICES to override.)
  GPU Layers:       99

  ✓ Model file found: /models/Qwen3-30B-A3B-Q4_K_M.gguf
  Model size: 6.8Gi

  Starting llama-server...
  ✓ Server ready! (2s)

  Starting WebSocket agent...
  Agent started (PID: 42)

╔══════════════════════════════════════════════════════════════╗
║  ✅ Volunteer is running — waiting for review requests...   ║
╚══════════════════════════════════════════════════════════════╝
```

When a review comes in, you'll see a log message like:

```
📥 Received a code review request — running inference...
✅ Review complete — result sent back to coordinator
```

## Understanding the Options

### `--gpus all`

Gives the container access to your GPU. Without this flag, the container won't see your GPU at all. If you have multiple GPUs, the container auto-selects the first one by default. You can change this behavior with `GPU_DEVICES`; See below.

### `-v C:\code-review-models:/models`

This shares a folder on your Windows machine so the model file persists between runs. Create the folder in File Explorer first, then use the path `C:\code-review-models` or wherever you put it.

### `COORDINATOR_URL`

The web address of the coordinator (looks like `https://something.ts.net`). Provided by whoever is running this.

### `VOLUNTEER_ID`

A friendly name so the team knows who's helping. Defaults to your computer's hostname. Better to set it to the name by which you're known in the community (eg GitHub name, Discord handle).

### `VOLUNTEER_SECRET`

The coordinator requires authentication, so you must set this — the coordinator will reject your connection without the correct value.

## Advanced: Custom Model

If the coordinator asks you to use a different model, make sure Git for Windows is installed, open a Bash terminal, and run:

```bash
docker run -d \
  --name code-review-volunteer \
  --gpus all \
  -v /c/Users/YourName/code-review-models:/models \
  -e COORDINATOR_URL="<coordinator-url>" \
  -e MODEL_REPO="Qwen/Qwen3-30B-A3B-GGUF" \
  -e MODEL_FILE="Qwen3-30B-A3B-Q4_K_M.gguf" \
  ghcr.io/slopsmith/volunteer:latest
```

Or from a direct download URL:

```bash
  -e MODEL_URL="https://huggingface.co/Qwen/Qwen3-30B-A3B-GGUF/resolve/main/Qwen3-30B-A3B-Q4_K_M.gguf?download=true"
```

### `GPU_DEVICES` (optional)

Controls which GPU to use when you have multiple GPUs. By default the container auto-selects the first available GPU.

- `-e GPU_DEVICES="all"` — use all GPUs
- `-e GPU_DEVICES="0,1"` — use specific GPUs by index

Most volunteers won't need to set this.

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| "no NVIDIA GPU detected" | Docker Desktop not using WSL 2, or no NVIDIA driver | Check Docker Desktop → Settings → Resources → WSL Integration is enabled; update GPU drivers from nvidia.com |
| "Coordinator not reachable" | Wrong URL or coordinator is down | Check the coordinator URL is correct and the server is running |
| "Registration rejected" | Wrong VOLUNTEER_SECRET | Ask the coordinator for the correct secret |
| "Model download failed" | Network issue or wrong URL | Check MODEL_URL is correct |
| Slow reviews | Under-powered machine | Every bit helps! Consider lowering `LLAMA_CTX_SIZE=16384` |

## Stopping

In **Docker Desktop**, go to the **Containers** tab, find `code-review-volunteer`, click the **stop** icon. To remove it, click the **delete** icon next to it.

Or in Docker Desktop's terminal:

```bash
docker stop code-review-volunteer
docker rm code-review-volunteer
```

You can stop anytime. The coordinator will detect your absence and route reviews to other volunteers. No hard feelings! 🎉

## Checking Logs

In **Docker Desktop**, go to the **Containers** tab, click on `code-review-volunteer`, then click the **Logs** tab.

Or in Docker Desktop's terminal:

```bash
docker logs -f code-review-volunteer
```

Press `Ctrl+C` to stop following logs (container keeps running).

## FAQ

**Q: Will this slow down my computer?**
A: The container uses your GPU, but only when a PR review is happening. Between reviews, it idles. You can set `LLAMA_N_PARALLEL=1` to limit GPU usage.

**Q: Does the coordinator have any access to my machine?**
A: No. The coordinator only knows about your `VOLUNTEER_ID` and GPU info. All communication is your container reaching out to check in.

**Q: How much bandwidth does this use?**
A: The initial model download is ~7GB. After that, only small JSON payloads for reviews — typically a few KB per review.

**Q: How do I check if my GPU is working with Docker?**
A. In Docker Desktop's terminal (or any terminal), run:

```bash
docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi
```

If you see GPU info, it's working. If you get an error, make sure Docker Desktop is using the WSL 2 backend and your NVIDIA drivers are up to date.

**Q: Can I run a smaller/bigger model to better suit my hardware?**
A. Not yet, let's get this working on the target RTX 3060 spec before we make it adjustable.