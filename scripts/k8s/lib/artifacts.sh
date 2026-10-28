#!/bin/bash
# artifacts.sh — verified k3s/Helm artifact acquisition for the Phase 254 k3s
# bootstrap (D-10, D-11, D-12, D-13; T-254-01, T-254-03, T-254-04). Sourceable;
# requires common.sh for step/preflight_error. Downloads only official pinned
# URLs into a private temporary directory, verifies the tagged k3s installer,
# the selected architecture-specific k3s server binary, and the selected Helm
# archive with `sha256sum -c` against the committed manifest BEFORE any
# execution or extraction, and exposes deterministic version-comparison
# decisions plus verified artifact paths to the k3s/Helm consumers. A digest
# mismatch, missing manifest entry, download failure, or unsupported
# architecture fails closed: non-zero, empty verified paths, nothing executed.
#
# Consumer contract (D-11/D-15): the later k3s module must install the
# verified staged binary and invoke the official installer with
# INSTALL_K3S_SKIP_DOWNLOAD=true so no unchecked upstream binary download can
# bypass this gate. The k3s module must detect the installed version from
# parseable `k3s --version` output and fail closed before k3s_install_decision
# when the binary exists but its version cannot be parsed.
set -euo pipefail

ARTIFACTS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
K8S_DIR="$(dirname "$ARTIFACTS_LIB_DIR")"

# Staged filenames MUST match the checksums.sha256 entries exactly so
# `sha256sum -c` resolves them from the staging directory (D-12, T-254-01).
K3S_INSTALLER_FILE="k3s-installer.sh"
K3S_BINARY_FILE_PREFIX="k3s-server"
HELM_ARCHIVE_PREFIX="helm"

# Verified artifact paths — empty until acquire_artifacts verifies everything.
VERIFIED_INSTALLER=""
VERIFIED_K3S_BINARY=""
VERIFIED_HELM_ARCHIVE=""

K3S_VERSION_REGEX='^v?[0-9]+\.[0-9]+\.[0-9]+'

# ---------------------------------------------------------------------------
# Pinned version loading (D-10)
# ---------------------------------------------------------------------------

load_versions() {  # load_versions [file] — read literal KEY=value pins only
  local file="${1:-$K8S_DIR/versions.env}"
  local key value
  K3S_VERSION=""
  HELM_VERSION=""
  HELM_CHART_PATH=""
  HELM_RELEASE_NAME=""
  CERT_MANAGER_VERSION=""
  CERT_MANAGER_KEYRING_URL=""
  CERT_MANAGER_KEYRING_FILE=""
  KUBE_PROMETHEUS_STACK_VERSION=""
  KUBE_PROMETHEUS_STACK_CHART=""
  PROMETHEUS_COMMUNITY_KEYRING_URL=""
  PROMETHEUS_COMMUNITY_KEYRING_FILE=""
  HEADLAMP_VERSION=""
  HEADLAMP_IMAGE=""
  LOKI_CHART_VERSION=""
  LOKI_CHART=""
  GRAFANA_COMMUNITY_KEYRING_URL=""
  GRAFANA_COMMUNITY_KEYRING_FILE=""
  ALLOY_CHART_VERSION=""
  ALLOY_CHART_URL=""
  ALLOY_CHART_FILE=""
  [ -f "$file" ] || return 1
  while IFS='=' read -r key value; do
    case "$key" in
      K3S_VERSION) K3S_VERSION="$value" ;;
      HELM_VERSION) HELM_VERSION="$value" ;;
      HELM_CHART_PATH) HELM_CHART_PATH="$value" ;;
      HELM_RELEASE_NAME) HELM_RELEASE_NAME="$value" ;;
      CERT_MANAGER_VERSION) CERT_MANAGER_VERSION="$value" ;;
      CERT_MANAGER_KEYRING_URL) CERT_MANAGER_KEYRING_URL="$value" ;;
      CERT_MANAGER_KEYRING_FILE) CERT_MANAGER_KEYRING_FILE="$value" ;;
      KUBE_PROMETHEUS_STACK_VERSION) KUBE_PROMETHEUS_STACK_VERSION="$value" ;;
      KUBE_PROMETHEUS_STACK_CHART) KUBE_PROMETHEUS_STACK_CHART="$value" ;;
      PROMETHEUS_COMMUNITY_KEYRING_URL) PROMETHEUS_COMMUNITY_KEYRING_URL="$value" ;;
      PROMETHEUS_COMMUNITY_KEYRING_FILE) PROMETHEUS_COMMUNITY_KEYRING_FILE="$value" ;;
      HEADLAMP_VERSION) HEADLAMP_VERSION="$value" ;;
      HEADLAMP_IMAGE) HEADLAMP_IMAGE="$value" ;;
      LOKI_CHART_VERSION) LOKI_CHART_VERSION="$value" ;;
      LOKI_CHART) LOKI_CHART="$value" ;;
      GRAFANA_COMMUNITY_KEYRING_URL) GRAFANA_COMMUNITY_KEYRING_URL="$value" ;;
      GRAFANA_COMMUNITY_KEYRING_FILE) GRAFANA_COMMUNITY_KEYRING_FILE="$value" ;;
      ALLOY_CHART_VERSION) ALLOY_CHART_VERSION="$value" ;;
      ALLOY_CHART_URL) ALLOY_CHART_URL="$value" ;;
      ALLOY_CHART_FILE) ALLOY_CHART_FILE="$value" ;;
    esac
  done < <(grep -E '^[A-Z_][A-Z0-9_]*=' "$file" | sed 's/\r$//')
  [ -n "$K3S_VERSION" ] && [ -n "$HELM_VERSION" ] \
    && [ -n "$HELM_CHART_PATH" ] && [ -n "$HELM_RELEASE_NAME" ] \
    && [ -n "$CERT_MANAGER_VERSION" ] && [ -n "$CERT_MANAGER_KEYRING_URL" ] \
    && [ -n "$CERT_MANAGER_KEYRING_FILE" ] \
    && [ -n "$KUBE_PROMETHEUS_STACK_VERSION" ] && [ -n "$KUBE_PROMETHEUS_STACK_CHART" ] \
    && [ -n "$PROMETHEUS_COMMUNITY_KEYRING_URL" ] && [ -n "$PROMETHEUS_COMMUNITY_KEYRING_FILE" ] \
    && [ -n "$HEADLAMP_VERSION" ] && [ -n "$HEADLAMP_IMAGE" ]
}

# ---------------------------------------------------------------------------
# Deterministic version comparison (D-11, T-254-04)
# ---------------------------------------------------------------------------

version_cmp() {  # version_cmp <a> <b> — echo -1 (a<b), 0, or 1 (a>b) via sort -V
  local a="$1" b="$2"
  if [ "$a" = "$b" ]; then
    echo 0
    return 0
  fi
  if [ "$(printf '%s\n%s\n' "$a" "$b" | sort -V | head -n1)" = "$a" ]; then
    echo -1
  else
    echo 1
  fi
}

k3s_version_parseable() {  # k3s_version_parseable <v> — 0 when it looks like a k3s version
  [[ "${1:-}" =~ $K3S_VERSION_REGEX ]]
}

k3s_install_decision() {  # k3s_install_decision <installed_version>
  # echo install|upgrade|keep|preserve (D-11, T-254-04). Empty or unparseable
  # installed versions install the pin; the consumer must fail closed first
  # when an existing binary's version cannot be parsed (see header contract).
  local installed="${1:-}"
  local cmp
  load_versions
  if [ -z "$installed" ] || ! k3s_version_parseable "$installed"; then
    echo "install"
    return 0
  fi
  cmp="$(version_cmp "$installed" "$K3S_VERSION")"
  case "$cmp" in
    -1) echo "upgrade" ;;
    0) echo "keep" ;;
    1) echo "preserve" ;;
  esac
}

helm_install_decision() {  # helm_install_decision <installed_version>
  # echo keep|replace (D-11). Helm is compatible while its major equals the
  # pinned major (v4); missing, unparseable, or other-major Helm is replaced
  # only after archive verification. The chart's declared minimum (research
  # A3) does not exist yet — the v4-major rule is the Phase 254 default and
  # must be revisited when deploy/helm/indramesh chart metadata lands.
  local installed="${1:-}"
  local installed_major pinned_major
  load_versions
  [ -n "$installed" ] || { echo "replace"; return 0; }
  installed_major="$(printf '%s\n' "$installed" | sed -E 's/^v?([0-9]+).*/\1/')"
  pinned_major="$(printf '%s\n' "$HELM_VERSION" | sed -E 's/^v?([0-9]+).*/\1/')"
  if [ -n "$installed_major" ] && [ "$installed_major" = "$pinned_major" ]; then
    echo "keep"
  else
    echo "replace"
  fi
}

# ---------------------------------------------------------------------------
# Verified acquisition (D-12, T-254-01, T-254-03)
# ---------------------------------------------------------------------------

_verify_one() {  # _verify_one <tmpdir> <manifest> <name> — sha256sum -c one manifest entry
  local tmpdir="$1" manifest="$2" name="$3"
  local entry
  entry="$(awk -v n="$name" '$2 == n { print }' "$manifest")"
  if [ -z "$entry" ]; then
    preflight_error "checksum manifest is missing the '$name' entry (T-254-01)"
    return 1
  fi
  printf '%s\n' "$entry" > "$tmpdir/.verify-one.sha256"
  if ! (cd "$tmpdir" && sha256sum -c .verify-one.sha256) >/dev/null 2>&1; then
    preflight_error "digest verification failed for '$name' (T-254-01) — artifact is not executable"
    return 1
  fi
  return 0
}

acquire_artifacts() {  # acquire_artifacts <arch> <tmpdir> [manifest_file] [versions_file]
  # Download and verify the tagged k3s installer, the architecture-specific
  # k3s server binary, and the Helm archive for <arch> into <tmpdir> using
  # only pinned official URLs (T-254-03). Sets VERIFIED_INSTALLER,
  # VERIFIED_K3S_BINARY, and VERIFIED_HELM_ARCHIVE only when every digest
  # verifies; otherwise returns non-zero with all verified paths empty.
  local arch="$1"
  local tmpdir="$2"
  local manifest_file="${3:-$K8S_DIR/checksums.sha256}"
  local versions_file="${4:-$K8S_DIR/versions.env}"
  local installer_url binary_url helm_url upstream_binary
  local staged_installer staged_binary staged_helm
  VERIFIED_INSTALLER=""
  VERIFIED_K3S_BINARY=""
  VERIFIED_HELM_ARCHIVE=""
  case "$arch" in
    amd64|arm64) ;;
    *)
      preflight_error "unsupported architecture '$arch' — only amd64/arm64 are supported (D-13)"
      return 1
      ;;
  esac
  load_versions "$versions_file" || {
    preflight_error "failed to load pinned versions from $(basename "$versions_file") (D-10)"
    return 1
  }
  [ -d "$tmpdir" ] || {
    preflight_error "artifact staging directory does not exist: $tmpdir"
    return 1
  }
  [ -f "$manifest_file" ] || {
    preflight_error "checksum manifest missing: $manifest_file (D-12)"
    return 1
  }
  case "$arch" in
    amd64) upstream_binary="k3s" ;;
    arm64) upstream_binary="k3s-arm64" ;;
  esac
  installer_url="https://raw.githubusercontent.com/k3s-io/k3s/${K3S_VERSION}/install.sh"
  binary_url="https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION}/${upstream_binary}"
  helm_url="https://get.helm.sh/${HELM_ARCHIVE_PREFIX}-${HELM_VERSION}-linux-${arch}.tar.gz"

  staged_installer="$tmpdir/${K3S_INSTALLER_FILE}"
  staged_binary="$tmpdir/${K3S_BINARY_FILE_PREFIX}-${arch}"
  staged_helm="$tmpdir/${HELM_ARCHIVE_PREFIX}-${HELM_VERSION}-linux-${arch}.tar.gz"

  # 1. tagged official k3s installer — verify before it can be executed
  curl -fsSL -o "$staged_installer" "$installer_url" || {
    preflight_error "failed to download the pinned k3s installer (D-12)"
    return 1
  }
  _verify_one "$tmpdir" "$manifest_file" "$K3S_INSTALLER_FILE" || return 1

  # 2. architecture-specific k3s server binary — verify before installation
  curl -fsSL -o "$staged_binary" "$binary_url" || {
    preflight_error "failed to download the pinned k3s server binary for ${arch} (D-12)"
    return 1
  }
  _verify_one "$tmpdir" "$manifest_file" "${K3S_BINARY_FILE_PREFIX}-${arch}" || return 1

  # 3. Helm release archive — verify before any extraction
  curl -fsSL -o "$staged_helm" "$helm_url" || {
    preflight_error "failed to download the pinned Helm archive for ${arch} (D-12)"
    return 1
  }
  _verify_one "$tmpdir" "$manifest_file" "$(basename "$staged_helm")" || return 1

  VERIFIED_INSTALLER="$staged_installer"
  VERIFIED_K3S_BINARY="$staged_binary"
  VERIFIED_HELM_ARCHIVE="$staged_helm"
  step "artifacts: all pinned artifacts verified for ${arch} (T-254-01)"
  return 0
}

verified_artifact_paths() {  # verified_artifact_paths — echo installer, binary, Helm archive
  printf '%s\n%s\n%s\n' "$VERIFIED_INSTALLER" "$VERIFIED_K3S_BINARY" "$VERIFIED_HELM_ARCHIVE"
}