#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR=/opt/factorio-web
LOG_DIR=/var/log/factorio-web
SERVICE_NAME=factorio-web
SERVICE_FILE=/etc/systemd/system/${SERVICE_NAME}.service

# ── colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
die()   { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }
step()  { echo -e "\n${CYAN}▶${NC} $*"; }

# ── root check ───────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && die "Please run as root:  sudo bash install.sh"

# ── must run from project directory ──────────────────────────────────────────
[[ -f app.py && -d templates ]] || die "Run this script from the factorio-server-manager directory."

# ── port prompt ──────────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}Factorio Server Manager — installer${NC}"
echo "────────────────────────────────────"
read -rp "Web UI port [8080]: " PORT
PORT=${PORT:-8080}
[[ "$PORT" =~ ^[0-9]+$ && "$PORT" -ge 1 && "$PORT" -le 65535 ]] || die "Invalid port: $PORT"

# ── detect distro / package manager ──────────────────────────────────────────
step "Detecting distribution..."

if [[ -f /etc/os-release ]]; then
    source /etc/os-release
    info "Detected: ${PRETTY_NAME:-$ID}"
fi

if command -v apt-get &>/dev/null; then
    PKG_MGR=apt
    PYTHON_PKGS="python3 python3-pip python3-venv"
elif command -v dnf &>/dev/null; then
    PKG_MGR=dnf
    PYTHON_PKGS="python3 python3-pip"
elif command -v yum &>/dev/null; then
    PKG_MGR=yum
    PYTHON_PKGS="python3 python3-pip"
elif command -v pacman &>/dev/null; then
    PKG_MGR=pacman
    PYTHON_PKGS="python python-pip"
elif command -v zypper &>/dev/null; then
    PKG_MGR=zypper
    PYTHON_PKGS="python3 python3-pip"
else
    die "No supported package manager found (apt/dnf/yum/pacman/zypper). Install Python 3.10+ manually and re-run."
fi

info "Package manager: $PKG_MGR"

# ── install system dependencies ───────────────────────────────────────────────
step "Installing system dependencies..."

case $PKG_MGR in
    apt)
        apt-get update -qq
        apt-get install -y -qq $PYTHON_PKGS
        ;;
    dnf)  dnf install -y -q $PYTHON_PKGS ;;
    yum)  yum install -y -q $PYTHON_PKGS ;;
    pacman) pacman -Sy --noconfirm $PYTHON_PKGS ;;
    zypper) zypper install -y $PYTHON_PKGS ;;
esac

# ── check Python version ──────────────────────────────────────────────────────
PYTHON=$(command -v python3 || command -v python)
PY_VER=$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$($PYTHON -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$($PYTHON -c 'import sys; print(sys.version_info.minor)')

[[ $PY_MAJOR -ge 3 && $PY_MINOR -ge 10 ]] || die "Python 3.10+ required (found $PY_VER)."
info "Python $PY_VER"

# ── detect upgrade vs fresh install ──────────────────────────────────────────
UPGRADE=false
if [[ -f "$INSTALL_DIR/app.py" ]]; then
    UPGRADE=true
    warn "Existing installation found — upgrading (your users and settings are kept)."
fi

# ── copy application files ────────────────────────────────────────────────────
step "Installing application files to $INSTALL_DIR..."

mkdir -p "$INSTALL_DIR"
cp app.py "$INSTALL_DIR/app.py"
cp requirements.txt "$INSTALL_DIR/requirements.txt"
rm -rf "$INSTALL_DIR/templates"
cp -r templates "$INSTALL_DIR/templates"
mkdir -p "$INSTALL_DIR/static"
cp -r static/. "$INSTALL_DIR/static/"

info "Files copied."

# ── python virtual environment ────────────────────────────────────────────────
step "Setting up Python virtual environment..."

if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    $PYTHON -m venv "$INSTALL_DIR/venv"
fi

"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" gunicorn

info "Packages installed."

# ── directories and permissions ───────────────────────────────────────────────
mkdir -p "$LOG_DIR"
chmod 750 "$LOG_DIR"

# ── systemd service ───────────────────────────────────────────────────────────
step "Creating systemd service..."

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Factorio Web Manager
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/gunicorn -w 1 -b 0.0.0.0:$PORT app:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

if $UPGRADE; then
    step "Restarting service..."
    systemctl restart "$SERVICE_NAME"
else
    step "Enabling and starting service..."
    systemctl enable --now "$SERVICE_NAME"
fi

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}────────────────────────────────────────────────${NC}"
echo -e "${GREEN}  Factorio Web Manager installed successfully!${NC}"
echo -e "${GREEN}────────────────────────────────────────────────${NC}"
echo ""
echo -e "  URL:     ${CYAN}http://$(hostname -I | awk '{print $1}'):${PORT}${NC}"
echo -e "  Logs:    journalctl -u $SERVICE_NAME -f"
echo -e "  Status:  systemctl status $SERVICE_NAME"
echo ""
if ! $UPGRADE; then
    echo -e "  ${YELLOW}Open the URL above to create your admin account.${NC}"
    echo ""
fi
