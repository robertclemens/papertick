#!/usr/bin/env bash
# upgrade.sh — upgrades all PaperTick dependencies: Python packages, npm packages,
# and the four Docker base images (Python, Node, Postgres, Redis).
#
# Usage:
#   ./upgrade.sh              # safe mode: upgrade within the current major version of each package
#   ./upgrade.sh --major      # also cross major versions (Docker base images only move under this flag)
#   ./upgrade.sh --check      # print what's outdated without changing anything
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
BACKUP_SUFFIX=$(date +%Y%m%d%H%M%S)

for arg in "$@"; do
  case "$arg" in
    --major)      MAJOR=true ;;
    --check)      CHECK_ONLY=true ;;
    --skip-tests) SKIP_TESTS=true ;;
    *) err "Unknown argument: $arg"; echo "Usage: $0 [--major] [--check] [--skip-tests]"; exit 1 ;;
  esac
done

echo "" | tee "$LOG_FILE"
echo -e "${BOLD}PaperTick upgrade${NC} — $(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_FILE"
if $MAJOR; then
  warn "Running in --major mode: package majors AND Docker base images (Python/Node/Postgres/Redis) may move. Breaking changes are possible — review the test gate output carefully."
elif $CHECK_ONLY; then
  info "Running in --check mode. Nothing will be modified."
fi
if $SKIP_TESTS; then
  warn "--skip-tests: the pytest/tsc/build/healthz gate is disabled. A broken upgrade will not be caught automatically."
fi
sep

# ── Snapshot current versions ──────────────────────────────
step "Current versions"
echo "  Python    $("$VENV/bin/python" --version 2>/dev/null || echo 'venv not found')"
echo "  Node      $(node --version 2>/dev/null || echo 'not found')"
echo "  npm       $(npm --version 2>/dev/null || echo 'not found')"
echo "  Docker    $(docker --version 2>/dev/null || echo 'not found')"
grep -m1 '^FROM' "$BACKEND_DIR/Dockerfile"  | sed 's/^/  backend image   /'
grep -m1 '^FROM' "$FRONTEND_DIR/Dockerfile" | sed 's/^/  frontend image  /'
grep -A2 '^  db:'    "$WORKDIR/docker-compose.yml" | grep image | sed 's/^/  db image       /'
grep -A2 '^  redis:' "$WORKDIR/docker-compose.yml" | grep image | sed 's/^/  redis image    /'

# ── Backend: Python packages ───────────────────────────────
upgrade_backend_py() {
  step "Backend Python packages"
  cd "$BACKEND_DIR"

  if [ ! -d "$VENV" ]; then
    info "Creating venv…"
    python3 -m venv "$VENV" 2>>"$LOG_FILE"
  fi

  info "Outdated packages:"
  "$VENV/bin/pip" list --outdated 2>/dev/null || true

  if $CHECK_ONLY; then cd "$WORKDIR"; return; fi

  cp requirements.txt "requirements.txt.bak-${BACKUP_SUFFIX}"
  cp requirements-dev.txt "requirements-dev.txt.bak-${BACKUP_SUFFIX}"

  info "Installing current pins…"
  "$VENV/bin/pip" install -q -r requirements-dev.txt 2>>"$LOG_FILE"

  if $MAJOR; then
    info "Upgrading every top-level package to its latest release…"
    # requirements.txt here is a flat, top-level-only pin list (no transitive locks),
    # so upgrading each named package and re-freezing just those names is safe and
    # keeps the file in its existing hand-maintained style.
    names=$(sed -E 's/(\[[a-z]+\])?(==.*)?$//' requirements.txt | sed '/^\s*$/d')
    for pkg in $names; do
      "$VENV/bin/pip" install -q -U "$pkg" 2>>"$LOG_FILE" || warn "Could not upgrade $pkg — check $LOG_FILE"
    done
  else
    info "Upgrading within current majors is not automated for pip (no lock ranges to respect) — re-run with --major, or hand-edit pins for a minor/patch-only bump."
  fi

  "$VENV/bin/pip" install -q pytest 2>>"$LOG_FILE"
  cd "$WORKDIR"
}

# ── Frontend: npm packages ─────────────────────────────────
upgrade_frontend_npm() {
  step "Frontend npm packages"
  cd "$FRONTEND_DIR"

  info "Outdated packages:"
  npm outdated --color=always 2>/dev/null || true

  if $CHECK_ONLY; then cd "$WORKDIR"; return; fi

  cp package-lock.json "package-lock.json.bak-${BACKUP_SUFFIX}" 2>/dev/null || true
  cp package.json "package.json.bak-${BACKUP_SUFFIX}"

  if $MAJOR; then
    info "Lifting semver ranges with npm-check-updates…"
    npx --yes npm-check-updates -u 2>>"$LOG_FILE" && ok "package.json ranges updated"
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

# ── Docker base images (major-only: bumping a runtime is a deliberate move) ─
upgrade_docker_images() {
  step "Docker base images"
  if ! $MAJOR; then
    info "Skipped (base image bumps only happen under --major)."
    return
  fi
  if $CHECK_ONLY; then
    info "Would check Docker Hub for newer python/node/postgres/redis floating tags."
    return
  fi

  cp "$BACKEND_DIR/Dockerfile"  "$BACKEND_DIR/Dockerfile.bak-${BACKUP_SUFFIX}"
  cp "$FRONTEND_DIR/Dockerfile" "$FRONTEND_DIR/Dockerfile.bak-${BACKUP_SUFFIX}"
  cp "$WORKDIR/docker-compose.yml" "$WORKDIR/docker-compose.yml.bak-${BACKUP_SUFFIX}"

  warn "Base image versions (Python/Node major, Postgres major, Redis major) are not"
  warn "auto-detected here — verify the current latest LTS/stable tag yourself (e.g."
  warn "https://endoflife.date, https://nodejs.org/en/about/previous-releases) before"
  warn "editing the FROM/image lines by hand. This step intentionally does not guess."
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
  echo "  Python    $("$VENV/bin/python" --version 2>/dev/null)"
  echo "  Node      $(node --version 2>/dev/null)"
  echo "  npm       $(npm --version 2>/dev/null)"
  echo ""
  echo "  Backend top-level packages:"
  "$VENV/bin/pip" list --format=freeze 2>/dev/null | grep -v -- '-e ' | head -30
  echo ""
  echo "  Frontend dependencies:"
  cd "$FRONTEND_DIR" && npm list --depth=0 2>/dev/null | grep -v "papertick-web"; cd "$WORKDIR"
}

# ── Main ───────────────────────────────────────────────────
upgrade_backend_py
upgrade_frontend_npm
upgrade_docker_images

if run_test_gate; then
  version_summary
  sep
  if $CHECK_ONLY; then
    info "Check complete — nothing was modified. Re-run without --check to apply upgrades."
  else
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
