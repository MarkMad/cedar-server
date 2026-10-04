# Cedar on Windows ARM64 (Snapdragon X2)

This guide runs Cedar on a Snapdragon X2 Windows PC with WSL 2 and Docker Desktop. The stack runs on CPU; it does not use Qualcomm GPU or NPU acceleration.

## 1. Install WSL 2 and Docker Desktop

In an elevated PowerShell window, install WSL and an Ubuntu distribution, then restart if Windows asks:

```powershell
wsl --install -d Ubuntu
```

Install Docker Desktop for Windows using Docker's [Windows ARM installation guide](https://docs.docker.com/desktop/setup/install/windows-install/). In Docker Desktop, enable the WSL 2 based engine and turn on integration for your Ubuntu distribution under **Settings → Resources → WSL Integration**. Docker Desktop provides the Docker CLI and engine to that WSL distribution.

Open Ubuntu and confirm it is running as 64-bit ARM:

```bash
uname -m
```

It should print `aarch64`. In Docker Desktop, confirm the engine architecture from a WSL shell:

```bash
docker info --format '{{.Architecture}}'
```

Expect `aarch64` or `arm64`.

## 2. Put the repository in WSL and start Cedar

For best Linux filesystem performance, keep the working copy in your Ubuntu home directory. If the repository is on the Windows D: drive at `/mnt/d/Projects/cedar-server-arm`, copy it with:

```bash
cp -a /mnt/d/Projects/cedar-server-arm ~/cedar-server-arm
cd ~/cedar-server-arm
```

Alternatively, clone this ARM64 repository directly into your WSL home directory:

```bash
cd ~
git clone https://github.com/MarkMad/cedar-server-arm.git
cd cedar-server-arm
```

From the repository directory, use the ARM64 Compose override:

```bash
docker compose -f docker-compose.yml -f docker-compose.arm64.yml up -d --build
```

The override selects `linux/arm64` for Cedar, Kokoro, and the optional Classics image. Kokoro `v0.6.0` publishes an ARM64 image manifest. Initial build and model downloads can take a while.

Check startup and get the generated owner key:

```bash
docker compose -f docker-compose.yml -f docker-compose.arm64.yml ps
docker compose -f docker-compose.yml -f docker-compose.arm64.yml logs cedar
```

Open `http://localhost:8000` in a Windows browser to see Cedar's status page. Keep using both Compose files for later commands, such as `logs`, `ps`, `down`, or the optional Classics task. Pair the Cedar app using the server address and owner key shown in the logs; `docker compose ... exec -u cedar cedar cat /data/cedar.key` prints the key again.

Check voice-engine readiness with `curl http://localhost:8000/api/health`; wait for
`kokoro_ok` to be `true` before trying speech. See the Kokoro logs if it stays false:

```bash
docker compose -f docker-compose.yml -f docker-compose.arm64.yml logs kokoro
docker compose -f docker-compose.yml -f docker-compose.arm64.yml exec -u cedar cedar cat /data/cedar.key
```

The `cedar-data` named volume keeps the library, owner key, and audio cache across
rebuilds. Do not use `down -v` unless you intend to delete that data. Do not combine
this profile with the NVIDIA GPU override. The optional Pocket TTS override has
not been validated for this ARM64 setup; start with the standard Kokoro voices.

## 3. Connect from devices on your network

For a phone or other device on your LAN, use the Windows PC's LAN IPv4 address with port 8000, for example `http://192.168.1.20:8000`. Find it in Windows with `ipconfig`. `localhost` is only for the PC itself. WSL networking and Windows firewall rules affect LAN access: if another device cannot open the status page, check the Windows network profile, the current WSL networking mode, and whether an inbound TCP 8000 rule is needed. Configure any firewall rule yourself and limit it to the trusted network; do not expose the plain HTTP service to the public internet.

## Native Docker Engine alternative

Docker Desktop is not required. You can instead install Docker Engine inside ARM64 Ubuntu on WSL 2, following Docker's [official Ubuntu installation guide](https://docs.docker.com/engine/install/ubuntu/). Follow its post-install steps to run Docker as your user. Use the same Compose command above. Docker's [WSL 2 guide](https://docs.docker.com/desktop/features/wsl/) covers Docker Desktop integration and WSL considerations.
