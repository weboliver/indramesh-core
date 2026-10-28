# Wrapper image for OpenSearch — one image for both install paths (Compose and Kubernetes).
#
# The upstream image ships the security plugin's tooling — including
# install_demo_configuration.sh, which the entrypoint runs on a fresh config directory —
# as mode 750 owned by the image's own user (uid 1000). A Kubernetes pod started with an
# arbitrary UID (the repo's SCC-conformant security context deliberately assigns none)
# cannot even READ that directory, so the demo security configuration would never install
# and no client could authenticate. `chmod -R g=u` makes the tree group-readable; on
# Kubernetes the process runs with group 0 as its primary group, which is what lets an
# arbitrary UID reach group-owned files.
#
# Compose does not need the permission change, but it builds this same wrapper so both
# paths run one image and the THIRD-PARTY-NOTICES table records one repository.
# Tag-pinned only — digest pinning stays FUTURE-02.
FROM opensearchproject/opensearch:3.9.0

USER root
RUN dnf upgrade -y --security && dnf clean all \
 && chmod -R g=u /usr/share/opensearch/plugins/opensearch-security/tools
# The image's own uid with group 0 as the primary group — the same shape as the postgres
# (70:0) and valkey (999:0) wrappers; scripts/k8s/verify_stateful_services.sh asserts
# exactly this value.
USER 1000:0
