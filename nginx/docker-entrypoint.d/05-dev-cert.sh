#!/bin/sh
# nginx/certs/ is gitignored (1b172604), so a fresh checkout or a deleted dir would
# leave nginx crash-looping on a missing server.crt. Generates a self-signed dev pair
# only when either file is missing; existing certs are kept. Runs inside the nginx
# container's own entrypoint so no separate init container is needed.
set -e

CERT_DIR=/etc/nginx/certs

if [ -s "$CERT_DIR/server.crt" ] && [ -s "$CERT_DIR/server.key" ]; then
    echo "$0: existing certificate kept"
    exit 0
fi

command -v openssl >/dev/null 2>&1 || apk add -q --no-cache openssl
openssl req -x509 -quiet -newkey rsa:2048 -nodes -sha256 -days 825 \
    -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.crt" \
    -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,DNS:nginx,IP:127.0.0.1"
chmod 644 "$CERT_DIR/server.crt"
chmod 640 "$CERT_DIR/server.key"
chown "${CERT_UID:-1000}:${CERT_GID:-1000}" "$CERT_DIR" "$CERT_DIR/server.crt" "$CERT_DIR/server.key"
echo "$0: generated self-signed certificate"
