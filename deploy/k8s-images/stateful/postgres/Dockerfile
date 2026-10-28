# deploy/k8s-images/stateful/postgres/Dockerfile — the own Postgres image of
# Phase 312.1 (D-10/D-11/D-12, PGAGE-01/PGAGE-02): Apache AGE and pgvector
# compiled from pinned upstream sources onto the UNCHANGED base line
# (postgres:18.4-alpine — musl + major 18, so the existing postgres18_data
# directory is reused exactly as it is: no dump, no restore, no collation
# change), plus the two restart-time server settings baked in:
#   shared_preload_libraries = 'age'    (merged into any operator-configured list)
#   wal_level = logical                 (enforced)
#
# The same image serves both install paths: Compose builds it from this file,
# and scripts/k8s/install.sh builds and imports it from the chart's image refs.
# The settings reach an initialized data directory through the image's own
# wrapper entrypoint and a fresh initdb through the initdb script directory —
# there is NO `command:`/`args:` for them anywhere (D-12).
#
# Extension DDL is deliberately NOT part of this image: creating the age and
# vector extensions in the application database belongs to the Core migration
# (D-13).

# --- build stage: compile both extensions from pinned upstream sources --------
FROM postgres:18.4-alpine AS age-build

# Pinned sources and why these lines:
#   * Apache AGE 1.8.0 for PostgreSQL 18 — the official Apache source release
#     published at https://downloads.apache.org/age/PG18/1.8.0/ (voted as the
#     upstream git tag PG18/v1.8.0-rc0; Apache publishes releases from its -rc
#     tags, so the suffix is upstream's release convention, not a pre-release).
#     1.8.0 is the FIRST PG18 line whose hooks are guarded for preload safety,
#     which is what makes `shared_preload_libraries = 'age'` safe in every
#     database of the server — including databases without the extension
#     catalog such as the Jira database on the same instance.
#   * pgvector v0.8.7 — the tag tarball from
#     https://github.com/pgvector/pgvector (the vector extension needs no
#     preload).
# A tag alone is mutable upstream; the checksums below are what pins the sources.
ARG AGE_SHA512=8103dc6a4daec0441c2bdfe4e456be619d04526efa5c4e1a5148a29d7e6359e5df7d9a1fabbc6a6a08f6229c4d6f8634b7bf3554eca8ffb58550382542279866
ARG PGVECTOR_SHA256=cac0b10c360f05b2d521200105ba3697e773d4cd3731f5a915a7e37ebe0bea85

WORKDIR /src

# Build toolchain: build-base (C compiler and make), bison 3.8.x (AGE's grammar
# conflict counts are pinned via %expect, so the 3.8 line Alpine 3.24 ships is
# exactly right), flex, perl (AGE's keyword-list generator), curl and tar.
RUN apk add --no-cache --virtual .build-deps build-base bison flex perl curl tar

# Apache AGE: checksum-verified (SHA-512 from the release directory), then
# built serially — the AGE Makefile generates grammar and keyword-list sources,
# which is safest unparallelized — and with JIT bitcode disabled: the base
# image's PGXS expects clang-21 for LLVM bitcode, which this image does not
# ship, so `with_llvm=no` skips bitcode emission (these two extensions need no
# JIT inlining).
RUN set -eux; \
    curl -fsSL -o apache-age-1.8.0-src.tar.gz https://downloads.apache.org/age/PG18/1.8.0/apache-age-1.8.0-src.tar.gz; \
    printf '%s  %s\n' "$AGE_SHA512" apache-age-1.8.0-src.tar.gz > age.sha512; \
    sha512sum -c age.sha512; \
    tar -xzf apache-age-1.8.0-src.tar.gz; \
    cd apache-age-1.8.0; \
    make with_llvm=no; \
    make with_llvm=no install

# pgvector: same rules — SHA-256-verified tag tarball, serial make,
# with_llvm=no.
RUN set -eux; \
    curl -fsSL -o pgvector-v0.8.7.tar.gz https://github.com/pgvector/pgvector/archive/refs/tags/v0.8.7.tar.gz; \
    printf '%s  %s\n' "$PGVECTOR_SHA256" pgvector-v0.8.7.tar.gz > pgvector.sha256; \
    sha256sum -c pgvector.sha256; \
    tar -xzf pgvector-v0.8.7.tar.gz; \
    cd pgvector-0.8.7; \
    make with_llvm=no; \
    make with_llvm=no install

# --- runtime stage: the same base, only the extensions' artifacts -------------
FROM postgres:18.4-alpine

# Only the runtime pieces cross the stage boundary: the two shared libraries
# and each extension's control and SQL files, in the image's own PostgreSQL
# paths. No compiler, no bison, no flex, no perl, no sources.
COPY --from=age-build /usr/local/lib/postgresql/age.so /usr/local/lib/postgresql/
COPY --from=age-build /usr/local/share/postgresql/extension/age* /usr/local/share/postgresql/extension/
COPY --from=age-build /usr/local/lib/postgresql/vector.so /usr/local/lib/postgresql/
COPY --from=age-build /usr/local/share/postgresql/extension/vector* /usr/local/share/postgresql/extension/

# The settings mechanism: one shared definition, sourced by the wrapper
# entrypoint (existing data directory) and by the initdb script (fresh init).
COPY indramesh-pg-settings.sh /usr/local/lib/indramesh-pg-settings.sh
COPY indramesh-postgres-entrypoint.sh /usr/local/bin/indramesh-postgres-entrypoint.sh
COPY 00-indramesh-server-settings.sh /docker-entrypoint-initdb.d/00-indramesh-server-settings.sh

# The Kubernetes wrapper contract this image already carried: the group
# permission change on the data and socket directories, then the image's own
# non-root user (arbitrary-UID pods keep working through fsGroup).
USER root
RUN apk upgrade --no-cache \
 && chmod 0755 /usr/local/bin/indramesh-postgres-entrypoint.sh \
        /docker-entrypoint-initdb.d/00-indramesh-server-settings.sh \
 && chmod -R g=u /var/lib/postgresql /var/run/postgresql
USER 70:0

# The wrapper runs before the official entrypoint so the settings reach an
# already-initialized data directory on every start; the official entrypoint
# remains the one that initializes a fresh directory and execs the server.
ENTRYPOINT ["/usr/local/bin/indramesh-postgres-entrypoint.sh"]
CMD ["postgres"]
