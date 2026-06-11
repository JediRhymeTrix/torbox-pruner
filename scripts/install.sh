#!/usr/bin/env bash
# install.sh — bootstrap torbox-pruner on a new machine
#
# What this does:
#   1. Validates required env vars (TORBOX_API_KEY, TORBOX_TELEGRAM_CHAT_ID)
#   2. Writes .env from those vars (if not already present)
#   3. Copies config.example.json → config.json (if not already present)
#   4. Creates empty queue.jsonl and notifications.jsonl
#   5. Installs the Goose recipe to ~/.config/goose/recipes/
#   6. Installs the Goose schedule to ~/.config/goose/schedules/
#   7. Sets correct permissions on run-prune.sh
#
# Usage:
#   TORBOX_API_KEY=xxx TORBOX_TELEGRAM_CHAT_ID=yyy bash scripts/install.sh
#
# Or, to be prompted interactively:
#   bash scripts/install.sh --interactive
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GOOSE_CFG="${HOME}/.config/goose"
RECIPES_DIR="${GOOSE_CFG}/recipes"
SCHEDULES_DIR="${GOOSE_CFG}/schedules"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*" >&2; exit 1; }

# ── Interactive mode ──────────────────────────────────────────────────────────
if [[ "${1:-}" == "--interactive" ]]; then
  read -rp "TorBox API key: " TORBOX_API_KEY
  read -rp "Telegram chat ID: " TORBOX_TELEGRAM_CHAT_ID
  export TORBOX_API_KEY TORBOX_TELEGRAM_CHAT_ID
fi

# ── Validate env vars ─────────────────────────────────────────────────────────
[[ -n "${TORBOX_API_KEY:-}" ]]        || die "TORBOX_API_KEY is not set. See .env.example."
[[ -n "${TORBOX_TELEGRAM_CHAT_ID:-}" ]] || die "TORBOX_TELEGRAM_CHAT_ID is not set. See .env.example."

echo "Installing TorBox Pruner from: ${SCRIPT_DIR}"

# ── .env ─────────────────────────────────────────────────────────────────────
ENV_FILE="${SCRIPT_DIR}/.env"
if [[ -f "${ENV_FILE}" ]]; then
  warn ".env already exists — skipping (edit manually if needed)"
else
  cat > "${ENV_FILE}" <<EOF
TORBOX_API_KEY=${TORBOX_API_KEY}
TORBOX_TELEGRAM_CHAT_ID=${TORBOX_TELEGRAM_CHAT_ID}
EOF
  chmod 600 "${ENV_FILE}"
  ok ".env written (chmod 600)"
fi

# ── config.json ───────────────────────────────────────────────────────────────
CFG_FILE="${SCRIPT_DIR}/config.json"
if [[ -f "${CFG_FILE}" ]]; then
  warn "config.json already exists — skipping"
else
  cp "${SCRIPT_DIR}/config.example.json" "${CFG_FILE}"
  ok "config.json created from config.example.json"
fi

# ── Runtime files ─────────────────────────────────────────────────────────────
touch "${SCRIPT_DIR}/queue.jsonl"       && ok "queue.jsonl ready"
touch "${SCRIPT_DIR}/notifications.jsonl" && ok "notifications.jsonl ready"
mkdir -p "${SCRIPT_DIR}/logs"           && ok "logs/ directory ready"

# ── run-prune.sh permissions ──────────────────────────────────────────────────
chmod +x "${SCRIPT_DIR}/run-prune.sh"
chmod +x "${SCRIPT_DIR}/scripts/"*.sh
ok "Scripts marked executable"

# ── Python sanity check ───────────────────────────────────────────────────────
python3 -c "import json, sys, pathlib; print('python3 OK')" || die "python3 not found or broken"
python3 -c "import py_compile; py_compile.compile('${SCRIPT_DIR}/prune.py', doraise=True)" \
  && ok "prune.py syntax OK"
python3 -c "import py_compile; py_compile.compile('${SCRIPT_DIR}/notify.py', doraise=True)" \
  && ok "notify.py syntax OK"

# ── Goose recipe ──────────────────────────────────────────────────────────────
mkdir -p "${RECIPES_DIR}" "${SCHEDULES_DIR}"

RECIPE_SRC="${SCRIPT_DIR}/goose/recipe.yaml"
RECIPE_DST="${RECIPES_DIR}/torbox-pruner.yaml"

# Substitute INSTALL_DIR placeholder with the actual install path
sed "s|INSTALL_DIR|${SCRIPT_DIR}|g" "${RECIPE_SRC}" > "${RECIPE_DST}"
ok "Goose recipe installed → ${RECIPE_DST}"

# ── Goose schedule ────────────────────────────────────────────────────────────
SCHEDULE_SRC="${SCRIPT_DIR}/goose/schedule.yaml"
SCHEDULE_DST="${SCHEDULES_DIR}/torbox-pruner.yaml"
cp "${SCHEDULE_SRC}" "${SCHEDULE_DST}"
ok "Goose schedule installed → ${SCHEDULE_DST}"

# ── Dry-run verification ──────────────────────────────────────────────────────
echo ""
echo "Running a dry-run to verify everything works..."
TORBOX_API_KEY="${TORBOX_API_KEY}" \
TORBOX_TELEGRAM_CHAT_ID="${TORBOX_TELEGRAM_CHAT_ID}" \
  python3 "${SCRIPT_DIR}/prune.py" --dry-run --no-notify \
  && ok "Dry-run passed" \
  || die "Dry-run failed — check config and API key"

echo ""
echo -e "${GREEN}✓ Installation complete.${NC}"
echo ""
echo "Next steps:"
echo "  • The Goose GUI scheduler will pick up the schedule automatically."
echo "    Open Goose and check the Scheduler tab."
echo "  • To run manually: python3 ${SCRIPT_DIR}/prune.py"
echo "  • To add a torrent to the queue:"
echo "      ${SCRIPT_DIR}/scripts/add-to-queue.sh \"magnet:?xt=urn:btih:...\""
echo ""
