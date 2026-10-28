# Raspberry Pi

IndraMesh runs on a Raspberry Pi 5 with 8 GB RAM and a 64-bit OS, in both installation shapes: the
Docker Compose quickstart and the single-node k3s quickstart. This page lists only what differs from
the general instructions in the [README](../README.md#installation) and in
[docs/DEPLOYMENT.md](DEPLOYMENT.md#kubernetes-host-prerequisites).

## Requirements

| Item | Requirement |
|---|---|
| Board | Raspberry Pi 5 with 8 GB RAM (a 4 GB board is too small for OpenSearch plus the build) |
| OS | 64-bit Raspberry Pi OS (reports `ID=debian`) or 64-bit Ubuntu for Raspberry Pi |
| Storage | An SSD (NVMe HAT or USB 3) is strongly recommended; an SD card works but makes builds and OpenSearch slow |
| Free disk | About 20 GB for images, build cache and data |

Check the architecture first; it must print `aarch64`:

```bash
uname -m
```

## Install Docker

Both shapes need Docker Engine with the Compose v2 plugin, because the application images are built
locally on the Pi. Install it from Docker's official repository (the convenience script supports
Raspberry Pi OS 64-bit):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

Log out and back in so the group membership applies, then check `docker compose version`.

## Docker Compose quickstart

Nothing is Pi-specific here. Clone the repository and run the installer as described in the
[README](../README.md#quick-start):

```bash
git clone https://github.com/weboliver/indramesh-core.git
cd indramesh-core
./install_indramesh_docker.sh
```

On a Raspberry Pi 5 with 8 GB the cold install took **3 min 13.5 s** (measured 2026-10). Open
`https://localhost:8443` on the Pi itself.

**From another machine on the network**, use the Pi's IP address, not its host name:
`https://<pi-ip>:8443`. Add that origin to `CORS_ALLOWED_ORIGINS` in `.env` and restart the stack.
A host name such as `raspberrypi.local` is refused by the frontend in 1.0 (its preview server only
accepts `localhost`, `nginx` and IP addresses).

## k3s quickstart

One thing needs preparing on a Pi before `./install_indramesh_k8s.sh` succeeds.

### Enable the memory cgroup

Raspberry Pi OS boots with the memory cgroup controller disabled, and k3s does not start without it.
The installer then stops with:

```
[INFO]  Failed to find memory cgroup, you may need to add "cgroup_memory=1 cgroup_enable=memory" to your linux cmdline (/boot/firmware/cmdline.txt on a Raspberry Pi)
...
[preflight] ERROR: k3s systemd unit is inactive and could not be started — run: systemctl status k3s
```

Append the two parameters to the **single line** of `/boot/firmware/cmdline.txt` (the file must stay
one line) and reboot:

```bash
grep -q cgroup_enable=memory /boot/firmware/cmdline.txt \
  || sudo sed -i '1 s/$/ cgroup_memory=1 cgroup_enable=memory/' /boot/firmware/cmdline.txt
sudo reboot
```

After the reboot this must list `memory`:

```bash
cat /sys/fs/cgroup/cgroup.controllers
```

The output looks like this (the exact list can vary by kernel; `memory` is the word that counts):

```
cpuset cpu io memory pids
```

Without the parameters `memory` is missing from that line, and that is exactly what stops k3s.

If a first installer run already failed here, simply run the installer again after the reboot; it is
idempotent and reuses the k3s installation.

### Memory

An 8 GB Pi 5 reports about 8063 MiB, because the firmware and the kernel reserve the rest. That is
above the installer's 6 GiB floor, so it selects the `compact` resource profile on its own:

```bash
./install_indramesh_k8s.sh
```

Memory is still tight. Stop a Compose stack running on the same Pi before the install
(`docker compose down` in its directory), and leave the three opt-in components
(`MONITORING_ENABLED`, `HEADLAMP_ENABLED`, `LOGINFRA_ENABLED`) switched off.

### Access

On the Pi itself, open `https://localhost`; the installer imports its certificate authority into the
Pi's trust store.

From another machine on the network, open `https://<pi-ip>`. The installer detects the Pi's primary
IPv4 address and adds it to the certificate; the closing banner prints that address. To trust the
certificate there, copy the certificate authority from
`/usr/local/share/ca-certificates/indramesh-local-ca.crt` on the Pi and import it into that
machine's trust store, or accept the browser warning.

If the Pi's address changes (DHCP), run the installer again; it reissues the certificate for the new
address. Give the Pi a fixed address or a DHCP reservation to avoid that. `INDRAMESH_HOST_IP=<ip>`
sets the address explicitly, `INDRAMESH_HOST_IP=` switches IP access off.

## Removing it again

The removal paths are the same as on any other host — see
[What the installer changes on your host](../README.md#what-the-installer-changes-on-your-host).
Additionally remove `cgroup_memory=1 cgroup_enable=memory` from `/boot/firmware/cmdline.txt` if
nothing else on the Pi needs it.
