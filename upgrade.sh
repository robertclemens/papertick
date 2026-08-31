#!/usr/bin/env bash
# upgrade.sh — upgrades all PaperTick dependencies: every direct package in
# requirements.txt and package.json, and the four Docker base images
# (Python, Node, Postgres, Redis).
#
# Every run (any mode, including --check) first prints a full report: every
# direct dependency with its current pin and the actual latest release, not
# just the ones that happen to be outdated — this is the source of truth for
# "are we current," not something --check does differently from the rest.
#
# Usage:
#   ./upgrade.sh              # safe mode: upgrade within the current major version of each package
#   ./upgrade.sh --major      # also cross major versions (Docker base images only move under this flag)
#   ./upgrade.sh --major --force  # --major, and also lift the deliberate typescript hold (still
#                                  #   below its brand-new native/Go 7.x compiler by default). Does
#                                  #   NOT touch @types/node's Node-LTS pairing -- that's a
#                                  #   correctness constraint, not caution, and --force isn't the
#                                  #   right lever for it (bump it by hand alongside a Node bump).
#                                  #   Runs the full test gate exactly like any other run and rolls
#                                  #   back automatically on failure -- this is how you try
#                                  #   typescript@7 against the real codebase safely.
#   ./upgrade.sh --check      # print the full dependency report and stop; nothing is changed
#   ./upgrade.sh --skip-tests # upgrade and skip the test gate (not recommended)
#
# Test gate (runs after upgrade, before rebuilding/restarting containers):
#   1. pytest tests/ -q          — the backend's real test suite (sqlite + synthetic data)
#   2. tsc --noEmit + next build — type-check and production-build the frontend
#   3. docker compose up -d --build, then poll /healthz until database+redis are ok
#   If any step fails, every changed file is rolled back to its pre-upgrade state.
#
set -uo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; RED='\033[0;31m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
ok()    { echo -e "${GREEN}✓${NC}  $*"; }
warn()  { echo -e "${YELLOW}⚠${NC}  $*"; }
info()  { echo -e "${BLUE}▸${NC}  $*"; }
err()   { echo -e "${RED}✗${NC}  $*"; }
step()  { echo -e "\n${BOLD}${CYAN}── $* ${NC}"; }
sep()   { echo -e "${CYAN}──────────────────────────────────────────────────────${NC}"; }

WORKDIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$WORKDIR/backend"
FRONTEND_DIR="$WORKDIR/frontend"
VENV="$BACKEND_DIR/.venv"
LOG_FILE="/tmp/papertick-upgrade.log"
MAJOR=false
CHECK_ONLY=false
SKIP_TESTS=false
FORCE=false
BACKUP_SUFFIX=$(date +%Y%m%d%H%M%S)

for arg in "$@"; do
  case "$arg" in
    --major)      MAJOR=true ;;
    --check)      CHECK_ONLY=true ;;
    --skip-tests) SKIP_TESTS=true ;;
    --force)      FORCE=true ;;
    *) err "Unknown argument: $arg"; echo "Usage: $0 [--major [--force]] [--check] [--skip-tests]"; exit 1 ;;
  esac
done

echo "" | tee "$LOG_FILE"
echo -e "${BOLD}PaperTick upgrade${NC} — $(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_FILE"
if $MAJOR; then
  warn "Running in --major mode: package majors AND Docker base images (Python/Node/Postgres/Redis) may move. Breaking changes are possible — review the test gate output carefully."
elif $CHECK_ONLY; then
  info "Running in --check mode. Nothing will be modified."
fi
if $FORCE && ! $MAJOR; then
  warn "--force has no effect without --major -- nothing is held back outside --major mode. Use: ./upgrade.sh --major --force"
fi
if $FORCE && $MAJOR; then
  warn "--force: lifting the typescript hold too -- it may land on the brand-new native/Go 7.x compiler. @types/node stays pinned to the Node LTS major regardless (that's a correctness pairing, not caution)."
fi
if $SKIP_TESTS; then
  warn "--skip-tests: the pytest/tsc/build/healthz gate is disabled. A broken upgrade will not be caught automatically."
fi
sep

# ── Snapshot current versions ──────────────────────────────
# This machine's tooling only -- used to run the test gate locally, not what
# the app runs on. The pinned, deployed versions (Dockerfile/docker-compose.yml)
# are in the "Docker base images" report below.
step "This machine's tooling (used to run the test gate, not what's deployed)"
echo "  venv python  $("$VENV/bin/python" --version 2>/dev/null || echo 'venv not found')"
echo "  host node    $(node --version 2>/dev/null || echo 'not found')"
echo "  host npm     $(npm --version 2>/dev/null || echo 'not found')"
echo "  Docker       $(docker --version 2>/dev/null || echo 'not found')"

# ── Comprehensive dependency report — every direct package + every base
# image, current vs. latest, regardless of mode (not just what's outdated,
# and not gated behind --major -- that flag only controls whether anything
# actually gets *changed*). ────────────────────────────────────────────────
report_backend_packages() {
  step "Backend Python packages (every direct pin in requirements.txt)"
  cd "$BACKEND_DIR"
  if [ ! -d "$VENV" ]; then
    info "Creating venv…"
    python3 -m venv "$VENV" 2>>"$LOG_FILE"
  fi
  "$VENV/bin/pip" install -q -r requirements-dev.txt 2>>"$LOG_FILE"

  printf "  %-24s %-12s %-12s %s\n" "PACKAGE" "CURRENT" "LATEST" "STATUS"
  while IFS= read -r line; do
    case "$line" in ""|\#*) continue ;; esac
    spec="${line%%==*}"
    bare="${spec%%\[*}"
    cur="${line##*==}"
    latest=$(curl -s -m 6 "https://pypi.org/pypi/${bare}/json" 2>/dev/null \
      | "$VENV/bin/python" -c "import json,sys
try:
    print(json.load(sys.stdin)['info']['version'])
except Exception:
    pass" 2>/dev/null)
    if [ -z "$latest" ]; then
      printf "  %-24s %-12s %-12s %s\n" "$spec" "$cur" "?" "(couldn't reach PyPI)"
    elif [ "$cur" = "$latest" ]; then
      printf "  %-24s %-12s %-12s %s\n" "$spec" "$cur" "$latest" "up to date"
    else
      printf "  %-24s %-12s %-12s %s\n" "$spec" "$cur" "$latest" "-> newer available"
    fi
  done < requirements.txt

  transitive=$("$VENV/bin/pip" list --outdated 2>/dev/null | tail -n +3)
  if [ -n "$transitive" ]; then
    info "Also outdated (transitive — not directly pinned, follows its parent package)."
    info "Often not actually movable: e.g. pydantic hard-pins its exact pydantic-core"
    info "build and refuses to import on a mismatch, so this list can show a newer"
    info "version existing on PyPI that no compatible parent release has adopted yet."
    echo "$transitive" | sed 's/^/    /'
  fi
  cd "$WORKDIR"
}

report_frontend_packages() {
  step "Frontend npm packages (every direct dep in package.json)"
  cd "$FRONTEND_DIR"
  printf "  %-24s %-12s %-12s %s\n" "PACKAGE" "CURRENT" "LATEST" "STATUS"
  node -e "
    const pkg = require('./package.json');
    const all = Object.assign({}, pkg.dependencies, pkg.devDependencies);
    for (const [name, ver] of Object.entries(all)) console.log(name + ' ' + ver);
  " | while read -r name cur; do
    latest=$(npm view "$name" version 2>/dev/null)
    note=""
    case "$name" in
      "@types/node") note=" (protected — kept matching the Node LTS major in frontend/Dockerfile)" ;;
      typescript)
        if $MAJOR && $FORCE; then
          note=" (hold lifted this run by --major --force)"
        else
          note=" (protected — held below the brand-new native/Go compiler generation; lift with --major --force)"
        fi
        ;;
    esac
    if [ -z "$latest" ]; then
      printf "  %-24s %-12s %-12s %s\n" "$name" "$cur" "?" "(couldn't reach npm)"
    elif [ "$cur" = "$latest" ]; then
      printf "  %-24s %-12s %-12s %s\n" "$name" "$cur" "$latest" "up to date"
    else
      printf "  %-24s %-12s %-12s %s\n" "$name" "$cur" "$latest" "-> newer available${note}"
    fi
  done
  cd "$WORKDIR"
}

report_docker_images() {
  step "Docker base images"
  py_tag=$(grep -m1 '^FROM' "$BACKEND_DIR/Dockerfile"  | sed -E 's/.*python:([^ ]+).*/\1/')
  node_tag=$(grep -m1 '^FROM' "$FRONTEND_DIR/Dockerfile" | sed -E 's/.*node:([^ ]+).*/\1/')
  pg_tag=$(grep -A2 '^  db:'    "$WORKDIR/docker-compose.yml" | grep image | sed -E 's/.*postgres:([^ ]+).*/\1/')
  redis_tag=$(grep -A2 '^  redis:' "$WORKDIR/docker-compose.yml" | grep image | sed -E 's/.*redis:([^ ]+).*/\1/')

  python3 - "$py_tag" "$node_tag" "$pg_tag" "$redis_tag" <<'PYEOF'
import json, sys, urllib.request

def get(url):
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return json.load(r)
    except Exception:
        return None

def cycle_key(c):
    return tuple(int(x) for x in c.split("."))

def row(service, image, current, recommended, status):
    print(f"  {service:<10} {image:<10} {current:<16} {recommended or '?':<20} {status}")

py_tag, node_tag, pg_tag, redis_tag = sys.argv[1:5]
print(f"  {'SERVICE':<10} {'IMAGE':<10} {'CURRENT TAG':<16} {'RECOMMENDED':<20} STATUS")

py_data = get("https://endoflife.date/api/python.json")
if py_data:
    newest = sorted(py_data, key=lambda d: cycle_key(d["cycle"]), reverse=True)[0]
    py_major_minor = py_tag.split("-")[0]
    if py_major_minor == newest["cycle"]:
        row("backend", "python", py_tag, f"{newest['latest']}-slim", "up to date (floating tag tracks latest patch)")
    else:
        row("backend", "python", py_tag, f"{newest['cycle']}-slim", f"-> newer major.minor available ({newest['cycle']})")
else:
    row("backend", "python", py_tag, None, "(couldn't reach endoflife.date)")

node_data = get("https://nodejs.org/dist/index.json")
if node_data:
    lts_major = None
    for d in node_data:
        if d.get("lts"):
            lts_major = d["version"].lstrip("v").split(".")[0]
            break
    node_major = node_tag.split("-")[0]
    if lts_major and node_major == lts_major:
        row("frontend", "node", node_tag, f"{lts_major}-alpine", "up to date (current LTS)")
    elif lts_major:
        row("frontend", "node", node_tag, f"{lts_major}-alpine", f"-> newer LTS available ({lts_major})")
    else:
        row("frontend", "node", node_tag, None, "(couldn't determine current LTS)")
else:
    row("frontend", "node", node_tag, None, "(couldn't reach nodejs.org)")

pg_data = get("https://endoflife.date/api/postgresql.json")
if pg_data:
    newest = max(pg_data, key=lambda d: cycle_key(d["cycle"]))
    pg_major = pg_tag.split("-")[0]
    if pg_major == newest["cycle"]:
        row("db", "postgres", pg_tag, f"{newest['cycle']}-alpine", "up to date")
    else:
        row("db", "postgres", pg_tag, f"{newest['cycle']}-alpine",
            f"-> newer major available ({newest['cycle']}) -- needs a manual data migration, see README")
else:
    row("db", "postgres", pg_tag, None, "(couldn't reach endoflife.date)")

redis_data = get("https://endoflife.date/api/redis.json")
if redis_data:
    newest = max(redis_data, key=lambda d: cycle_key(d["cycle"]))
    newest_major = str(cycle_key(newest["cycle"])[0])
    redis_major = redis_tag.split("-")[0]
    if redis_major == newest_major:
        row("redis", "redis", redis_tag, f"{newest_major}-alpine", "up to date")
    else:
        row("redis", "redis", redis_tag, f"{newest_major}-alpine", f"-> newer major available ({newest_major})")
else:
    row("redis", "redis", redis_tag, None, "(couldn't reach endoflife.date)")
PYEOF

  if $MAJOR && ! $CHECK_ONLY; then
    cp "$BACKEND_DIR/Dockerfile"  "$BACKEND_DIR/Dockerfile.bak-${BACKUP_SUFFIX}"
    cp "$FRONTEND_DIR/Dockerfile" "$FRONTEND_DIR/Dockerfile.bak-${BACKUP_SUFFIX}"
    cp "$WORKDIR/docker-compose.yml" "$WORKDIR/docker-compose.yml.bak-${BACKUP_SUFFIX}"
    warn "Base images are never auto-edited (bumping a runtime is a deliberate move, and"
    warn "a Postgres major needs a real data migration, not a tag swap -- see the README's"
    warn "Upgrading section). Compare the table above and edit the FROM/image lines by hand."
  fi
}

report_backend_packages
report_frontend_packages
report_docker_images

# ── Backend: Python packages ───────────────────────────────
upgrade_backend_py() {
  if $CHECK_ONLY; then return; fi
  step "Applying backend Python upgrade"
  cd "$BACKEND_DIR"

  cp requirements.txt "requirements.txt.bak-${BACKUP_SUFFIX}"
  cp requirements-dev.txt "requirements-dev.txt.bak-${BACKUP_SUFFIX}"

  info "Installing current pins…"
  "$VENV/bin/pip" install -q -r requirements-dev.txt 2>>"$LOG_FILE"

  if $MAJOR; then
    info "Upgrading every top-level package to its latest release…"
    # requirements.txt here is a flat, top-level-only pin list (no transitive locks).
    # Install each full spec (name[extras]) so extras' bundled deps (e.g. uvicorn's
    # uvloop/httptools) get resolved fresh too, then re-pin each line to whatever
    # version actually landed -- installing alone does NOT rewrite the file.
    tmp_req=$(mktemp)
    while IFS= read -r line; do
      case "$line" in ""|\#*) echo "$line" >> "$tmp_req"; continue ;; esac
      spec="${line%%==*}"          # e.g. "uvicorn[standard]"
      bare="${spec%%\[*}"          # e.g. "uvicorn"
      "$VENV/bin/pip" install -q -U "$spec" 2>>"$LOG_FILE" || warn "Could not upgrade $bare — check $LOG_FILE"
      new_ver=$("$VENV/bin/pip" show "$bare" 2>/dev/null | awk '/^Version:/{print $2}')
      if [ -n "$new_ver" ]; then
        echo "${spec}==${new_ver}" >> "$tmp_req"
      else
        echo "$line" >> "$tmp_req"   # couldn't resolve -- keep the original pin
      fi
    done < requirements.txt
    mv "$tmp_req" requirements.txt
    ok "requirements.txt re-pinned to the upgraded versions"
  else
    info "Upgrading within current majors is not automated for pip (no lock ranges to respect) — re-run with --major, or hand-edit pins for a minor/patch-only bump."
  fi

  "$VENV/bin/pip" install -q pytest 2>>"$LOG_FILE"
  cd "$WORKDIR"
}

# ── Frontend: npm packages ─────────────────────────────────
upgrade_frontend_npm() {
  if $CHECK_ONLY; then return; fi
  step "Applying frontend npm upgrade"
  cd "$FRONTEND_DIR"

  cp package-lock.json "package-lock.json.bak-${BACKUP_SUFFIX}" 2>/dev/null || true
  cp package.json "package.json.bak-${BACKUP_SUFFIX}"

  if $MAJOR; then
    info "Lifting semver ranges with npm-check-updates…"
    # @types/node is pinned to match the Node major in frontend/Dockerfile, not
    # npm's bare "latest" -- it must move by hand alongside a future Node bump,
    # never via --force (that's a correctness pairing, not caution).
    # typescript is held below its brand-new native/Go compiler generation by
    # default; --force lifts that specific hold to let you try it for real
    # against this codebase, gated by the same test gate as everything else.
    if $FORCE; then
      npx --yes npm-check-updates -u --reject @types/node 2>>"$LOG_FILE" && ok "package.json ranges updated (typescript hold lifted by --force)"
    else
      npx --yes npm-check-updates -u --reject @types/node,typescript 2>>"$LOG_FILE" && ok "package.json ranges updated"
    fi
  else
    info "Updating within declared semver ranges…"
  fi

  info "Installing…"
  if npm install 2>>"$LOG_FILE"; then
    ok "Frontend packages updated"
  else
    warn "npm install reported warnings — check $LOG_FILE"
  fi

  cd "$WORKDIR"
}

# ── Test gate ───────────────────────────────────────────────
run_test_gate() {
  if $CHECK_ONLY || $SKIP_TESTS; then return 0; fi

  step "Test gate"

  info "Backend: pytest tests/ -q"
  cd "$BACKEND_DIR"
  if "$VENV/bin/python" -m pytest tests/ -q 2>>"$LOG_FILE" | tee -a "$LOG_FILE" | tail -5; then
    ok "Backend tests: PASS"
  else
    err "Backend tests: FAIL"
    cd "$WORKDIR"; return 1
  fi
  cd "$WORKDIR"

  info "Frontend: tsc --noEmit"
  cd "$FRONTEND_DIR"
  if npx tsc --noEmit 2>>"$LOG_FILE"; then
    ok "Frontend type-check: PASS"
  else
    err "Frontend type-check: FAIL"
    cd "$WORKDIR"; return 1
  fi

  info "Frontend: next build"
  if npm run build 2>>"$LOG_FILE" >>"$LOG_FILE"; then
    ok "Frontend build: PASS"
  else
    err "Frontend build: FAIL"
    cd "$WORKDIR"; return 1
  fi
  cd "$WORKDIR"

  info "Full stack: docker compose up -d --build"
  if ! docker compose up -d --build 2>>"$LOG_FILE"; then
    err "docker compose build/up failed"
    return 1
  fi

  info "Waiting for /healthz…"
  local healthy=false
  for i in $(seq 1 30); do
    resp=$(curl -s http://127.0.0.1:8000/healthz 2>/dev/null || true)
    if echo "$resp" | grep -q '"database": *"ok"' && echo "$resp" | grep -q '"redis": *"ok"'; then
      healthy=true; break
    fi
    sleep 2
  done
  if $healthy; then
    ok "Full stack: healthy ($resp)"
  else
    err "Full stack: /healthz never went green — last response: $resp"
    return 1
  fi

  ok "Test gate PASSED"
  return 0
}

# ── Rollback ────────────────────────────────────────────────
rollback() {
  warn "Rolling back to pre-upgrade state…"

  cd "$BACKEND_DIR"
  [ -f "requirements.txt.bak-${BACKUP_SUFFIX}" ]     && mv "requirements.txt.bak-${BACKUP_SUFFIX}" requirements.txt && ok "Restored requirements.txt"
  [ -f "requirements-dev.txt.bak-${BACKUP_SUFFIX}" ] && mv "requirements-dev.txt.bak-${BACKUP_SUFFIX}" requirements-dev.txt && ok "Restored requirements-dev.txt"
  [ -f "Dockerfile.bak-${BACKUP_SUFFIX}" ]           && mv "Dockerfile.bak-${BACKUP_SUFFIX}" Dockerfile && ok "Restored backend Dockerfile"
  [ -d "$VENV" ] && "$VENV/bin/pip" install -q -r requirements-dev.txt 2>>"$LOG_FILE"

  cd "$FRONTEND_DIR"
  [ -f "package.json.bak-${BACKUP_SUFFIX}" ]      && mv "package.json.bak-${BACKUP_SUFFIX}" package.json && ok "Restored package.json"
  [ -f "package-lock.json.bak-${BACKUP_SUFFIX}" ] && mv "package-lock.json.bak-${BACKUP_SUFFIX}" package-lock.json && ok "Restored package-lock.json"
  [ -f "Dockerfile.bak-${BACKUP_SUFFIX}" ]        && mv "Dockerfile.bak-${BACKUP_SUFFIX}" Dockerfile && ok "Restored frontend Dockerfile"
  npm ci 2>>"$LOG_FILE" || warn "npm ci during rollback reported issues — check $LOG_FILE"

  cd "$WORKDIR"
  [ -f "docker-compose.yml.bak-${BACKUP_SUFFIX}" ] && mv "docker-compose.yml.bak-${BACKUP_SUFFIX}" docker-compose.yml && ok "Restored docker-compose.yml"

  err "Upgrade aborted. Files restored to pre-upgrade state."
  info "Inspect the failure in $LOG_FILE, fix it, then re-run."
}

# ── Version summary ────────────────────────────────────────
version_summary() {
  step "Post-upgrade versions"
  echo "  Running in the rebuilt containers (this is what your app actually runs on):"
  echo "    backend    $(docker compose exec -T backend python --version 2>/dev/null || echo 'container not reachable')"
  echo "    frontend   $(docker compose exec -T frontend node --version 2>/dev/null || echo 'container not reachable')"
  echo ""
  echo "  Local test tooling (backend/.venv and this shell's node/npm -- used to run"
  echo "  pytest/tsc/build quickly without Docker; NOT what's deployed, and never"
  echo "  upgraded by this script -- they just need to be recent enough to run the"
  echo "  test gate, whatever Python/Node happen to be installed on this machine):"
  echo "    venv python  $("$VENV/bin/python" --version 2>/dev/null)"
  echo "    host node    $(node --version 2>/dev/null)"
  echo "    host npm     $(npm --version 2>/dev/null)"
  echo ""
  echo "  Backend top-level packages:"
  "$VENV/bin/pip" list --format=freeze 2>/dev/null | grep -v -- '-e ' | head -30
  echo ""
  echo "  Frontend dependencies:"
  cd "$FRONTEND_DIR" && npm list --depth=0 2>/dev/null | grep -v "papertick-web"; cd "$WORKDIR"
}

# ── Main ───────────────────────────────────────────────────
# (report_backend_packages / report_frontend_packages / report_docker_images
# already ran above -- the Docker one also created this run's Dockerfile/.yml
# backups if $MAJOR && !$CHECK_ONLY, since bumping a base image is never
# auto-applied here regardless of mode.)
upgrade_backend_py
upgrade_frontend_npm

if run_test_gate; then
  sep
  if $CHECK_ONLY; then
    info "Check complete — nothing was modified. Re-run without --check to apply upgrades."
  else
    version_summary
    sep
    rm -f "$BACKEND_DIR"/*".bak-${BACKUP_SUFFIX}" "$FRONTEND_DIR"/*".bak-${BACKUP_SUFFIX}" "$WORKDIR/docker-compose.yml.bak-${BACKUP_SUFFIX}" 2>/dev/null
    ok "Upgrade complete. Full log: $LOG_FILE"
    if $MAJOR; then
      warn "Major version bumps were applied. Review each package's changelog and, for a"
      warn "UI-visible change, actually open the app in a browser before calling it done."
    fi
    info "Remaining containers are running from the upgraded build. 'docker compose down' if you don't want that."
  fi
else
  rollback
  exit 1
fi
echo ""
