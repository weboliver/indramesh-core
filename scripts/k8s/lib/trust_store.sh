#!/bin/bash
# trust_store.sh — installer-owned consumer trust-store lifecycle for the
# Phase 259 TLS flow (BOOT-03; D-08; T-259-16..18). Sourceable; requires
# common.sh (step/preflight_error/require_sudo/K3S_KUBECONFIG) and secrets.sh
# (_kubectl_indramesh). Never self-sources — k3s.sh:1-18 convention.
#
# export_ca_root extracts ONLY the public root certificate (tls.crt) from the
# retained cert-manager Secret indramesh-ca-root into a host file (D-08): the
# private key never leaves the cluster, is never printed, and is never copied
# to the host (T-259-17, research anti-pattern 244). install_ca_trust imports
# that public root into the REAL consumer trust store — native Ubuntu via
# install + update-ca-certificates, Windows via a fingerprint-idempotent
# Import-Certificate into LocalMachine\Root, falling back to
# `certutil -addstore -user Root` for the per-user store (D-08, T-259-18). The
# library is additive only: unrelated trust entries are never removed, so the
# entry stays removable (D-08 reversibility).
#
# CR-01 gap-closure fix (259-REVIEW.md): the Windows thumbprint-check
# certificate is now built from DER bytes decoded out of the PEM body
# (Get-Content -Raw + a header/whitespace strip + FromBase64String), never
# from the raw PEM file path passed directly to X509Certificate2(string) —
# that constructor only accepts DER/PKCS#12 on Windows PowerShell 5.1/.NET
# Framework and threw CryptographicException on every real WSL2/Windows
# install. Import-Certificate and the fingerprint-idempotency check are
# unchanged.
set -euo pipefail

CA_ROOT_SECRET="indramesh-ca-root"
CA_ROOT_SECRET_NAMESPACE="cert-manager"
CA_TRUST_FILE_NAME="indramesh-local-ca.crt"
CA_TRUST_DIR="/usr/local/share/ca-certificates"

export_ca_root() {  # export_ca_root <out_file> — write the public root tls.crt
  # to <out_file> from the retained cert-manager Secret (D-08). Fail-closed
  # when the Secret is missing or carries no tls.crt data — the diagnostic
  # names the Secret only, never any key material (T-259-17).
  local out_file="${1:?output file required}"
  local out=""
  out="$(_kubectl_indramesh get secret "$CA_ROOT_SECRET" -n "$CA_ROOT_SECRET_NAMESPACE" \
    -o jsonpath='{.data.tls\.crt}' 2>/dev/null)" || {
    preflight_error "trust-store: retained CA root Secret '$CA_ROOT_SECRET' could not be read — no key material is exported (D-08)"
    return 1
  }
  if [ -z "$out" ]; then
    preflight_error "trust-store: retained CA root Secret '$CA_ROOT_SECRET' carries no tls.crt data — refusing to export (D-08)"
    return 1
  fi
  printf '%s' "$out" | base64 -d > "$out_file" || {
    preflight_error "trust-store: failed to decode the retained CA root tls.crt (D-08)"
    return 1
  }
  if [ ! -s "$out_file" ]; then
    preflight_error "trust-store: exported CA root file is empty — refusing to install (D-08)"
    return 1
  fi
  step "trust-store: exported the public CA root from '$CA_ROOT_SECRET' to $out_file (public tls.crt only, D-08)"
  return 0
}

install_ca_trust() {  # install_ca_trust <ca_crt_file> — platform dispatch on
  # the same PLATFORM_WSL seam reachability.sh uses (D-08): native Ubuntu
  # install + update-ca-certificates; Windows fingerprint-idempotent elevated
  # Import-Certificate. Additive only — never removes or alters unrelated
  # trust entries (T-259-18).
  local ca_crt_file="${1:?CA certificate file required}"
  [ -f "$ca_crt_file" ] || { preflight_error "trust-store: CA certificate file missing: $ca_crt_file (D-08)"; return 1; }
  if [ "${PLATFORM_WSL:-0}" -eq 1 ]; then
    _trust_import_windows "$ca_crt_file" || return 1
  else
    _trust_import_native "$ca_crt_file" || return 1
  fi
  return 0
}

_trust_import_native() {  # _trust_import_native <ca_crt_file> — require_sudo +
  # install one named file + update-ca-certificates (D-08). Idempotent:
  # re-importing the same root is a no-op for update-ca-certificates.
  local ca_crt_file="$1"
  require_sudo || return 1
  step "trust-store: installing the retained CA root into the native trust store (D-08)"
  sudo install -m 0644 "$ca_crt_file" "$CA_TRUST_DIR/$CA_TRUST_FILE_NAME" || {
    preflight_error "trust-store: failed to install $CA_TRUST_FILE_NAME into $CA_TRUST_DIR (D-08)"
    return 1
  }
  sudo update-ca-certificates || {
    preflight_error "trust-store: update-ca-certificates failed — the CA root was not registered (D-08)"
    return 1
  }
  step "trust-store: native CA root trust registered (D-08)"
  return 0
}

_trust_import_windows() {  # _trust_import_windows <ca_crt_file> — fingerprint-
  # idempotent import; the machine store when the session can write it,
  # otherwise the per-user store via certutil (no consent dialog); NEVER fatal
  # (Phase 311.1 clean-host
  # finding). The interop process inherits the Windows session's token, so a
  # normal (non-elevated) terminal cannot write LocalMachine\Root — measured
  # live: the import failed and aborted an otherwise healthy, fully deployed
  # release. The README documents accepting the self-signed warning and the
  # Docker quickstart ships exactly that UX, so the per-user store is the
  # fallback that makes the CA actually trusted for the operator without
  # elevation, and a failure here warns instead of stopping the install.
  # The thumbprint is computed INSIDE PowerShell from the exported root file
  # (X509Certificate2.Thumbprint) and the import runs only when that fingerprint
  # is not already present. The controlled file path is passed via the
  # INDRAMESH_CA_FILE environment variable — never interpolated into the
  # command string (T-259-19, research 531).
  local ca_crt_file="$1"
  local ps_cmd
  if ! command -v powershell.exe >/dev/null 2>&1; then
    step "trust-store: WARNING — Windows interop (powershell.exe) is unavailable, so the CA root cannot be registered in the Windows trust store; the install continues (D-08)"
    return 0
  fi
  ps_cmd="\$ErrorActionPreference='Stop'; "
  ps_cmd+="\$pem = Get-Content -Raw \$env:INDRAMESH_CA_FILE; "
  ps_cmd+="\$b64 = (\$pem -replace '-----(BEGIN|END) CERTIFICATE-----','') -replace '\s',''; "
  ps_cmd+='$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(,[Convert]::FromBase64String($b64)); '
  ps_cmd+='$thumb = $cert.Thumbprint; '
  ps_cmd+='if (Get-ChildItem Cert:\CurrentUser\Root | Where-Object { $_.Thumbprint -eq $thumb }) { exit 0 } ; '
  ps_cmd+="try { Import-Certificate -FilePath \$env:INDRAMESH_CA_FILE -CertStoreLocation 'Cert:\LocalMachine\Root' | Out-Null; exit 0 } catch { } ; "
  # Phase 318 (host-B clean-host finding): Import-Certificate into
  # Cert:\CurrentUser\Root raises the CA-installation consent dialog, and under
  # -NonInteractive Windows refuses it ("Die Benutzeroberfläche ist für diesen
  # Vorgang nicht zulässig") — so the documented no-elevation fallback could
  # never work. Measured on WSL2: the CA stayed unregistered, Schannel rejected
  # the chain (0x80096004, "Die Signatur des Zertifikats konnte nicht bestätigt
  # werden"), and the endpoint probe failed an otherwise healthy, fully deployed
  # install. certutil -addstore -user needs no dialog: verified live, exit 0 and
  # the Windows-side probe answers 200 afterwards.
  ps_cmd+='& certutil.exe -addstore -f -user Root $env:INDRAMESH_CA_FILE | Out-Null; '
  ps_cmd+='if ($LASTEXITCODE -ne 0) { exit 1 } ; '
  ps_cmd+='exit 0'
  # WSL does not forward shell environment variables to an interop-launched
  # Windows process unless the variable is registered in WSLENV — without
  # this, $env:INDRAMESH_CA_FILE arrives as $null in PowerShell regardless of
  # elevation, Get-Content throws, and the failure is indistinguishable from
  # a real elevation error. The /p flag also translates the Linux path to
  # its \\wsl.localhost\... form so PowerShell can actually open the file.
  local _wslenv="INDRAMESH_CA_FILE/p"
  [ -n "${WSLENV:-}" ] && _wslenv="${WSLENV}:${_wslenv}"
  if ! INDRAMESH_CA_FILE="$ca_crt_file" WSLENV="$_wslenv" powershell.exe -NoProfile -NonInteractive -Command "$ps_cmd" >/dev/null 2>&1; then
    step "trust-store: WARNING — could not register the CA root in the Windows trust store (neither the machine nor the per-user store accepted it). The release is deployed and reachable; the browser will show the self-signed certificate warning (README 'Kubernetes Installation', D-08)"
    return 0
  fi
  step "trust-store: Windows CA root trust registered (machine store when the session can write it, else the per-user store; fingerprint-idempotent, D-08)"
  return 0
}