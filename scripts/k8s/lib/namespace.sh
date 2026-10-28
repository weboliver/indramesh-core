#!/bin/bash
# namespace.sh — installer-owned fixed-namespace Pod Security Admission gate
# for the v16.0 k3s bootstrap (D-01..D-04; SEC-01; T-255-01, T-255-02).
# Sourceable; requires common.sh (step/preflight_error/K3S_KUBECONFIG).
#
# The installer — never Helm — creates or reconciles the fixed `indramesh`
# namespace and sets pod-security.kubernetes.io/enforce=restricted BEFORE any
# Helm release can run (D-01). Reconciliation is idempotent and narrowly
# scoped (D-02): only the enforced label is created/repaired via targeted
# create-or-label commands; unrelated namespace labels are never touched, and
# a correctly labelled but already populated namespace proceeds without
# inspecting or deleting any foreign resource as an admission condition
# (D-04). Every path ends with a label read-back verification; a failed
# read-back returns non-zero so the installer stops before Helm (T-255-02).
# `indramesh` is the only target namespace (D-03) — there is deliberately no
# configurable-namespace option.
set -euo pipefail

NAMESPACE_NAME="indramesh"
PSA_ENFORCE_LABEL="pod-security.kubernetes.io/enforce"
PSA_ENFORCE_VALUE="restricted"

_kubectl_indramesh() {  # _kubectl_indramesh <args...> — targeted sudo + root-only kubeconfig (D-14)
  sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl "$@"
}

namespace_exists() {  # namespace_exists <name> — 0 when the namespace is present
  local name="$1"
  _kubectl_indramesh get namespace "$name" >/dev/null 2>&1
}

namespace_label_value() {  # namespace_label_value <name> <label> — echo the label value ("" when absent)
  local name="$1" label="$2" escaped
  escaped="$(_jsonpath_escape_key "$label")"
  _kubectl_indramesh get namespace "$name" -o "jsonpath={.metadata.labels['$escaped']}" 2>/dev/null || true
}

reconcile_indramesh_namespace() {  # reconcile_indramesh_namespace [namespace] — D-01/D-02/D-04 gate
  local ns="${1:-$NAMESPACE_NAME}"
  local current
  step "namespace: reconciling $PSA_ENFORCE_LABEL=$PSA_ENFORCE_VALUE on '$ns' (D-01)"
  if namespace_exists "$ns"; then
    current="$(namespace_label_value "$ns" "$PSA_ENFORCE_LABEL")"
    if [ "$current" = "$PSA_ENFORCE_VALUE" ]; then
      step "namespace: '$ns' already carries the restricted label (D-02)"
    else
      step "namespace: repairing $PSA_ENFORCE_LABEL on '$ns' — unrelated labels untouched (D-02)"
      _kubectl_indramesh label namespace "$ns" "$PSA_ENFORCE_LABEL=$PSA_ENFORCE_VALUE" --overwrite >/dev/null || return 1
    fi
  else
    step "namespace: creating '$ns' with $PSA_ENFORCE_LABEL=$PSA_ENFORCE_VALUE (D-01)"
    _kubectl_indramesh create namespace "$ns" >/dev/null || return 1
    _kubectl_indramesh label namespace "$ns" "$PSA_ENFORCE_LABEL=$PSA_ENFORCE_VALUE" >/dev/null || return 1
  fi
  current="$(namespace_label_value "$ns" "$PSA_ENFORCE_LABEL")"
  if [ "$current" != "$PSA_ENFORCE_VALUE" ]; then
    preflight_error "namespace '$ns' $PSA_ENFORCE_LABEL read-back failed (expected '$PSA_ENFORCE_VALUE', got '${current:-<missing>}') — refusing Helm (T-255-02)"
    return 1
  fi
  step "namespace: '$ns' verified $PSA_ENFORCE_LABEL=$PSA_ENFORCE_VALUE — Helm may proceed (D-01)"
  return 0
}