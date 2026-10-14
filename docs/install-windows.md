# Installing on Windows

This guide covers Windows 10 (22H2) and Windows 11, 64-bit. There are two ways to run the copilot:

- **A. Docker Desktop (recommended).** Runs the API, web UI, Qdrant, Redis and Ollama in containers.
- **B. Native Python.** No Docker. It uses Ollama for Windows and keeps the vector index and cache in
  memory.

Both serve the same web UI. Open the port in the Windows firewall (step 6) only if other machines
need to reach it.

## 1. Check the hardware

| | Minimum | Recommended |
|---|---|---|
| RAM | 16 GB | 32 GB |
| Free disk | 25 GB | 40 GB |
| GPU (optional) | NVIDIA, 8 GB VRAM | NVIDIA, 8 GB+ VRAM |
| CPU | 4 cores with virtualization (VT-x / AMD-V) | 8 cores |

Without a GPU the LLM runs on the CPU. That works, but an answer takes tens of seconds instead of a
few.

## 2. Install the prerequisites

Run these in **PowerShell as Administrator** (Start menu → type *PowerShell* → *Run as
administrator*).

### 2.1 Turn on WSL 2 (Docker Desktop needs it)

```powershell
wsl --install
wsl --update
```

Restart Windows when asked. If `wsl --install` says virtualization is disabled, enable
**Intel VT-x** or **AMD SVM** in your BIOS/UEFI settings and try again.

### 2.2 Install the tools

Use `winget` (built into Windows 10/11), or download each installer from the linked site.

```powershell
winget install -e --id Git.Git                 # https://git-scm.com/download/win
winget install -e --id Python.Python.3.12      # https://www.python.org/downloads/windows/
winget install -e --id Docker.DockerDesktop    # path A: https://www.docker.com/products/docker-desktop/
winget install -e --id Ollama.Ollama           # path B only: https://ollama.com/download/windows
winget install -e --id astral-sh.uv            # path B only: https://docs.astral.sh/uv/
```

Close and reopen PowerShell afterwards so the new programs are on `PATH`. Then check them:

```powershell
git --version
python --version          # 3.11 or newer
docker --version          # path A
docker compose version    # path A
```

If `python` opens the Microsoft Store instead, go to **Settings → Apps → Advanced app settings → App
execution aliases**, turn off the two *python* aliases, and reopen PowerShell. You can also use
`py` instead of `python` everywhere in this guide.

### 2.3 Configure Docker Desktop (path A)

1. Start **Docker Desktop** and wait until the bottom-left corner says **Engine running**.
2. Open **Settings → General** and make sure **Use the WSL 2 based engine** is checked.
3. Optional: limit how much RAM WSL 2 may use so Windows stays responsive. Create
   `%UserProfile%\.wslconfig` with the following, then run `wsl --shutdown` and restart Docker
   Desktop:

   ```ini
   [wsl2]
   memory=12GB
   processors=6
   ```

### 2.4 NVIDIA GPU (optional)

1. Install the latest **NVIDIA Game Ready or Studio driver** from
   https://www.nvidia.com/Download/index.aspx. Do not install a Linux driver inside WSL; the Windows
   driver covers both.
2. Check that Docker can see the GPU:

   ```powershell
   docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
   ```

   You should see your GPU listed. If you see an error, update the driver, run `wsl --update`, and
   restart Docker Desktop.

## 3. Get the code

```powershell
cd $HOME
git clone https://github.com/samirdash4u/knowledge-copilot.git
cd knowledge-copilot
```

The repository's `.gitattributes` keeps Unix line endings, so the Linux containers and shell scripts
work even if Git is set to convert line endings.

## 4. Configure

```powershell
python scripts\manage.py setup
notepad .env
```

`setup` copies `.env.example` to `.env` and fills in a random `COPILOT_JWT_SECRET`. The settings
you're most likely to change:

| Setting | Meaning |
|---|---|
| `COPILOT_HOST` | Address the web UI listens on. `0.0.0.0` means all network interfaces (reachable from other machines once the firewall allows it). `127.0.0.1` means this PC only. You can also give one specific IP of this PC, for example `192.168.1.20`. |
| `COPILOT_PORT` | Port for the web UI, `8000` by default. Change it if 8000 is already taken. |
| `COPILOT_GPU` | `true` to run the LLM on the NVIDIA GPU (path A). |
| `COPILOT_DEMO_PASSWORD` | Initial password of the demo users alice, bob and carol. |
| `COPILOT_OLLAMA_MODEL` | The LLM. The default `qwen2.5:7b-instruct-q4_K_M` fits an 8 GB GPU. |

To find this PC's IP address, run `ipconfig` and look for **IPv4 Address** under your Wi-Fi or
Ethernet adapter.

## 5A. Run with Docker Desktop

```powershell
python scripts\manage.py up            # add --gpu, or set COPILOT_GPU=true, for the GPU
python scripts\manage.py pull-model    # downloads the LLM, about 4.7 GB
python scripts\manage.py ingest        # downloads embedding models on first run, then indexes
python scripts\manage.py status        # prints the URLs to open
```

The first `up` builds the image, which takes several minutes. Later starts take seconds, and the
containers restart automatically with Docker Desktop. Other commands:

```powershell
python scripts\manage.py logs     # follow the API log (Ctrl+C to stop following)
python scripts\manage.py down     # stop everything; users, index and models are kept
python scripts\manage.py eval     # run the evaluation set, report in reports\
```

## 5B. Run natively (no Docker)

1. Start **Ollama** from the Start menu (it runs in the system tray), then download the model:

   ```powershell
   ollama pull qwen2.5:7b-instruct-q4_K_M
   ```

2. Create the Python environment. If activation fails with *running scripts is disabled*, run
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and try again.

   ```powershell
   uv venv
   .venv\Scripts\Activate.ps1
   uv pip install -e ".[ml,dev]"
   ```

3. Add these lines to `.env` so Qdrant and Redis aren't needed:

   ```ini
   COPILOT_VECTOR_STORE=memory
   COPILOT_CACHE=memory
   ```

4. Index the documents and start the server. Host and port come from `.env`, and the flags override
   them:

   ```powershell
   copilot ingest
   copilot serve                                  # uses COPILOT_HOST / COPILOT_PORT
   copilot serve --host 0.0.0.0 --port 8080       # or override on the command line
   ```

   The server prints the URLs it can be reached on. Press Ctrl+C to stop it.

## 6. Open the port in Windows Firewall

You only need this when other machines should reach the UI, which means `COPILOT_HOST` is `0.0.0.0`
or this PC's IP address. Run in **PowerShell as Administrator**, using your `COPILOT_PORT`:

```powershell
New-NetFirewallRule -DisplayName "Knowledge Copilot (TCP 8000)" `
  -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
```

`-Profile Private` allows connections only on networks Windows treats as private (home or office).
Check how your network is classified:

```powershell
Get-NetConnectionProfile
# if NetworkCategory is Public on a network you trust:
Set-NetConnectionProfile -InterfaceAlias "Wi-Fi" -NetworkCategory Private
```

Avoid opening the port on the *Public* profile. Everyone on a café or hotel network could then reach
the login page.

Windows may also show a pop-up the first time the server starts, asking to let `com.docker.backend`
(path A) or `python.exe` (path B) through the firewall. Allow it on **Private networks** only.

To check from another machine: `Test-NetConnection <this-pc-ip> -Port 8000` (Windows) or
`curl http://<this-pc-ip>:8000/health` (any OS) should succeed.

To remove the rule later:

```powershell
Remove-NetFirewallRule -DisplayName "Knowledge Copilot (TCP 8000)"
```

## 7. Sign in

Open the URL printed by `status` (or by `copilot serve`), for example `http://localhost:8000` or
`http://192.168.1.20:8000`. Sign in as `alice`, `bob` or `carol` with `COPILOT_DEMO_PASSWORD`, then
set a new password. To add accounts:

```powershell
# path A
docker compose exec api copilot users add dave --role manager
# path B (with .venv activated)
copilot users add dave --role manager
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `make` is not recognized | Windows has no `make`. Use `python scripts\manage.py <command>` instead. |
| `docker` is not recognized | Reopen PowerShell after installing Docker Desktop. Check that `C:\Program Files\Docker\Docker\resources\bin` is on `PATH`. |
| *Docker daemon is not reachable* / `error during connect ... docker_engine` | Start Docker Desktop and wait for **Engine running**. |
| *WSL 2 installation is incomplete* | Run `wsl --update` as Administrator, then restart. |
| *port is already allocated* / *address already in use* | Something else uses the port. Find it with `netstat -ano \| findstr :8000`, or set another `COPILOT_PORT`. |
| *could not select device driver "nvidia"* | The GPU isn't available to Docker. Follow 2.4, or run without `--gpu`. |
| Other machines can't connect | Check `COPILOT_HOST` isn't `127.0.0.1`, add the firewall rule from step 6, and make sure the network is *Private*. Some VPN clients block LAN access. |
| The first answer is very slow | The model loads into memory on the first question. Later answers are faster. Without a GPU, each answer takes tens of seconds. |
| `scripts/demo/*.sh` don't run | They are bash scripts. Run them from Git Bash or WSL, and set `COPILOT_DEV_TOKEN_ENDPOINT=true` with `COPILOT_HOST=127.0.0.1` first. The web UI does the same thing without them. |
