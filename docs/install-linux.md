# Installing on Linux

This guide is tested on Ubuntu 22.04 and 24.04 and Debian 12. Fedora and RHEL notes are included
where they differ. There are two ways to run the copilot:

- **A. Docker (recommended).** Runs the API, web UI, Qdrant, Redis and Ollama in containers.
- **B. Native Python.** No Docker. It uses Ollama installed on the host and keeps the vector index and
  cache in memory.

## 1. Check the hardware

| | Minimum | Recommended |
|---|---|---|
| RAM | 16 GB | 32 GB |
| Free disk | 25 GB | 40 GB |
| GPU (optional) | NVIDIA, 8 GB VRAM | NVIDIA, 8 GB+ VRAM |

Without a GPU the LLM runs on the CPU. That works, but an answer takes tens of seconds instead of a
few.

## 2. Install the prerequisites

### 2.1 Basic tools

```bash
# Ubuntu / Debian
sudo apt-get update
sudo apt-get install -y git python3 python3-venv curl ca-certificates

# Fedora / RHEL
sudo dnf install -y git python3 curl
```

Check that `python3 --version` reports 3.11 or newer for path B. Path A only needs 3.9+ to run the
helper script.

### 2.2 Docker Engine and Compose (path A)

Install Docker from Docker's own repository. Distribution packages such as `docker.io` are often too
old for `docker compose`. On Ubuntu:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo tee /etc/apt/keyrings/docker.asc >/dev/null
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

For other distributions, follow https://docs.docker.com/engine/install/. Then let your user run
Docker without `sudo`:

```bash
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker            # or log out and back in
docker run --rm hello-world
docker compose version
```

### 2.3 NVIDIA GPU (optional)

1. Install the NVIDIA driver and check that `nvidia-smi` lists the GPU. On Ubuntu:
   `sudo ubuntu-drivers install`, then reboot.
2. For path A, install the **NVIDIA Container Toolkit** so containers can use the GPU
   (https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
   On Ubuntu and Debian:

   ```bash
   curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
     | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
   curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
     | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
     | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
   sudo apt-get update
   sudo apt-get install -y nvidia-container-toolkit
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker
   ```

3. Check it:

   ```bash
   docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
   ```

### 2.4 Ollama and uv (path B only)

```bash
curl -fsSL https://ollama.com/install.sh | sh     # installs and starts the ollama service
curl -LsSf https://astral.sh/uv/install.sh | sh   # then open a new shell
```

## 3. Get the code

```bash
cd ~
git clone https://github.com/samirdash4u/knowledge-copilot.git
cd knowledge-copilot
```

## 4. Configure

```bash
python3 scripts/manage.py setup
nano .env
```

`setup` copies `.env.example` to `.env` and fills in a random `COPILOT_JWT_SECRET`. The settings
you're most likely to change:

| Setting | Meaning |
|---|---|
| `COPILOT_HOST` | Address the web UI listens on. `0.0.0.0` means all network interfaces (reachable from other machines once the firewall allows it). `127.0.0.1` means this machine only. You can also give one specific IP of this machine, for example `192.168.1.20`. |
| `COPILOT_PORT` | Port for the web UI, `8000` by default. |
| `COPILOT_GPU` | `true` to run the LLM on the NVIDIA GPU (path A). |
| `COPILOT_DEMO_PASSWORD` | Initial password of the demo users alice, bob and carol. After a change, run `up` again; demo users who already chose their own password keep it. |
| `COPILOT_OLLAMA_MODEL` | The LLM. The default `qwen2.5:7b-instruct-q4_K_M` fits an 8 GB GPU. |

To find this machine's IP address, run `hostname -I` or `ip -4 addr show`.

## 5A. Run with Docker

```bash
python3 scripts/manage.py up            # add --gpu, or set COPILOT_GPU=true, for the GPU
python3 scripts/manage.py pull-model    # downloads the LLM, about 4.7 GB
python3 scripts/manage.py ingest        # downloads embedding models on first run, then indexes
python3 scripts/manage.py status        # prints the URLs to open
```

If you have `make`, `make up`, `make up-gpu`, `make pull-model`, `make ingest` and `make status`
run the same commands. The containers restart automatically after a reboot as long as the Docker
service is enabled. Other commands:

```bash
python3 scripts/manage.py logs     # follow the API log
python3 scripts/manage.py down     # stop everything; users, index and models are kept
python3 scripts/manage.py eval     # run the evaluation set, report in reports/
```

## 5B. Run natively (no Docker)

```bash
ollama pull qwen2.5:7b-instruct-q4_K_M
uv venv
source .venv/bin/activate
uv pip install -e ".[ml,dev]"
printf 'COPILOT_VECTOR_STORE=memory\nCOPILOT_CACHE=memory\n' >> .env
copilot ingest
copilot serve                              # uses COPILOT_HOST / COPILOT_PORT from .env
copilot serve --host 0.0.0.0 --port 8080   # or override on the command line
```

To keep it running after you log out, create a systemd service. Replace `YOUR_USER` with your
username:

```ini
# /etc/systemd/system/knowledge-copilot.service
[Unit]
Description=Knowledge Copilot
After=network-online.target ollama.service

[Service]
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/knowledge-copilot
ExecStart=/home/YOUR_USER/knowledge-copilot/.venv/bin/copilot serve
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now knowledge-copilot
journalctl -u knowledge-copilot -f
```

## 6. Open the port in the firewall

You only need this when other machines should reach the UI, which means `COPILOT_HOST` is `0.0.0.0`
or this machine's IP address. Use your `COPILOT_PORT`.

**Ubuntu / Debian (ufw):**

```bash
sudo ufw allow 8000/tcp comment 'Knowledge Copilot'
# or only from your LAN:
sudo ufw allow from 192.168.1.0/24 to any port 8000 proto tcp
sudo ufw status
```

**Fedora / RHEL (firewalld):**

```bash
sudo firewall-cmd --permanent --add-port=8000/tcp
sudo firewall-cmd --reload
sudo firewall-cmd --list-ports
```

**Important for Docker (path A):** Docker publishes ports through its own iptables rules, which take
effect *before* ufw. A port published on `0.0.0.0` is therefore reachable from the network even if
ufw would block it. To control who can reach the UI with Docker:

- set `COPILOT_HOST=127.0.0.1` to keep it local, or set it to one specific LAN IP so it's only on
  that interface; or
- add a rule to Docker's `DOCKER-USER` chain, for example to allow only your LAN:

  ```bash
  sudo iptables -I DOCKER-USER -p tcp --dport 8000 ! -s 192.168.1.0/24 -j DROP
  ```

  This rule doesn't survive a reboot unless you save it, for example with the
  `iptables-persistent` package.

Qdrant, Redis and Ollama are published on `127.0.0.1` only, so they are never reachable from other
machines whatever the firewall says.

To check from another machine: `curl http://<this-machine-ip>:8000/health` should print
`{"status":"ok"}`.

To remove the rules later: `sudo ufw delete allow 8000/tcp`, or
`sudo firewall-cmd --permanent --remove-port=8000/tcp && sudo firewall-cmd --reload`.

## 7. Sign in

Open the URL printed by `status` (or by `copilot serve`), for example `http://localhost:8000` or
`http://192.168.1.20:8000`. Sign in as `alice`, `bob` or `carol` with `COPILOT_DEMO_PASSWORD`, then
set a new password. To add accounts:

```bash
docker compose exec api copilot users add dave --role manager   # path A
copilot users add dave --role manager                            # path B, venv active
```

To watch quality, safety, speed and cost (Recall@5, MRR, groundedness, faithfulness,
unauthorized retrieval rate, p95 latency, cost per answer), sign in as an admin such as `carol` and
click **KPIs**, or run `copilot kpis`. See [monitoring.md](monitoring.md).

## 8. Clean up

Run these in the project folder. Each command lists what it will delete and asks first; add
`--dry-run` to only see the list.

```bash
python3 scripts/manage.py clean            # stop containers; delete the index, eval reports, caches
python3 scripts/manage.py clean --data     # also Qdrant data and user accounts (data/users.db)
python3 scripts/manage.py clean --models   # also the downloaded models (~6 GB)
python3 scripts/manage.py clean --all      # everything, including .env and .venv
```

`make clean` runs the first one. After `clean`, run `up` and `ingest` again. After `--models`, also
run `pull-model`. After `--all`, start again from step 4. The documents in `data/corpus` are never
deleted.

To uninstall completely, also:

- remove the firewall rule from step 6;
- path A: `docker image rm qdrant/qdrant:v1.12.4 redis:7.4-alpine ollama/ollama:0.5.7`;
- path B: `ollama rm qwen2.5:7b-instruct-q4_K_M` and `rm -rf ~/.cache/huggingface` (shared with
  any other Hugging Face projects, so check first);
- delete the project folder.

## Troubleshooting

| Problem | Fix |
|---|---|
| *invalid username or password* for a demo user | The user already chose a password, or `up` wasn't run again after `COPILOT_DEMO_PASSWORD` changed. Reset it with `copilot users reset-password alice` (in Docker: `docker compose exec api copilot users reset-password alice`). Five wrong attempts lock the account for five minutes. |
| `permission denied ... /var/run/docker.sock` | Your user isn't in the `docker` group yet: `sudo usermod -aG docker $USER`, then log out and in. |
| `docker: 'compose' is not a docker command` | Install `docker-compose-plugin` from Docker's repository (2.2). |
| *port is already allocated* / *address already in use* | Find the user with `sudo ss -ltnp \| grep :8000`, or set another `COPILOT_PORT`. |
| *could not select device driver "nvidia"* | Install the NVIDIA Container Toolkit and restart Docker (2.3), or run without `--gpu`. |
| Permission denied reading `data/corpus` inside the container (Fedora/RHEL) | SELinux blocks the bind mount. Run `chcon -Rt container_file_t data/corpus` once. |
| Other machines can't connect | Check `COPILOT_HOST` isn't `127.0.0.1`, open the port (step 6), and check cloud security groups if this is a VM. |
| The first answer is very slow | The model loads into memory on the first question. Without a GPU, each answer takes tens of seconds. |
