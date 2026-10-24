#!/usr/bin/env bash
# One-shot deploy of adsops-mcp on an Ubuntu/Debian VPS behind Caddy (automatic HTTPS).
#   sudo DOMAIN=adspire.ostap49.marketing bash install.sh
# Re-run any time to update (git pull + restart). Idempotent.
set -euo pipefail

DOMAIN="${DOMAIN:-adspire.ostap49.marketing}"
REPO="${REPO:-https://github.com/ostap49/Google-Ads-Optimization.git}"
BRANCH="${BRANCH:-claude/inspiring-ritchie-a0s99k}"
APP_USER="${APP_USER:-adsops}"
BASE="/opt/adsops"
APP="$BASE/Google-Ads-Optimization/adsops-mcp"
PORT="${PORT:-8765}"

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip curl debian-keyring debian-archive-keyring apt-transport-https >/dev/null
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi

id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$BASE" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$BASE" && chown "$APP_USER:$APP_USER" "$BASE"

if [ -d "$BASE/Google-Ads-Optimization/.git" ]; then
  sudo -u "$APP_USER" git -C "$BASE/Google-Ads-Optimization" fetch -q origin "$BRANCH"
  sudo -u "$APP_USER" git -C "$BASE/Google-Ads-Optimization" checkout -q "$BRANCH"
  sudo -u "$APP_USER" git -C "$BASE/Google-Ads-Optimization" reset -q --hard "origin/$BRANCH"
else
  sudo -u "$APP_USER" git clone -q -b "$BRANCH" "$REPO" "$BASE/Google-Ads-Optimization"
fi

sudo -u "$APP_USER" python3 -m venv "$APP/.venv"
sudo -u "$APP_USER" "$APP/.venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

# .env / config.yaml: create once, never overwrite
if [ ! -f "$APP/.env" ]; then
  sudo -u "$APP_USER" cp "$APP/.env.example" "$APP/.env"
  TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
  sed -i "s|^ADSOPS_HTTP_TOKEN=.*|ADSOPS_HTTP_TOKEN=$TOKEN|; s|^ADSOPS_HTTP_PORT=.*|ADSOPS_HTTP_PORT=$PORT|" "$APP/.env"
  echo "Generated ADSOPS_HTTP_TOKEN (see $APP/.env)"
fi
[ -f "$APP/config.yaml" ] || sudo -u "$APP_USER" cp "$APP/config.yaml.example" "$APP/config.yaml"
chmod 600 "$APP/.env"

cat > /etc/systemd/system/adsops-mcp.service <<UNIT
[Unit]
Description=adsops-mcp (Google Ads MCP server, HTTP)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python server.py --transport http --host 127.0.0.1 --port $PORT --allowed-host $DOMAIN
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/adsops-monitor.service <<UNIT
[Unit]
Description=adsops read-only monitor (Telegram alerts)

[Service]
Type=oneshot
User=$APP_USER
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python monitor.py
UNIT

cat > /etc/systemd/system/adsops-monitor.timer <<UNIT
[Unit]
Description=Run adsops monitor daily at 09:15 Kyiv time

[Timer]
OnCalendar=*-*-* 09:15:00 Europe/Kyiv
Persistent=true

[Install]
WantedBy=timers.target
UNIT

# Caddy site (keeps other sites in /etc/caddy/Caddyfile untouched)
mkdir -p /etc/caddy/sites
grep -q 'import sites/\*' /etc/caddy/Caddyfile 2>/dev/null || echo 'import sites/*' >> /etc/caddy/Caddyfile
cat > /etc/caddy/sites/adsops.caddy <<SITE
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:$PORT {
        flush_interval -1
    }
}
SITE

systemctl daemon-reload
systemctl enable -q adsops-mcp.service caddy
caddy validate --config /etc/caddy/Caddyfile >/dev/null
systemctl reload caddy || systemctl restart caddy

if [ -f "$APP/google-ads.yaml" ]; then
  systemctl restart adsops-mcp.service
  sleep 2
  systemctl --no-pager --lines=5 status adsops-mcp.service || true
else
  echo
  echo "!! $APP/google-ads.yaml is missing — the server runs, but every tool returns a credentials error until you add it:"
  echo "   1) on your laptop: python auth.py   (creates google-ads.yaml)"
  echo "   2) scp google-ads.yaml root@<server>:$APP/ && sudo chown $APP_USER: $APP/google-ads.yaml && sudo chmod 600 $APP/google-ads.yaml"
  echo "   3) sudo systemctl restart adsops-mcp"
  systemctl restart adsops-mcp.service || true
fi
echo
echo "Telegram monitor: fill TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in $APP/.env, then: sudo systemctl enable --now adsops-monitor.timer"
echo "Endpoint: https://$DOMAIN/mcp   (header: Authorization: Bearer \$(grep ADSOPS_HTTP_TOKEN $APP/.env | cut -d= -f2))"
