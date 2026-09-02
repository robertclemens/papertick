# Running on Podman

The stack runs on rootless Podman with **no changes to the images and no changes to
`docker-compose.yml`**. The two differences from Docker are both about tooling, not
about the application: you have to point Compose at Podman explicitly, and you have to
let Podman build the images rather than Compose.

Everything else — the healthchecks, the `depends_on` gating, `cap_drop: ALL`,
`no-new-privileges`, the pinned `999:1000` user on Redis, the memory and pid caps —
carries over untouched.

## What was tested

Podman 4.9.3, rootless, cgroups v2. The full six-service stack came up healthy, and
signup, opening an account, a deposit and a live-priced order all worked through it.
Confirmed with `podman ps` rather than assumed — see the warning in the next section
for why that distinction matters.

## Prerequisites

- **Podman 4.x or newer.** Rootless is fine and is what these instructions assume.
- **A Compose implementation.** Two work:
  - The Docker Compose plugin (v2 or v5) pointed at Podman's socket. That is what is
    documented here and what was tested.
  - `podman-compose`, the separate Python implementation. It builds with `podman build`
    natively, so it avoids the second difference below, but it was not tested here.

You do **not** need the Docker daemon. If it happens to be installed, read the next
section carefully.

## Difference 1: point Compose at Podman, explicitly

Podman exposes a Docker-compatible API over a socket, and Compose talks to whatever
`DOCKER_HOST` names. Start the socket and point at it:

```bash
systemctl --user start podman.socket
export DOCKER_HOST=unix:///run/user/$(id -u)/podman/podman.sock
```

> **If Docker is also installed on this host, do not skip this.** `DOCKER_HOST` is
> usually already exported to the Docker socket, and `podman compose` will use it
> without complaint — building and running your entire stack on Docker while you
> believe you are testing Podman. It prints nothing to suggest anything is wrong.
> The only reliable check is that the containers show up in `podman ps`:
>
> ```bash
> podman ps          # should list six papertick-* containers
> ```

## Difference 2: build with Podman, not with Compose

Compose v2.24 and newer build through BuildKit, which starts a privileged helper
container that rootless Podman refuses:

```
ERROR: Error response from daemon: crun: create `/sys/fs/cgroup/docker`:
Permission denied: OCI permission denied
```

Podman's own builder handles both Dockerfiles unmodified, so build first and then bring
the stack up with `--no-build`.

The image names have to match what Compose expects for a built service, which is
`<project>-<service>`. The project is `papertick`, set on the first line of
`docker-compose.yml`. `worker` and `beat` run the same image as `backend` with a
different command, so they are tags of the same build.

## Install

```bash
git clone https://github.com/robertclemens/papertick.git
cd papertick
cp .env.example .env
```

Fill in the three secrets at the top of `.env` exactly as the
[README](../README.md#install) describes — the hex generators matter here too.

```bash
# 1. point Compose at Podman
systemctl --user start podman.socket
export DOCKER_HOST=unix:///run/user/$(id -u)/podman/podman.sock

# 2. build with Podman
podman build -t papertick-backend ./backend
podman tag papertick-backend papertick-worker
podman tag papertick-backend papertick-beat
podman build -t papertick-frontend \
  --build-arg BACKEND_URL=http://backend:8000 \
  --build-arg BASE_PATH= \
  ./frontend

# 3. run
docker compose up -d --no-build
```

Then check it, and check *which runtime* answered:

```bash
podman ps                                  # six containers, db/redis/backend healthy
curl -s localhost:3000/healthz             # {"status":"ok","database":"ok","redis":"ok"}
```

Serving from a sub-folder? `BASE_PATH` is compiled into the frontend bundle, so it goes
in the `podman build` line above *as well as* in `.env` — see
[reverse-proxy.md](reverse-proxy.md).

## Everyday commands

With `DOCKER_HOST` pointed at Podman, every command in the README works unchanged,
including backup and restore:

```bash
docker compose exec -T db pg_dump -U papertick -d papertick --format=custom \
  > papertick-$(date +%F).dump
```

Put the export in your shell profile so you don't have to remember it:

```bash
echo 'export DOCKER_HOST=unix:///run/user/$(id -u)/podman/podman.sock' >> ~/.bashrc
```

Rebuilding after a `git pull` means repeating step 2 — `--build` won't work, for the
BuildKit reason above.

## Surviving a reboot

`restart: unless-stopped` is honoured by the Docker *daemon*, which systemd starts at
boot. Rootless Podman has no daemon, so nothing brings the containers back unless you
arrange it:

```bash
loginctl enable-linger "$USER"                    # user services run without a login
systemctl --user enable --now podman-restart.service
systemctl --user enable podman.socket
```

Without `enable-linger`, everything stops when your last session ends.

## Notes

- **SELinux.** Nothing here needs `:z` or `:Z`. The stack uses named volumes only, and
  there are no bind mounts to relabel.
- **Resource limits.** `mem_limit` and `pids_limit` applied correctly in testing, but
  they need cgroup v2 delegation. Check yours:
  ```bash
  cat /sys/fs/cgroup/user.slice/user-$(id -u).slice/cgroup.controllers   # want memory, pids
  ```
  Without them Podman warns and carries on; nothing in the application depends on the
  caps being enforced.
- **`podman kube generate`** will happily emit Kubernetes YAML from the running
  containers. Don't ship it — it encodes host-specific details and none of the things
  a real deployment needs (a migration Job, a single-replica `beat`, Secrets). There is
  no supported Kubernetes deployment yet.
