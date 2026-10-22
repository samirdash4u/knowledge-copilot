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

By default almost all of that disk space is used on `C:`. If `C:` is short on space, follow 1.1
before installing anything. If you already ran the copilot and `C:` is full, see 8.1.

### 1.1 Put everything on another drive (optional)

These are the places that grow, how big they get, and the setting that moves each one. The
examples use `D:\copilot`; any folder on any local drive works.

| What | Default location | Size | Moved by |
|---|---|---|---|
| Docker images, containers and volumes (path A: the LLM, embedding models, index, users) | `%LOCALAPPDATA%\Docker\wsl` | 12–20 GB | Docker Desktop's disk image location (2.2, 2.3) |
| Ollama models (path B) | `%USERPROFILE%\.ollama\models` | ~5 GB | `OLLAMA_MODELS` |
| Embedding and reranker models (path B) | `%USERPROFILE%\.cache\huggingface` | ~1.5 GB | `HF_HOME` |
| uv download cache (path B) | `%LOCALAPPDATA%\uv\cache` | 1–3 GB | `UV_CACHE_DIR` |
| Python installs made by uv, if any | `%APPDATA%\uv\python` | ~100 MB each | `UV_PYTHON_INSTALL_DIR` |
| pip download cache | `%LOCALAPPDATA%\pip\Cache` | up to a few GB | `PIP_CACHE_DIR` |
| The project: code, `.venv` (path B), `data\index`, `data\users.db`, `reports` | wherever you clone it | 1–3 GB | the folder you clone into (step 3) |
| WSL Linux distribution (Ubuntu) | `%LOCALAPPDATA%\Packages\...` | 1–2 GB+ | skip it with `--no-distribution` (2.1) |

Docker Desktop, Ollama, Git and Python themselves are a few hundred MB each and can stay on `C:`.

Set the cache and model locations once, in a normal (not Administrator) PowerShell. They are saved
for your Windows user, so every later window and program picks them up:

```powershell
$base = "D:\copilot"
New-Item -ItemType Directory -Force "$base\cache\uv", "$base\cache\pip", "$base\cache\huggingface",
  "$base\uv-python", "$base\ollama\models", "$base\docker" | Out-Null
[Environment]::SetEnvironmentVariable("UV_CACHE_DIR",          "$base\cache\uv",          "User")
[Environment]::SetEnvironmentVariable("UV_PYTHON_INSTALL_DIR", "$base\uv-python",         "User")
[Environment]::SetEnvironmentVariable("PIP_CACHE_DIR",         "$base\cache\pip",         "User")
[Environment]::SetEnvironmentVariable("HF_HOME",               "$base\cache\huggingface", "User")
[Environment]::SetEnvironmentVariable("OLLAMA_MODELS",         "$base\ollama\models",     "User")
```

Close and reopen PowerShell, then check: `$env:HF_HOME` should print `D:\copilot\cache\huggingface`.
If Ollama is already running, quit it from the system tray (right-click → *Quit Ollama*) and start
it again so it sees `OLLAMA_MODELS`.

These are Windows environment variables, not `.env` settings: `.env` is only read by the copilot
itself, while uv, pip, Hugging Face and Ollama read the Windows environment. Keep `UV_CACHE_DIR` on
the same drive as the project so uv can hard-link files into `.venv` instead of copying them.

Then follow the rest of the guide with the changes marked **Other drive** in 2.1, 2.2, 2.3 and 3.

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

**Other drive:** use `wsl --install --no-distribution` instead. Docker Desktop brings its own WSL
distribution, so the Ubuntu one that `wsl --install` adds on `C:` isn't needed.

### 2.2 Install the tools

Use `winget` (built into Windows 10/11), or download each installer from the linked site.

```powershell
winget install -e --id Git.Git                 # https://git-scm.com/download/win
winget install -e --id Python.Python.3.12      # https://www.python.org/downloads/windows/
winget install -e --id Docker.DockerDesktop    # path A: https://www.docker.com/products/docker-desktop/
winget install -e --id Ollama.Ollama           # path B only: https://ollama.com/download/windows
winget install -e --id astral-sh.uv            # path B only: https://docs.astral.sh/uv/
```

**Other drive:** install Docker Desktop and Ollama from their downloaded installers instead of
`winget`, so you can choose where their data goes. In PowerShell, in your Downloads folder:

```powershell
# Docker Desktop (path A): images, containers and volumes go to D:\copilot\docker
Start-Process -Wait ".\Docker Desktop Installer.exe" -ArgumentList `
  "install", "--accept-license", "--wsl-default-data-root=D:\copilot\docker"
# Ollama (path B): the program goes to D:\copilot\ollama\app; models follow OLLAMA_MODELS from 1.1
.\OllamaSetup.exe /DIR="D:\copilot\ollama\app"
```

If Docker Desktop is already installed, leave it and move its data in 2.3 instead.

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

4. **Other drive:** open **Settings → Resources → Advanced → Disk image location**, choose
   `D:\copilot\docker`, and click **Apply & restart**. Docker Desktop moves its disk image there,
   including any images and volumes already in it. Every container, image and volume of this
   project (the LLM, the embedding models, the index and the user accounts) lives inside that disk
   image, so nothing else is needed for path A. Skip this if you used `--wsl-default-data-root` in
   2.2.

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
cd $HOME                    # Other drive: cd D:\copilot
git clone https://github.com/samirdash4u/knowledge-copilot.git
cd knowledge-copilot
```

Everything the project writes itself stays in this folder: `.venv` (path B), the index in
`data\index`, user accounts in `data\users.db` and eval reports in `reports`. To keep the index or
the users database somewhere else, set `COPILOT_INDEX_DIR` or `COPILOT_USERS_DB` in `.env`.

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
| `COPILOT_DEMO_PASSWORD` | Initial password of the demo users alice, bob and carol. After a change, run `up` again; demo users who already chose their own password keep it. |
| `COPILOT_OLLAMA_MODEL` | The LLM. The default `qwen2.5:7b-instruct-q4_K_M` fits an 8 GB GPU. |
| `COPILOT_LLM_TIMEOUT_S` | Seconds to wait for an answer, `300` by default. Raise it if the model runs on the CPU. |

To find this PC's IP address, run `ipconfig` and look for **IPv4 Address** under your Wi-Fi or
Ethernet adapter.

## 5A. Run with Docker Desktop

```powershell
python scripts\manage.py up            # add --gpu, or set COPILOT_GPU=true, for the GPU
python scripts\manage.py pull-model    # downloads the LLM, about 4.7 GB
python scripts\manage.py ingest        # downloads embedding models on first run, then indexes
python scripts\manage.py status        # prints the URLs to open
```

Only `up` is required: the model download and ingest are also buttons under **Admin > Status** in
the web UI ([admin-console.md](admin-console.md)).

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

   The server prints the URLs it can be reached on. Press Ctrl+C to stop it. You can also skip
   `copilot ingest` (and `ollama pull`) and use **Download model** and **Run ingest** under
   **Admin > Status** in the web UI instead; ingest from the browser reloads the running server,
   so no restart is needed.

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

Everything after starting the server can be done in the browser: as an admin, open **Admin >
Status** to download the model and run ingest, and use the other tabs for documents, tenants,
users and the evaluation. See [admin-console.md](admin-console.md).

To watch quality, safety, speed and cost (Recall@5, MRR, groundedness, faithfulness,
unauthorized retrieval rate, p95 latency, cost per answer), sign in as an admin such as `carol` and
click **KPIs**, or run `copilot kpis`. See [monitoring.md](monitoring.md).

## 8. Clean up

Run these in the project folder. Each command lists what it will delete and asks first; add
`--dry-run` to only see the list.

```powershell
python scripts\manage.py clean            # stop containers; delete the index, eval reports, caches
python scripts\manage.py clean --data     # also Qdrant data and user accounts (data\users.db)
python scripts\manage.py clean --models   # also the downloaded models (~6 GB)
python scripts\manage.py clean --all      # everything, including .env and .venv
```

After `clean`, run `up` and `ingest` again. After `--models`, also run `pull-model`. After `--all`,
start again from step 4. The documents in `data\corpus` are never deleted.

To uninstall completely, also:

- remove the firewall rule from step 6;
- path A: `docker image rm qdrant/qdrant:v1.12.4 redis:7.4-alpine ollama/ollama:0.5.7`;
- path B: `ollama rm qwen2.5:7b-instruct-q4_K_M` and delete
  `%USERPROFILE%\.cache\huggingface`, or the `HF_HOME` folder if you set one (the embedding and
  reranker models);
- delete the project folder.

### 8.1 Free up space on C: after earlier runs

Work through these in order and stop once you have enough room. Steps 1–3 only delete things that
are downloaded again when needed.

**1. See what is taking the space.** In PowerShell:

```powershell
$paths = "$env:LOCALAPPDATA\Docker\wsl", "$env:USERPROFILE\.ollama\models",
  "$env:USERPROFILE\.cache\huggingface", "$env:LOCALAPPDATA\uv\cache", "$env:APPDATA\uv\python",
  "$env:LOCALAPPDATA\pip\Cache", "$HOME\knowledge-copilot"
foreach ($p in $paths) {
  if (Test-Path $p) {
    $gb = (Get-ChildItem $p -Recurse -Force -File -ErrorAction SilentlyContinue |
           Measure-Object Length -Sum).Sum / 1GB
    "{0,7:N1} GB  {1}" -f $gb, $p
  }
}
```

**2. Clear the download caches.** Both are safe to empty at any time:

```powershell
uv cache clean
python -m pip cache purge
```

**3. Clear the project's generated files.** In the project folder (`--dry-run` lists what would go):

```powershell
python scripts\manage.py clean            # index, eval reports, tool caches; stops containers
python scripts\manage.py clean --models   # also the downloaded models in Docker (~6 GB)
```

On path A these files live inside Docker's disk image, which does not shrink by itself. Follow
step 6 afterwards to get the space back on `C:`.

**4. Move the models (path B).** Set `HF_HOME` and `OLLAMA_MODELS` as in 1.1 and reopen
PowerShell. Quit Ollama from the system tray, then move what was already downloaded:

```powershell
Move-Item "$env:USERPROFILE\.cache\huggingface\*" "$env:HF_HOME"
Move-Item "$env:USERPROFILE\.ollama\models\*"     "$env:OLLAMA_MODELS"
```

Start Ollama again and check that `ollama list` still shows the model. If you'd rather download
them again, delete the two old folders instead of moving them.

**5. Move the project folder.** Stop the server first (`python scripts\manage.py down`, or Ctrl+C
for `copilot serve`). Then:

```powershell
Move-Item "$HOME\knowledge-copilot" "D:\copilot\knowledge-copilot"
cd D:\copilot\knowledge-copilot
```

A `.venv` doesn't work after it is moved: delete it (`Remove-Item -Recurse -Force .venv`) and create
it again as in 5B.2. On path A keep the folder name `knowledge-copilot`; Docker names the volumes
after it, so the index, users and models are found again after `python scripts\manage.py up`.

**6. Shrink or move Docker's disk image (path A).** First remove what Docker no longer uses:

```powershell
docker system prune        # stopped containers, unused networks, dangling images, build cache
docker builder prune -a    # all build cache; the next `up` rebuilds the image
```

Don't add `--volumes`: that also deletes the models and user accounts. Then choose one:

- **Move it off C: (recommended).** Docker Desktop → **Settings → Resources → Advanced → Disk
  image location** → `D:\copilot\docker` → **Apply & restart**. This frees all of it on `C:`.
- **Keep it on C: but compact it.** The disk image keeps its largest size until compacted. Quit
  Docker Desktop (tray icon → *Quit Docker Desktop*), then in **PowerShell as Administrator**:

  ```powershell
  wsl --shutdown
  Get-ChildItem "$env:LOCALAPPDATA\Docker\wsl" -Recurse -Filter *.vhdx   # note the full path
  diskpart
  ```

  At the `DISKPART>` prompt, using the path from above (usually `...\wsl\disk\docker_data.vhdx`,
  or `...\wsl\data\ext4.vhdx` on older Docker Desktop versions):

  ```text
  select vdisk file="C:\Users\<you>\AppData\Local\Docker\wsl\disk\docker_data.vhdx"
  attach vdisk readonly
  compact vdisk
  detach vdisk
  exit
  ```

**7. Remove or move an unused Ubuntu.** `wsl --list --verbose` lists the WSL distributions. If
`Ubuntu` is there and you don't use it, `wsl --unregister Ubuntu` deletes it and every file in it.
To keep it but move it, run `wsl --manage Ubuntu --move D:\copilot\wsl\Ubuntu` (needs a recent WSL;
run `wsl --update` first). Leave `docker-desktop` and `docker-desktop-data` alone; step 6 handles
them.

**8. Windows leftovers.** Installers leave files in `%TEMP%`. **Settings → System → Storage →
Temporary files** removes them.

## Troubleshooting

| Problem | Fix |
|---|---|
| *The language model took longer than ... seconds* / `httpx.ReadTimeout` | The model is loading, or running partly on the CPU. Run `ollama ps`: **100% GPU** is good; anything with CPU is slow. Close other GPU-heavy apps, keep `COPILOT_LLM_NUM_CTX` at 4096 or lower, and raise `COPILOT_LLM_TIMEOUT_S` (for example `600`) in `.env`, then restart. |
| *The language model is not reachable* | Start Ollama and check `ollama list` shows the model from `COPILOT_OLLAMA_MODEL`. |
| *invalid username or password* for a demo user | The user already chose a password, or `up` wasn't run again after `COPILOT_DEMO_PASSWORD` changed. Reset it with `copilot users reset-password alice` (in Docker: `docker compose exec api copilot users reset-password alice`). Five wrong attempts lock the account for five minutes. |
| *no space left on device* / `C:` is full | Follow 8.1 to free space on `C:`, and 1.1 to keep new downloads on another drive. |
| `make` is not recognized | Windows has no `make`. Use `python scripts\manage.py <command>` instead. |
| `docker` is not recognized | Reopen PowerShell after installing Docker Desktop. Check that `C:\Program Files\Docker\Docker\resources\bin` is on `PATH`. |
| *Docker daemon is not reachable* / `error during connect ... docker_engine` | Start Docker Desktop and wait for **Engine running**. |
| *WSL 2 installation is incomplete* | Run `wsl --update` as Administrator, then restart. |
| *port is already allocated* / *address already in use* | Something else uses the port. Find it with `netstat -ano \| findstr :8000`, or set another `COPILOT_PORT`. |
| *could not select device driver "nvidia"* | The GPU isn't available to Docker. Follow 2.4, or run without `--gpu`. |
| Other machines can't connect | Check `COPILOT_HOST` isn't `127.0.0.1`, add the firewall rule from step 6, and make sure the network is *Private*. Some VPN clients block LAN access. |
| The first answer is very slow | The model loads into memory on the first question. Later answers are faster. Without a GPU, each answer takes tens of seconds. |
| `scripts/demo/*.sh` don't run | They are bash scripts. Run them from Git Bash or WSL, and set `COPILOT_DEV_TOKEN_ENDPOINT=true` with `COPILOT_HOST=127.0.0.1` first. The web UI does the same thing without them. |
