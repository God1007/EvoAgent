#!/usr/bin/env bash
#
# One-click deployment for EvoAgent.
#
# Brings up the full stack (EvoAgent + PostgreSQL + Redis) via Docker Compose.
# Existing secrets in .env are preserved. `up` refuses to replace a running
# application; drain it first using docs/operations.md.
#
# Usage:
#   scripts/deploy.sh            Provision .env, build and start the stack, wait for readiness
#   scripts/deploy.sh up         Same as default
#   scripts/deploy.sh down       Stop the stack (keeps volumes/data)
#   scripts/deploy.sh destroy    Stop the stack and remove volumes (DELETES DATA)
#   scripts/deploy.sh logs       Follow application logs
#   scripts/deploy.sh status     Show container status and run a readiness check
#
# Options:
#   --no-build       Start without rebuilding the image
#   --port <PORT>    Host port to publish (default: 8080)
#   -h, --help       Show this help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${PROJECT_DIR}/.env"
ENV_EXAMPLE="${PROJECT_DIR}/.env.example"

HOST_PORT="8080"
DO_BUILD=1
COMMAND="up"

# --- pretty output ----------------------------------------------------------
if [ -t 1 ]; then
  C_RESET="\033[0m"; C_BOLD="\033[1m"; C_RED="\033[31m"
  C_GREEN="\033[32m"; C_YELLOW="\033[33m"; C_BLUE="\033[34m"
else
  C_RESET=""; C_BOLD=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""
fi

info()  { printf "${C_BLUE}==>${C_RESET} %s\n" "$*"; }
ok()    { printf "${C_GREEN}✓${C_RESET} %s\n" "$*"; }
warn()  { printf "${C_YELLOW}!${C_RESET} %s\n" "$*"; }
err()   { printf "${C_RED}✗ %s${C_RESET}\n" "$*" >&2; }
die()   { err "$*"; exit 1; }

usage() {
  sed -n '3,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# --- argument parsing -------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    up|down|destroy|logs|status) COMMAND="$1" ;;
    --no-build) DO_BUILD=0 ;;
    --port) shift; [ $# -gt 0 ] || die "--port requires a value"; HOST_PORT="$1" ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1 (use --help)" ;;
  esac
  shift
done

# --- docker / compose detection --------------------------------------------
detect_compose() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
  else
    die "Docker Compose not found. Install Docker Desktop or the compose plugin."
  fi
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "Docker is not installed. See https://docs.docker.com/get-docker/"
  docker info >/dev/null 2>&1 || die "Docker daemon is not running. Start Docker and retry."
  detect_compose
}

# --- .env helpers -----------------------------------------------------------
# Read a KEY's value from .env (empty if unset/blank).
env_get() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n1
}

# Set KEY=VALUE in .env, replacing an existing line or appending.
env_set() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  if [ -f "$ENV_FILE" ] && grep -q "^${key}=" "$ENV_FILE"; then
    # Use awk to avoid sed delimiter issues with special chars in value.
    awk -v k="$key" -v v="$value" \
      'BEGIN{FS=OFS="="} $1==k{print k"="v; next} {print}' \
      "$ENV_FILE" > "$tmp"
  else
    [ -f "$ENV_FILE" ] && cat "$ENV_FILE" > "$tmp"
    printf '%s=%s\n' "$key" "$value" >> "$tmp"
  fi
  mv "$tmp" "$ENV_FILE"
}

# Generate a URL-safe secret (hex) of N bytes.
gen_secret() {
  local bytes="${1:-32}"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$bytes"
  else
    LC_ALL=C tr -dc 'a-f0-9' < /dev/urandom | head -c $((bytes * 2))
    echo
  fi
}

# Generate an alphanumeric password of N chars (default 20).
gen_password() {
  local len="${1:-20}"
  LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$len"
  echo
}

provision_env() {
  if [ ! -f "$ENV_FILE" ]; then
    [ -f "$ENV_EXAMPLE" ] || die "Missing $ENV_EXAMPLE; cannot bootstrap .env"
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    ok "Created .env from .env.example"
  else
    info "Reusing existing .env"
  fi

  env_set EVOAGENT_AUTH_REQUIRED "true"

  if [ -z "$(env_get EVOAGENT_AUTH_SECRET)" ]; then
    env_set EVOAGENT_AUTH_SECRET "$(gen_secret 32)"
    ok "Generated EVOAGENT_AUTH_SECRET"
  fi

  if [ -z "$(env_get EVOAGENT_BOOTSTRAP_ADMIN_USERNAME)" ]; then
    env_set EVOAGENT_BOOTSTRAP_ADMIN_USERNAME "admin"
  fi

  if [ -z "$(env_get EVOAGENT_BOOTSTRAP_ADMIN_PASSWORD)" ]; then
    GENERATED_ADMIN_PASSWORD="$(gen_password 20)"
    env_set EVOAGENT_BOOTSTRAP_ADMIN_PASSWORD "$GENERATED_ADMIN_PASSWORD"
    ok "Generated bootstrap admin password"
  fi

  if [ -z "$(env_get EVOAGENT_GITHUB_WEBHOOK_SECRET)" ]; then
    env_set EVOAGENT_GITHUB_WEBHOOK_SECRET "$(gen_secret 24)"
    ok "Generated EVOAGENT_GITHUB_WEBHOOK_SECRET"
  fi
}

# --- readiness check --------------------------------------------------------
wait_for_ready() {
  local url="http://127.0.0.1:${HOST_PORT}/ready"
  local attempts=60 i=1
  info "Waiting for ${url} ..."
  while [ "$i" -le "$attempts" ]; do
    if curl -fsS "$url" >/dev/null 2>&1; then
      ok "Service is ready"
      curl -fsS "$url" 2>/dev/null || true
      echo
      return 0
    fi
    sleep 2
    i=$((i + 1))
  done
  err "Readiness check did not pass after $((attempts * 2))s."
  warn "Inspect logs with: ${COMPOSE} logs evoagent"
  return 1
}

print_summary() {
  echo
  printf "${C_BOLD}EvoAgent is up.${C_RESET}\n"
  echo "  URL:       http://127.0.0.1:${HOST_PORT}"
  echo "  Ready:     http://127.0.0.1:${HOST_PORT}/ready"
  echo "  Dashboard: http://127.0.0.1:${HOST_PORT}/"
  echo "  Auth:      enabled"
  echo "  Admin:     $(env_get EVOAGENT_BOOTSTRAP_ADMIN_USERNAME)"
  if [ -n "${GENERATED_ADMIN_PASSWORD:-}" ]; then
    printf "  Password:  ${C_BOLD}%s${C_RESET}  (generated once, saved in .env)\n" "$GENERATED_ADMIN_PASSWORD"
  else
    echo "  Password:  (see EVOAGENT_BOOTSTRAP_ADMIN_PASSWORD in .env)"
  fi
  echo
  echo "  Stop:      scripts/deploy.sh down"
  echo "  Logs:      scripts/deploy.sh logs"
}

# --- commands ---------------------------------------------------------------
cmd_up() {
  require_docker
  if [ -n "$(cd "$PROJECT_DIR" && $COMPOSE ps -q evoagent)" ]; then
    die "Refusing to replace a running EvoAgent; pause intake, drain it, then run 'scripts/deploy.sh down' before 'up'."
  fi
  provision_env
  export EVOAGENT_HOST_PORT="$HOST_PORT"

  local build_flag=""
  [ "$DO_BUILD" -eq 1 ] && build_flag="--build"

  info "Starting stack (${COMPOSE} up ${build_flag} -d)"
  (cd "$PROJECT_DIR" && EVOAGENT_HOST_PORT="$HOST_PORT" $COMPOSE up $build_flag -d)

  wait_for_ready || exit 1
  print_summary
}

cmd_down() {
  require_docker
  info "Stopping stack (data volumes preserved)"
  (cd "$PROJECT_DIR" && $COMPOSE down)
  ok "Stopped"
}

cmd_destroy() {
  require_docker
  warn "This will remove containers AND data volumes (Postgres/Redis)."
  printf "Type 'yes' to continue: "
  read -r reply
  [ "$reply" = "yes" ] || { info "Aborted"; exit 0; }
  (cd "$PROJECT_DIR" && $COMPOSE down -v)
  ok "Stack and volumes removed"
}

cmd_logs() {
  require_docker
  (cd "$PROJECT_DIR" && $COMPOSE logs -f evoagent)
}

cmd_status() {
  require_docker
  (cd "$PROJECT_DIR" && $COMPOSE ps)
  echo
  if curl -fsS "http://127.0.0.1:${HOST_PORT}/ready" 2>/dev/null; then
    echo; ok "Ready"
  else
    warn "Readiness endpoint not ready on port ${HOST_PORT}"
  fi
}

case "$COMMAND" in
  up)      cmd_up ;;
  down)    cmd_down ;;
  destroy) cmd_destroy ;;
  logs)    cmd_logs ;;
  status)  cmd_status ;;
  *)       die "Unknown command: $COMMAND" ;;
esac
