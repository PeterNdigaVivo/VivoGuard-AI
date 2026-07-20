#!/bin/sh
# =====================================================================
# VivoGuard AI — TLS cert-presence guard.
#
# The stock nginx image runs every executable /docker-entrypoint.d/*.sh
# before starting nginx. This script picks the active server config based
# on whether the TLS certs actually exist on disk:
#
#   certs present  → install the HTTPS profile  (443 + 80→443 redirect)
#   certs MISSING  → install the HTTP-only fallback (serve app on :80)
#
# Without this, a missing cert makes nginx crash-loop with
#   "cannot load certificate .../fullchain.pem: No such file or directory"
# and the whole stack is unreachable. Serving HTTP is strictly better than
# serving nothing, and Let's Encrypt HTTP-01 issuance still works so the
# first cert can be obtained; restart nginx afterwards to go HTTPS.
#
# Override the cert paths with VG_TLS_CERT / VG_TLS_KEY if needed.
# =====================================================================
set -eu

CERT="${VG_TLS_CERT:-/etc/letsencrypt/live/vivoops.vivofashiongroup.net/fullchain.pem}"
KEY="${VG_TLS_KEY:-/etc/letsencrypt/live/vivoops.vivofashiongroup.net/privkey.pem}"
SRC=/etc/nginx/vg-templates
ACTIVE=/etc/nginx/conf.d/default.conf

if [ -s "$CERT" ] && [ -s "$KEY" ]; then
    echo "[tls-guard] TLS certs present → serving HTTPS profile"
    cp "$SRC/https.conf" "$ACTIVE"
else
    echo "[tls-guard] WARNING: TLS certs not found at $CERT / $KEY"
    echo "[tls-guard] falling back to HTTP-only on :80 (nginx will still start)"
    cp "$SRC/http-only.conf" "$ACTIVE"
fi
