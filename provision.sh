#!/bin/sh
# Provision a Pixel 3a (PostmarketOS) as a 3D printer camera server.
# Must be run as root (or with sudo) directly on the phone.
#
# What this sets up:
#   - camera-web:     MJPEG HTTP stream on port 8080 (web browser UI)
#   - camera-stream:  VP8/WebM TCP stream on port 5000 (for mpv/ffplay)
#   - nginx:          Landing page on port 80
#   - nftables:       Firewall rules for all three ports on wlan0
#   - NetworkManager: Stops managing wlan0 (prevents wifi instability)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FILES="$SCRIPT_DIR/files"

# ---------------------------------------------------------------------------
# 1. Packages
# ---------------------------------------------------------------------------
echo "==> Installing packages..."
apk add --no-cache \
    nginx \
    python3 \
    gstreamer \
    gst-plugins-base \
    gst-plugins-good \
    gst-plugins-bad \
    gst-plugins-ugly \
    x264-libs \
    gstreamer-tools

# ---------------------------------------------------------------------------
# 2. Camera web server
# ---------------------------------------------------------------------------
echo "==> Deploying camera web server..."
cp "$FILES/camera-web.py" /usr/local/bin/camera-web.py
chmod 755 /usr/local/bin/camera-web.py

# ---------------------------------------------------------------------------
# 3. nginx landing page
# ---------------------------------------------------------------------------
echo "==> Deploying nginx landing page..."
mkdir -p /var/www/pxl-phone
cp "$FILES/index.html" /var/www/pxl-phone/index.html
rm -f /etc/nginx/http.d/default.conf
cp "$FILES/pxl-phone.conf" /etc/nginx/http.d/pxl-phone.conf

# ---------------------------------------------------------------------------
# 4. Firewall (nftables)
# ---------------------------------------------------------------------------
echo "==> Configuring firewall..."
mkdir -p /etc/nftables.d
cp "$FILES/52_stream.nft"     /etc/nftables.d/52_stream.nft
cp "$FILES/53_camera_web.nft" /etc/nftables.d/53_camera_web.nft
cp "$FILES/54_nginx.nft"      /etc/nftables.d/54_nginx.nft
for f in /etc/nftables.d/52_stream.nft \
         /etc/nftables.d/53_camera_web.nft \
         /etc/nftables.d/54_nginx.nft; do
    nft -f "$f"
done

# ---------------------------------------------------------------------------
# 5. NetworkManager: stop managing wlan0
#    (prevents wpa_supplicant conflict that causes wifi instability)
# ---------------------------------------------------------------------------
echo "==> Configuring NetworkManager..."
NM_CONF=/etc/NetworkManager/NetworkManager.conf
if ! grep -q 'unmanaged-devices=interface-name:wlan0' "$NM_CONF" 2>/dev/null; then
    printf '\n[keyfile]\nunmanaged-devices=interface-name:wlan0\n' >> "$NM_CONF"
    systemctl restart NetworkManager
    echo "    NetworkManager restarted — wlan0 is now unmanaged"
else
    echo "    Already configured, skipping"
fi

# ---------------------------------------------------------------------------
# 6. systemd units
# ---------------------------------------------------------------------------
echo "==> Installing systemd units..."
cp "$FILES/camera-stream.socket"   /etc/systemd/system/camera-stream.socket
cp "$FILES/camera-stream@.service" /etc/systemd/system/camera-stream@.service
cp "$FILES/camera-web.service"     /etc/systemd/system/camera-web.service
systemctl daemon-reload

echo "==> Enabling and starting services..."
systemctl enable --now camera-stream.socket
systemctl enable --now camera-web
systemctl reset-failed nginx 2>/dev/null || true
systemctl enable --now nginx

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "==> All done. Service status:"
for svc in camera-stream.socket camera-web nginx; do
    printf "    %-30s %s\n" "$svc" "$(systemctl is-active "$svc")"
done
echo ""
echo "    Web UI:    http://pxl-phone (port 80)"
echo "    Camera:    http://pxl-phone:8080"
echo "    TCP stream: mpv tcp://pxl-phone:5000"
