#!/usr/bin/env bash
# Sets up the Trading Agent on a fresh Ubuntu 22.04 / 24.04 server (written for Oracle Cloud's free Arm server).
#
#   cd ~/trading-agent && bash deploy/install.sh
#
# What it does (safe to run again; it never overwrites an existing .env, password or data):
#   1. India time zone; Python; automatic security updates, with any reboot they need at 02:30.
#   2. Copies the agent to /opt/trading-agent, owned by a service user "agent" (your data in runtime/ too,
#      the first time), and builds its Python environment.
#   3. Installs the service: starts at boot, restarts by itself.
#   4. Installs Tailscale and serves the dashboard on your private network over HTTPS:
#      https://<server>.<your-tailnet>.ts.net (nothing is opened to the internet).
#   5. Sets PUBLIC_URL and DASHBOARD_AUTH=required in .env; you then set the dashboard password.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/trading-agent
SVC=agent
[ "$(id -u)" -eq 0 ] || exec sudo -E bash "$0" "$@"
say() { printf '\n== %s\n' "$*"; }

say "Time zone: India"
timedatectl set-timezone Asia/Kolkata

say "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -q
apt-get install -y -q python3 python3-venv python3-pip sqlite3 rsync curl unattended-upgrades

say "Automatic security updates (any reboot they need happens at 02:30, outside market hours)"
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
cat >/etc/apt/apt.conf.d/52trading-agent <<'CONF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "02:30";
CONF

say "Service user and files in $DEST"
id -u "$SVC" >/dev/null 2>&1 || useradd --system --home-dir "$DEST" --shell /usr/sbin/nologin "$SVC"
mkdir -p "$DEST"
if [ "$SRC" != "$DEST" ]; then
  rsync -a --exclude '.venv' --exclude 'runtime' --exclude '.env' --exclude 'node_modules' --exclude '__pycache__' \
    "$SRC"/ "$DEST"/
  if [ -d "$SRC/runtime" ] && [ ! -e "$DEST/runtime/agent.db" ]; then
    echo "Copying your data (runtime/) the first time"
    rsync -a "$SRC/runtime/" "$DEST/runtime/"
  fi
  if [ -f "$SRC/.env" ] && [ ! -f "$DEST/.env" ]; then cp "$SRC/.env" "$DEST/.env"; fi
fi
[ -f "$DEST/.env" ] || cp "$DEST/.env.example" "$DEST/.env"
sed -i 's/\r$//' "$DEST/.env"                       # a .env copied from Windows has CRLF line ends
mkdir -p "$DEST/runtime"
chown -R "$SVC:$SVC" "$DEST"
chmod 600 "$DEST/.env"

say "Python environment"
sudo -u "$SVC" python3 -m venv "$DEST/.venv"
sudo -u "$SVC" "$DEST/.venv/bin/pip" install -q --upgrade pip
sudo -u "$SVC" "$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"
(cd "$DEST" && sudo -u "$SVC" "$DEST/.venv/bin/python" -m agent.cli init >/dev/null)

say "Service"
install -m 644 "$DEST/deploy/trading-agent.service" /etc/systemd/system/trading-agent.service
systemctl daemon-reload
systemctl enable trading-agent >/dev/null

say "Tailscale (your private network)"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
if ! tailscale status >/dev/null 2>&1; then
  echo "Open the link below on your phone or laptop and approve this server:"
  tailscale up
fi
tailscale serve --bg 8000
NAME="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
URL="https://$NAME"

setenv() {   # set KEY=VALUE in .env, replacing an existing line
  if grep -q "^$1=" "$DEST/.env"; then sed -i "s|^$1=.*|$1=$2|" "$DEST/.env"; else echo "$1=$2" >> "$DEST/.env"; fi
}
setenv PUBLIC_URL "$URL"
setenv DASHBOARD_AUTH required
setenv HOST 127.0.0.1

say "Done. Three things left:"
cat <<MSG
  1. Set the dashboard password:
       sudo -u $SVC $DEST/.venv/bin/python -m agent.cli set-password
     (run it from $DEST:  cd $DEST && sudo -u $SVC .venv/bin/python -m agent.cli set-password)
  2. In the Kite developer console, set your app's Redirect URL to:
       $URL/api/kite/callback
  3. Start it:   sudo systemctl start trading-agent
     Dashboard:  $URL   (from any device signed in to your Tailscale)
     Logs:       sudo journalctl -u trading-agent -f     and $DEST/runtime/logs/agent.log
MSG
