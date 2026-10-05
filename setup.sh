#!/usr/bin/env bash
# ==============================================================================
# setup.sh — Automated Production Installer for ChronoMed on a Clean Ubuntu VPS
# ==============================================================================
# This script:
# 1. Prompts interactively for your GitHub repo URL and production domain name.
# 2. Installs all required system dependencies (Git, Caddy, Docker, Node.js 22).
# 3. Configures UFW firewall for SSH, HTTP (80), and HTTPS (443).
# 4. Clones the project repository into /opt/chronomed.
# 5. Generates production secrets (.env with secure random JWT secret).
# 6. Prepares SQLite WAL directories with container UID 10001 permissions.
# 7. Builds and starts the FastAPI backend container via Docker Compose.
# 8. Compiles the Vue 3 PWA frontend and syncs it to /var/www/medical-app.
# 9. Configures Host Caddy with auto-HTTPS, reverse proxy, and PWA caching.
# 10. Validates end-to-end health and outputs a deployment summary.
# ==============================================================================

set -euo pipefail

# --- Color formatting helpers ---
BOLD='\033[1m'
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()    { echo -e "${CYAN}${BOLD}==>${NC} ${BOLD}$1${NC}"; }
success() { echo -e "${GREEN}${BOLD}✔${NC} ${GREEN}$1${NC}"; }
warn()    { echo -e "${YELLOW}${BOLD}⚠${NC} ${YELLOW}$1${NC}"; }
error()   { echo -e "${RED}${BOLD}✖${NC} ${RED}$1${NC}" >&2; }

# --- Ensure script is executed as root / sudo ---
if [[ $EUID -ne 0 ]]; then
  error "This installer must be run as root or with sudo privileges."
  echo "Please run: sudo bash $0"
  exit 1
fi

clear || true
echo -e "${CYAN}${BOLD}"
cat << 'EOF'
   _____ _                             __  __          _ 
  / ____| |                           |  \/  |        | |
 | |    | |__  _ __ ___  _ __   ___   | \  / | ___  __| |
 | |    | '_ \| '__/ _ \| '_ \ / _ \  | |\/| |/ _ \/ _` |
 | |____| | | | | | (_) | | | | (_) | | |  | |  __/ (_| |
  \_____|_| |_|_|  \___/|_| |_|\___/  |_|  |_|\___|\__,_|
      Anesthesiology & ICU Production VPS Installer
EOF
echo -e "${NC}"

echo -e "Welcome! This installer will configure your clean Ubuntu VPS for production."
echo -e "Make sure your domain's DNS A/AAAA records point to this server's public IP address.\n"

# ==============================================================================
# 1. INTERACTIVE CONFIGURATION PROMPTS
# ==============================================================================

# Prompt for GitHub Repository URL
if [[ -z "${REPO_URL:-}" ]]; then
  while true; do
    echo -e "${BOLD}Enter your GitHub repository link:${NC}"
    echo -e "  Examples: https://github.com/your-user/your-repo.git"
    echo -e "            git@github.com:your-user/your-repo.git"
    read -r -p "GitHub Repo URL: " REPO_URL
    REPO_URL=$(echo "$REPO_URL" | xargs)
    if [[ -n "$REPO_URL" ]]; then
      break
    else
      warn "Repository URL cannot be empty. Please enter a valid URL."
    fi
  done
fi

# Prompt for Branch (default: main)
if [[ -z "${GIT_BRANCH:-}" ]]; then
  read -r -p "Git branch to deploy [default: main]: " GIT_BRANCH
  GIT_BRANCH=${GIT_BRANCH:-main}
  GIT_BRANCH=$(echo "$GIT_BRANCH" | xargs)
fi

# Prompt for Domain Name
if [[ -z "${DOMAIN:-}" ]]; then
  while true; do
    echo -e "\n${BOLD}Enter your production domain name:${NC}"
    echo -e "  Examples: schedule.hospital.org, app.example.com"
    read -r -p "Domain: " DOMAIN
    DOMAIN=$(echo "$DOMAIN" | xargs)
    # Strip protocol prefix or trailing slash if entered by accident
    DOMAIN="${DOMAIN#http://}"
    DOMAIN="${DOMAIN#https://}"
    DOMAIN="${DOMAIN%/}"
    if [[ -n "$DOMAIN" ]]; then
      break
    else
      warn "Domain name cannot be empty."
    fi
  done
fi

APP_DIR="/opt/chronomed"
WEB_ROOT="/var/www/medical-app"
CONTAINER_UID=10001
CONTAINER_GID=10001

echo -e "\n${CYAN}================ Deployment Summary ================${NC}"
echo -e " Repository:    ${BOLD}${REPO_URL}${NC} (branch: ${GIT_BRANCH})"
echo -e " Domain:        ${BOLD}${DOMAIN}${NC} (automatic Let's Encrypt SSL)"
echo -e " App Directory: ${BOLD}${APP_DIR}${NC}"
echo -e " Web Root:      ${BOLD}${WEB_ROOT}${NC}"
echo -e "${CYAN}====================================================${NC}\n"

read -r -p "Proceed with installation? [Y/n]: " CONFIRM
CONFIRM=${CONFIRM:-Y}
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
  warn "Installation aborted by user."
  exit 0
fi

# ==============================================================================
# 2. UPDATE SYSTEM & INSTALL ESSENTIAL SYSTEM PACKAGES
# ==============================================================================
info "[1/9] Updating package lists and installing core utilities..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  curl \
  wget \
  git \
  rsync \
  openssl \
  ca-certificates \
  gnupg \
  lsb-release \
  ufw \
  tar \
  gzip
success "Core system utilities installed."

# ==============================================================================
# 3. INSTALL OFFICIAL CADDY WEB SERVER
# ==============================================================================
info "[2/9] Installing Caddy web server (official Debian/Ubuntu repo)..."
if ! command -v caddy &>/dev/null; then
  mkdir -p /etc/apt/keyrings
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
  curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
  success "Caddy installed: $(caddy version | head -n 1)"
else
  success "Caddy is already installed: $(caddy version | head -n 1)"
fi

# Ensure Caddy log directory exists
mkdir -p /var/log/caddy
chown -R caddy:caddy /var/log/caddy || true

# ==============================================================================
# 4. INSTALL DOCKER & DOCKER COMPOSE PLUGIN
# ==============================================================================
info "[3/9] Installing Docker and Docker Compose..."
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
  success "Docker installed: $(docker --version)"
else
  success "Docker is already installed: $(docker --version)"
fi

# Add invoking non-root user (if executed via sudo) to the docker group
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  usermod -aG docker "$SUDO_USER" || true
fi

# ==============================================================================
# 5. INSTALL NODE.JS 22 LTS (For building the Vue 3 PWA)
# ==============================================================================
info "[4/9] Installing Node.js 22 LTS & npm..."
CURRENT_NODE_MAJOR=0
if command -v node &>/dev/null; then
  CURRENT_NODE_MAJOR=$(node -v | cut -d'.' -f1 | tr -d 'v')
fi

if [[ "$CURRENT_NODE_MAJOR" -lt 20 ]]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
  success "Node.js installed: $(node -v) (npm $(npm -v))"
else
  success "Node.js is already up-to-date: $(node -v)"
fi

# ==============================================================================
# 6. CONFIGURE UFW FIREWALL
# ==============================================================================
info "[5/9] Configuring firewall rules (SSH, HTTP, HTTPS)..."
ufw allow OpenSSH comment 'Allow SSH' || ufw allow 22/tcp || true
ufw allow 80/tcp comment 'Allow Caddy HTTP (ACME challenge)'
ufw allow 443/tcp comment 'Allow Caddy HTTPS'
# Enable UFW non-interactively if inactive
if ! ufw status | grep -qw "active"; then
  echo "y" | ufw enable || true
fi
success "Firewall configured (OpenSSH, 80/tcp, 443/tcp enabled)."

# ==============================================================================
# 7. CLONE OR UPDATE GITHUB REPOSITORY IN /opt/chronomed
# ==============================================================================
info "[6/9] Fetching application code from GitHub into ${APP_DIR}..."
if [[ -d "${APP_DIR}/.git" ]]; then
  warn "${APP_DIR} already contains a git repository. Pulling latest '${GIT_BRANCH}'..."
  cd "${APP_DIR}"
  git fetch origin
  git checkout "${GIT_BRANCH}"
  git pull origin "${GIT_BRANCH}"
else
  if [[ -d "${APP_DIR}" ]]; then
    warn "Directory ${APP_DIR} exists but is not a git repo. Backing up to ${APP_DIR}.bak.$(date +%s)..."
    mv "${APP_DIR}" "${APP_DIR}.bak.$(date +%s)"
  fi
  mkdir -p "${APP_DIR}"
  git clone -b "${GIT_BRANCH}" "${REPO_URL}" "${APP_DIR}"
fi
success "Repository synchronized."

# If non-root sudo user invoked the script, grant them ownership of the repo files
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  chown -R "${SUDO_USER}:${SUDO_USER}" "${APP_DIR}"
fi

# ==============================================================================
# 8. CONFIGURE SECRETS (.env) AND SQLITE WAL DIRECTORY PERMISSIONS
# ==============================================================================
info "[7/9] Configuring SQLite storage, environment secrets, and backend container..."

# Prepare persistent SQLite directory for UID 10001 (matching backend/Dockerfile)
SQLITE_DATA_DIR="${APP_DIR}/data/sqlite"
mkdir -p "${SQLITE_DATA_DIR}"
chown -R "${CONTAINER_UID}:${CONTAINER_GID}" "${SQLITE_DATA_DIR}"
chmod 2770 "${SQLITE_DATA_DIR}"
find "${SQLITE_DATA_DIR}" -type f -exec chmod 660 {} + || true

# Generate secure .env file if missing
ENV_FILE="${APP_DIR}/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  info "Generating new cryptographic secret in ${ENV_FILE}..."
  JWT_SECRET=$(openssl rand -hex 32)
  cat << EOF > "$ENV_FILE"
# ChronoMed Production Environment
JWT_SECRET_KEY=${JWT_SECRET}
SQLITE_DB_PATH=/app/data/medical_scheduler.db
SQLITE_ECHO=false
ACCESS_TOKEN_EXPIRE_MINUTES=720
DOMAIN=${DOMAIN}
EOF
  chmod 600 "$ENV_FILE"
  success "Generated ${ENV_FILE} with random 64-char JWT key."
else
  success "Existing ${ENV_FILE} preserved."
fi

# Build and start FastAPI Docker container
cd "${APP_DIR}"
info "Building and launching backend Docker container..."
docker compose up -d --build --remove-orphans

# Poll backend health endpoint until ready (up to 45s)
info "Waiting for backend health check at http://127.0.0.1:8000/api/v1/health..."
BACKEND_HEALTHY=false
for i in {1..15}; do
  if curl -fsS http://127.0.0.1:8000/api/v1/health &>/dev/null; then
    BACKEND_HEALTHY=true
    break
  fi
  sleep 3
done

if [[ "$BACKEND_HEALTHY" = true ]]; then
  success "FastAPI backend is healthy: $(curl -fsS http://127.0.0.1:8000/api/v1/health)"
else
  warn "Backend did not respond immediately. Check logs with: docker compose logs backend"
fi

# ==============================================================================
# 9. COMPILE VUE 3 PWA FRONTEND
# ==============================================================================
info "[8/9] Compiling Vue 3 PWA frontend bundle..."
cd "${APP_DIR}/frontend"

if [[ -f "package-lock.json" ]]; then
  npm ci
else
  npm install
fi

npm run build

# Deploy compiled bundle to host web root for Caddy
mkdir -p "${WEB_ROOT}"
rsync -av --delete dist/ "${WEB_ROOT}/"
chown -R caddy:caddy "${WEB_ROOT}"
chmod -R 755 "${WEB_ROOT}"
success "Frontend built and published to ${WEB_ROOT}."

# ==============================================================================
# 10. CONFIGURE AND RELOAD HOST CADDY
# ==============================================================================
info "[9/9] Configuring Host Caddy with auto-HTTPS for ${DOMAIN}..."

CADDYFILE_PATH="/etc/caddy/Caddyfile"
if [[ -f "$CADDYFILE_PATH" ]]; then
  cp "$CADDYFILE_PATH" "${CADDYFILE_PATH}.bak.$(date +%s)"
fi

cat << EOF > "$CADDYFILE_PATH"
# ==============================================================================
# /etc/caddy/Caddyfile — Host Caddy Production Configuration for ChronoMed
# Generated automatically by setup.sh on $(date -u +"%Y-%m-%d %H:%M:%S UTC")
# ==============================================================================

${DOMAIN} {
    # 1. Enable high-efficiency Zstandard and Gzip response compression
    encode zstd gzip

    # 2. Security headers suitable for clinical web & mobile PWA deployments
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "SAMEORIGIN"
        Referrer-Policy "strict-origin-when-cross-origin"
        -Server
    }

    # 3. Reverse Proxy FastAPI REST API & OpenAPI Docs to Docker Loopback
    @backend_routes {
        path /api/* /docs* /redoc* /openapi.json
    }
    handle @backend_routes {
        reverse_proxy 127.0.0.1:8000 {
            header_up Host {host}
            header_up X-Real-IP {remote_host}
            header_up X-Forwarded-For {remote_host}
            header_up X-Forwarded-Proto {scheme}
        }
    }

    # 4. Serve Compiled Vue 3 PWA Static Bundle from ${WEB_ROOT}
    handle {
        root * ${WEB_ROOT}

        # Prevent browser stale caching of service worker and web app manifest
        @pwa_control_files {
            path /sw.js /registerSW.js /manifest.webmanifest /index.html
        }
        header @pwa_control_files Cache-Control "no-cache, no-store, must-revalidate"

        # Immutable long-term caching for Vite fingerprinted assets
        @hashed_assets {
            path /assets/*
        }
        header @hashed_assets Cache-Control "public, max-age=31536000, immutable"

        # Client-side Vue Router SPA fallback
        try_files {path} /index.html
        file_server
    }

    # 5. Access logging
    log {
        output file /var/log/caddy/chronomed-access.log {
            roll_size 50MiB
            roll_keep 5
        }
    }
}
EOF

# Validate syntax and reload Caddy
caddy validate --config /etc/caddy/Caddyfile
systemctl enable caddy
systemctl restart caddy
success "Caddy reloaded with automatic Let's Encrypt TLS."

# Make sure update script is executable
chmod +x "${APP_DIR}/deploy/deploy.sh" || true

# ==============================================================================
# COMPLETION SUMMARY
# ==============================================================================
echo -e "\n${GREEN}${BOLD}====================================================================${NC}"
echo -e "${GREEN}${BOLD}           INSTALLATION COMPLETED SUCCESSFULLY!                     ${NC}"
echo -e "${GREEN}${BOLD}====================================================================${NC}\n"

echo -e "Your ChronoMed instance is now live at:"
echo -e "  🌐 Public App URL:     ${CYAN}${BOLD}https://${DOMAIN}${NC}"
echo -e "  📄 Interactive API:     ${CYAN}https://${DOMAIN}/docs${NC}"
echo -e "  💓 Health Endpoint:     ${CYAN}https://${DOMAIN}/api/v1/health${NC}\n"

echo -e "${BOLD}Important System Locations:${NC}"
echo -e "  • Project Directory:    ${APP_DIR}"
echo -e "  • SQLite WAL Storage:   ${SQLITE_DATA_DIR}"
echo -e "  • Frontend Web Root:    ${WEB_ROOT}"
echo -e "  • Caddy Configuration:  /etc/caddy/Caddyfile"
echo -e "  • Caddy Access Log:     /var/log/caddy/chronomed-access.log\n"

echo -e "${BOLD}Useful Commands:${NC}"
echo -e "  • View backend logs:    cd ${APP_DIR} && docker compose logs -f backend"
echo -e "  • Restart backend:      cd ${APP_DIR} && docker compose restart"
echo -e "  • View Caddy logs:      journalctl -u caddy -f"
echo -e "  • Future 1-Click Update: ${BOLD}${APP_DIR}/deploy/deploy.sh${NC}\n"

if ! host "$DOMAIN" &>/dev/null; then
  warn "Reminder: Ensure DNS A/AAAA record for '${DOMAIN}' points to this server's public IP"
  warn "so Caddy can automatically obtain your SSL certificate from Let's Encrypt."
fi

echo -e "${GREEN}Done! You can now access ChronoMed in your browser.${NC}\n"
