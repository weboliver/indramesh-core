FROM valkey/valkey:9.1.1-alpine

USER root
RUN apk upgrade --no-cache && chmod -R g=u /data
USER 999:0
