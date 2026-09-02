# Upgrading

Pulling a newer version of this repo and rebuilding is normally all that's needed:

```bash
git pull
diff .env.example .env   # check for newly-added variables and pick values for them
docker compose up -d --build
```

- **Schema changes are automatic.** `entrypoint.sh` runs `python -m app.init_db`
  before the API starts, which creates any new tables and applies small,
  idempotent migrations (column renames, new enum values) to existing ones —
  see the module docstring in `backend/app/init_db.py`. There's no separate
  migration command to remember.
- **There is no down-migration path.** Schema changes are additive and forward-only,
  so `pg_dump` the `db` volume before an upgrade that touches anything you'd
  regret losing:
  `docker compose exec db pg_dump -U papertick papertick > backup.sql`.
- **Always rebuild the frontend on a real upgrade** (`--build`, not just `up -d`).
  `BACKEND_URL`, `BASE_PATH` and other frontend config are baked in at image build
  time via Next.js, so a plain restart won't pick up compose or `.env` changes on
  that side.
- **New environment variables** ship with a default in both `.env.example` and
  `docker-compose.yml`, so an upgrade won't break an existing `.env` that's
  missing them — diff the two files after pulling to see what's new and worth
  setting explicitly.

## Dependency upgrades

[`upgrade.sh`](../upgrade.sh) bumps Python packages, npm packages, and (under `--major`)
the four Docker base images, gated behind a test run (`pytest`, `tsc --noEmit`,
`next build`, then a containerized `/healthz` check) that auto-rolls-back on failure:

```bash
./upgrade.sh --check          # print the full dependency report and stop
./upgrade.sh                  # bump within current majors
./upgrade.sh --major          # cross major versions too (review changelogs after)
./upgrade.sh --major --force  # also lift the deliberate version holds (see the script header)
./upgrade.sh --skip-tests     # skip the test gate, so a broken upgrade is not caught
```

Every run, `--check` included, starts by printing the same full report of what is
outdated; `--check` simply stops there.

**Postgres major-version upgrades need a manual data migration** — a Postgres data
directory isn't binary-compatible across major versions, so swapping the image tag
alone will not work (the container simply refuses to start against an incompatible
volume; Postgres 18's image additionally changed its expected mount point from
`.../postgresql/data` to `.../postgresql` — already reflected in this repo's
`docker-compose.yml`). `upgrade.sh` deliberately does not automate this. The safe
sequence, if you're bumping the `db` image to a new major:

```bash
docker compose exec -T db pg_dump -U papertick -d papertick --format=custom > backup.dump
# point docker-compose.yml's pgdata volume at a new name so the old volume is left
# untouched as a fallback, then bring the new image up on the fresh volume:
docker compose up -d db
docker compose cp backup.dump db:/tmp/backup.dump
docker compose exec -T db pg_restore -U papertick -d papertick --no-owner --role=papertick /tmp/backup.dump
```

Verify row counts against the old volume before removing it — `docker run --rm -v
<old-volume>:<mountpoint> postgres:<old-tag> ...` spins up a throwaway read-only check.
Mount it where that image expects its data directory: `/var/lib/postgresql/data` up to
Postgres 17, `/var/lib/postgresql` from 18 on.

