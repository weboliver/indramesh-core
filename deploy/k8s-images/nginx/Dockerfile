# deploy/k8s-images/nginx/Dockerfile — config-agnostic nginx wrapper image
# for the k3s release (IMG-01, D-06). Mirrors the stateful-wrapper shape (see
# deploy/k8s-images/stateful/postgres/Dockerfile): pin the upstream base,
# prepare group-writable runtime paths for the cluster-assigned arbitrary UID
# (Phase 255 D-10 group-writable pattern), then step down to the image's own
# nginx user (101) — the cluster overrides it at runtime.
#
# CONFIG-AGNOSTIC by design: no nginx.conf COPY, no certs COPY. The config
# variant mounts from the indramesh-nginx-config ConfigMap (D-02) and the TLS
# material mounts from the installer-owned indramesh-nginx-tls Secret (D-01) —
# both at deploy time, never baked into this image. No capability-stripping
# step either: the shared restricted securityContext already drops ALL
# capabilities, and this image only ever binds the unprivileged ports 8080/
# 8443 (D-03), so there is nothing to strip (unlike the frontend Caddy image,
# which needs cap_net_bind_service removed — see deploy/k8s-images/frontend/
# Dockerfile).
FROM nginx:1.30.6-alpine

USER root
RUN apk upgrade --no-cache && chmod -R g=u /var/cache/nginx
USER 101
