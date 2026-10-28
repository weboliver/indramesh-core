{{- /*
_helpers.tpl — reusable Restricted-PSA/SCC security and writable-path contract
for every later IndraMesh Core workload (D-09..D-12, T-255-04). Consumed by all app,
stateful, init, hook, and test containers created by later v16.0 phases.

This file carries NO generated Secret data and NO workload manifests — workload
templates remain owned by later phases (255-03+). Later templates consume these
named helpers so one authoritative contract stays in force for the whole chart.
*/ -}}

{{- /*
indramesh.restrictedSecurityContext — the shared container securityContext for
Restricted Pod Security Admission / OpenShift SCC (T-255-04).

Deliberately omits any fixed UID/GID or group-assignment field (D-09): the
cluster assigns an arbitrary non-root UID; group 0 supplies write access to the
declared paths prepared at image build time (D-10).
*/ -}}
{{- define "indramesh.restrictedSecurityContext" -}}
runAsNonRoot: true
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
seccompProfile:
  type: RuntimeDefault
capabilities:
  drop:
    - ALL
{{- end -}}

{{- /*
indramesh.writableMountPaths — the explicit writable-volume targets the
backend/worker image prepares with group-0 access (D-10/D-11). Under
readOnlyRootFilesystem every runtime write location MUST be an explicit mounted
volume; a missing mount is a deployment error, never an implicit writable root.

The Python temporary directory defaults to /tmp (world-writable sticky 1777) and
must be mounted explicitly under a read-only root.
*/ -}}
{{- define "indramesh.writableMountPaths" -}}
# backend/worker write inventory (D-10): export output, folder import, upload
# import data, and the Python temporary directory.
- /tmp/exports
- /tmp/imports
- /data/imports
- /tmp
{{- end -}}

{{- /*
indramesh.kubernetesEnv — the explicit non-secret runtime signal for later
Kubernetes backend/worker workloads. INDRAMESH_RUNTIME_MODE is the single strict
resolver input shared by startup and the System Logs provider selection;
`kubernetes` selects the in-cluster path and never writes the developer
initial-password artifact (D-12). Docker Compose sets the same variable to
`compose` to keep its own path separate.
*/ -}}
{{- define "indramesh.kubernetesEnv" -}}
- name: INDRAMESH_RUNTIME_MODE
  value: kubernetes
# Phase 272/K8SFIX-04: the single ENVIRONMENT signal decides GraphQL
# visibility on the k8s stack exactly as it does on Compose — rebuild.sh/
# install.sh set apps.environment from the same root .env source (default
# production keeps the current k8s deployment unchanged).
- name: ENVIRONMENT
  value: {{ .Values.apps.environment | default "production" | quote }}
# Phase 305 (LOGX-01): the k8s namespace the log-explorer plugin uses as its
# fixed query scope (`namespace="..."`). Downward API so the value is always
# the pod's actual namespace — never a hardcode — and equals the Alloy-emitted
# `namespace` label.
- name: POD_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
{{- end -}}

{{- /*
indramesh.loggingEnv — the explicit LOG_FORMAT signal for every container that
runs IndraMesh code (Phase 298, D-15). Backend/worker also carry the full
kubernetesEnv; the migration and maintenance Jobs, which set no ENVIRONMENT,
would otherwise render the development console default — so they include this
helper too. The default `json` matches the production/Loki contract; operators
may override apps.logFormat with logfmt or console.
*/ -}}
{{- define "indramesh.loggingEnv" -}}
- name: LOG_FORMAT
  value: {{ .Values.apps.logFormat | default "json" | quote }}
{{- /*
Phase 299 (D-17/LVL-01): the env level basis for every container that calls
configure_logging. Both default to an empty string — never an INFO default —
so the Foundation noisy defaults survive an unconfigured install. Runtime
overrides go through the logging-level API, not these values.
*/}}
- name: LOG_LEVEL
  value: {{ .Values.apps.logLevel | default "" | quote }}
- name: LOG_LEVELS
  value: {{ .Values.apps.logLevels | default "" | quote }}
{{- end -}}

{{- /*
Phase 300 (D-12/TLOG-05): task-log cap per run and retention window in days for
the backend and worker deployments. The values mirror the code defaults in
TaskLogConfig; a private values file may override apps.taskLog.{maxLines,retentionDays}.
*/ -}}
{{- define "indramesh.taskLogEnv" -}}
- name: TASK_LOG_MAX_LINES
  value: {{ .Values.apps.taskLog.maxLines | default 10000 | quote }}
- name: TASK_LOG_RETENTION_DAYS
  value: {{ .Values.apps.taskLog.retentionDays | default 30 | quote }}
{{- end -}}
