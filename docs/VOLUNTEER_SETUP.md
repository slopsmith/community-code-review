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
| Docker Desktop | Look for the Docker whale icon in your system tray (taskbar) | Install from [docker.com](https://docs.docker.com/get-docker/) — enable **WSL 2 backend** during setup on Windows |
| Compatible GPU | Open **Task Manager → Performance** tab — look for "GPU" | NVIDIA, AMD, Intel Arc, or any GPU with Vulkan drivers |
| 19GB+ free disk | Check **This PC** in File Explorer | For the model file (~19GB) |
| 6GB+ free disk | Check **This PC** in File Explorer | For Docker Desktop image storage (~6GB) |
| 8GB+ VRAM (dedicated or shared) | Task Manager → Performance → GPU memory | Required for smooth performance; less VRAM just means slower inference |

### Which GPU backend should I use?

The right image tag depends on your hardware. Check which backend matches your setup:

| Backend | Tag | When to use |
|---------|-----|-------------|
| **NVIDIA CUDA** | `ghcr.io/slopsmith/volunteer:cuda` (also `:latest`) | NVIDIA GeForce RTX, GTX, Quadro, Tesla (most common) |
| **AMD ROCm** | `ghcr.io/slopsmith/volunteer:rocm` | AMD Radeon RX 7000+, Instinct |
| **Vulkan** | `ghcr.io/slopsmith/volunteer:vulkan` | Any GPU: NVIDIA, AMD (older cards), Intel Arc, Apple via MoltenVK |
| **Intel SYCL** | `ghcr.io/slopsmith/volunteer:intel` | Intel Arc, Iris Xe, built-in GPUs |

If you're not sure, start with `cuda` for NVIDIA or `vulkan` for everything else. Both can be run side by side, so you can switch if one performs better.


## Quick Start

### Step 1: Create a folder for the model

In File Explorer, create a folder called `code-review-models` — for example at `C:\code-review-models`. The drive needs about 19GB of free space for the model file.

### Step 2: Open Docker Desktop's terminal and pull the image

1. Open **Docker Desktop** (double-click the whale icon in your system tray)
2. At the bottom-right of the Docker Desktop window, click the **terminal icon**
3. In the terminal that opens, paste this command and press Enter:

```
docker pull ghcr.io/slopsmith/volunteer:latest
```

If you have an AMD or Intel GPU, replace `latest` with the matching tag from the table above (e.g. `rocm`, `vulkan`, or `intel`).

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

> **No GPU?** Leave the GPU settings as-is. The container detects your GPU automatically and offloads as much as it can.

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

## GPU Awareness — Gaming & Other GPU Work

The volunteer **automatically detects** when your GPU is busy with other work and steps aside. You don't need to do anything.

### What you'll see in the logs

**When you start a game or other GPU-heavy app:**
```
🎮 Looks like something else is using the GPU... I'll stop accepting work until things quiet down.
```

**When a review is already in progress when your GPU gets busy:**
```
🎮 Looks like the GPU just got busy, but I'm in the middle of handling something. I'll finish that up, then stop accepting work until things quiet down. Should be ~60s. You might see the GPU stutter until then.
```

**When your GPU is free again:**
```
✅ GPU is quiet again — ready to accept review requests!
```

### How it works

The container monitors your GPU utilization every 5 seconds. If your GPU is busy (above ~70% utilization or ~85% memory usage), the volunteer **unloads the model from VRAM** to free up space for your game or other work. The coordinator stops sending new review requests to you.

When your GPU quiets down again, the volunteer returns to the `unloaded` state — connected and ready, but with no model in memory. The next review request that comes in will trigger a reload (30–60 seconds). This way your GPU is completely free when you're using it, and only occupied when there's actual review work to do.

**You don't need to stop the container before gaming.** Just launch your game — the volunteer will detect it, unload the model, and free your VRAM automatically.

## The Model File

The first time you run the container, it downloads a ~19GB model file to your shared folder (e.g., `C:\code-review-models`). The container also creates a `README.md` in that folder explaining what the file is.

### If you need disk space temporarily

You can safely delete the model file when the container is stopped. It will be re-downloaded automatically the next time the container starts. Just remember that re-downloading takes time and bandwidth.

### If you delete the container or Docker

The model file stays in your shared folder (`C:\code-review-models` or wherever you created it). If you're cleaning up disk space and wondering what that large file is, the `README.md` in the same folder should help jog your memory.

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
MSYS_NO_PATHCONV=1 docker run -d \
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
| "no GPU detected" | Docker Desktop not using WSL 2, missing GPU drivers, or wrong image tag | Check Docker Desktop → Settings → Resources → WSL Integration is enabled; ensure GPU drivers are up to date; verify you're using the correct image tag for your GPU (`cuda`, `rocm`, `vulkan`, or `intel`) |
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

**Q: Will this slow down my computer or interfere with gaming?**
A: The container automatically detects when your GPU is busy with other work (gaming, rendering, etc.) and **unloads the model from VRAM** to free up space. It will finish any review already in progress, then unload and step aside until your GPU is free again. You don't need to manually pause or stop anything — just launch your game and the volunteer will handle the rest.

**Q: What happens if I start a game while a review is running?**
A: The volunteer will finish the current review, then **unload the model from VRAM** and stop accepting new work until your GPU quiets down. You might see a brief GPU stutter while the review wraps up (usually 30–60 seconds). After that, your GPU and VRAM are completely free. When gaming ends, the volunteer will be ready to reload the model on the next review request.

**Q: Does the coordinator have any access to my machine?**
A: No. The coordinator only knows about your `VOLUNTEER_ID` and GPU info. All communication is your container reaching out to check in.

**Q: How much bandwidth does this use?**
A: The initial model download is ~19GB. After that, only small JSON payloads for reviews — typically a few KB per review.

**Q: How do I check if my GPU is working with Docker?**
A. The command depends on your GPU:

- **NVIDIA:** Run `docker run --rm --gpus all nvidia/cuda:12.0-base nvidia-smi`
- **AMD/ROCm:** Run `docker run --rm --device /dev/kfd --device /dev/dri rocm/pytorch rocm-smi`
- **Vulkan/Intel:** Start the volunteer container and check the startup logs for GPU detection

If you see GPU info, it's working. If you get an error, make sure Docker Desktop is using the WSL 2 backend and your GPU drivers are up to date.

**Q: Can I run a smaller/bigger model to better suit my hardware?**
A. Not yet, let's get this working on the target RTX 3060 spec before we make it adjustable.