import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from './use-api'

// --- Reference Types ---
export function useReferenceTypes(skip = 0, limit = 50, schemaId = null) {
  return useQuery({
    queryKey: ['reference-types', { skip, limit, schemaId }],
    queryFn: () => {
      const params = { skip, limit }
      if (schemaId) params.schema_id = schemaId
      return api.get('/reference-types/', { params }).then(r => r.data)
    },
  })
}
export function useCreateReferenceType() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data) => api.post('/reference-types/', data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['reference-types'] }),
  })
}
export function useUpdateReferenceType() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...data }) => api.patch(`/reference-types/${id}`, data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['reference-types'] }),
  })
}
export function useDeleteReferenceType() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id) => api.delete(`/reference-types/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['reference-types'] }),
  })
}

// --- Object Type Attributes ---
export function useObjectTypeAttributes(objectTypeId, skip = 0, limit = 50) {
  return useQuery({
    queryKey: ['object-type-attributes', { objectTypeId, skip, limit }],
    queryFn: () => api.get('/object-type-attributes/', { params: { object_type_id: objectTypeId, skip, limit } }).then(r => r.data),
    enabled: !!objectTypeId,
  })
}
export function useCreateObjectTypeAttribute() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data) => api.post('/object-type-attributes/', data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['object-type-attributes'] }),
  })
}
export function useUpdateObjectTypeAttribute() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...data }) => api.patch(`/object-type-attributes/${id}`, data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['object-type-attributes'] }),
  })
}
export function useDeleteObjectTypeAttribute() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id) => api.delete(`/object-type-attributes/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['object-type-attributes'] }),
  })
}

// --- Statuses ---
export function useStatuses(schemaId, skip = 0, limit = 50) {
  return useQuery({
    queryKey: ['statuses', { schemaId, skip, limit }],
    queryFn: () => api.get('/statuses/', { params: { schema_id: schemaId, skip, limit } }).then(r => r.data),
  })
}
export function useCreateStatus() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data) => api.post('/statuses/', data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['statuses'] }),
  })
}
export function useDeleteStatus() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id) => api.delete(`/statuses/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['statuses'] }),
  })
}

// --- Tasks ---
// Phase 247 (D-11/D-15, T-247-07/T-247-08): task_type/currentStatus are
// server-side filters (validated against the real enums by the API — see
// app/api/tasks.py) sent as request params and included in the query key, so
// a filter change is a real refetch rather than a client-side re-filter of
// an already-paginated page. Polling is exactly 2000ms while any visible row
// is PENDING/RUNNING and stops entirely (false) otherwise — no separate
// terminal-state polling interval.
// 261022-np1: `createdByMe` scopes the list to the caller's own tasks
// (server-authored Task.creator_id). The Create Schedule template picker uses
// it so a caller never sees another user's permanent template.
export function useTasks(skip = 0, limit = 50, taskType = null, scheduleIdNull = null, isPermanent = null, currentStatus = null, createdByMe = null) {
  return useQuery({
    queryKey: ['tasks', { skip, limit, taskType, scheduleIdNull, isPermanent, currentStatus, createdByMe }],
    queryFn: () => api.get('/tasks/', {
      params: {
        skip, limit,
        ...(taskType ? { task_type: taskType } : {}),
        ...(scheduleIdNull !== null ? { schedule_id_null: scheduleIdNull } : {}),
        ...(isPermanent !== null ? { is_permanent: isPermanent } : {}),
        ...(currentStatus ? { current_status: currentStatus } : {}),
        ...(createdByMe !== null ? { created_by_me: createdByMe } : {}),
      }
    }).then(r => ({
      items: r.data,
      total: parseInt(r.headers['x-total-count'] || '0', 10),
    })),
    refetchInterval: (query) => {
      const tasks = query.state.data?.items || []
      const hasRunning = tasks.some(t => t.current_status === 'RUNNING' || t.current_status === 'PENDING')
      return hasRunning ? 2000 : false
    },
  })
}
export function useTask(id) {
  return useQuery({
    queryKey: ['tasks', id],
    queryFn: () => api.get(`/tasks/${id}`).then(r => r.data),
    enabled: !!id,
    staleTime: 0,
    refetchInterval: (query) => {
      const data = query.state.data
      if (!data) return 2000
      const running = data.current_status === 'RUNNING' || data.current_status === 'PENDING'
      return running ? 2000 : false
    },
  })
}

// --- Task Actions ---
export function useDeleteTask() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (taskId) => api.delete(`/tasks/${taskId}`).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  })
}
export function useRetryTask() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (taskId) => api.post(`/tasks/${taskId}/retry`).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  })
}
export function useCancelTask() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (taskId) => api.post(`/tasks/${taskId}/cancel`).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks'] }),
  })
}
export function useTriggerReindex() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ schema_id, is_permanent, name }) => {
      const params = { schema_id }
      if (is_permanent) {
        params.is_permanent = true
      }
      if (name) {
        params.name = name
      }
      return api.post('/search/reindex/schema', null, { params }).then(r => r.data)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tasks'] })
      qc.invalidateQueries({ queryKey: ['search'] })
    },
  })
}

export function useTriggerReindexAll() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ is_permanent, name } = {}) => {
      const params = {}
      if (is_permanent) {
        params.is_permanent = true
      }
      if (name) {
        params.name = name
      }
      return api.post('/search/reindex/all', null, { params }).then(r => r.data)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tasks'] })
      qc.invalidateQueries({ queryKey: ['search'] })
    },
  })
}

export function useTriggerExportTask() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data) => api.post('/tasks/', data).then(r => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tasks'] })
    },
  })
}

// Phase 291 (FEEXT-05): generic task creation — POST /tasks/ with a full
// TaskCreate body. Plugin task types submit through this path with
// `plugin_parameters` and `current_step: null` (D-10); admission errors
// (409/422) surface through the mutation's error for the modal's banner.
export function useCreateTask() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data) => api.post('/tasks/', data).then(r => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tasks'] })
    },
  })
}

export function useTriggerImportTask() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ file, import_definition_id }) => {
      const formData = new FormData()
      formData.append('file', file)
      formData.append('import_definition_id', import_definition_id)
      return api.post('/imports/csv/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      }).then(r => r.data)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tasks'] })
    },
  })
}

// --- Index Outbox (dead-letter view for index/remove jobs without Task tracking) ---
export function useFailedOutboxEntries() {
  return useQuery({
    queryKey: ['outbox-failed'],
    queryFn: () => api.get('/search/outbox/failed').then(r => r.data),
    refetchInterval: 30000,
  })
}
export function useRetryOutboxEntry() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (outboxId) => api.post(`/search/outbox/${outboxId}/retry`).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['outbox-failed'] }),
  })
}

// --- Service Accounts ---
export function useServiceAccount(id) {
  return useQuery({
    queryKey: ['service-accounts', id],
    queryFn: () => api.get(`/service-accounts/${id}`).then(r => r.data),
    enabled: !!id,
  })
}
export function useServiceAccounts(sourceId, skip = 0, limit = 50, targetId = null, options = {}) {
  const { includeTotal = false, ...queryOptions } = options
  return useQuery({
    queryKey: ['service-accounts', { sourceId, targetId, skip, limit, includeTotal }],
    queryFn: () => api.get('/service-accounts/', {
      params: { source_id: sourceId || undefined, target_id: targetId || undefined, skip, limit },
    }).then(r =>
      includeTotal
        ? { items: r.data, total: parseInt(r.headers['x-total-count'] || '0', 10) }
        : r.data
    ),
    ...queryOptions,
  })
}
export function useCreateServiceAccount() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data) => api.post('/service-accounts/', data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['service-accounts'] }),
  })
}
export function useUpdateServiceAccount() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...data }) => api.patch(`/service-accounts/${id}`, data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['service-accounts'] }),
  })
}
export function useDeleteServiceAccount() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id) => api.delete(`/service-accounts/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['service-accounts'] }),
  })
}

// --- Schedules ---
export function useSchedules(skip = 0, limit = 100, options = {}) {
  return useQuery({
    queryKey: ['schedules', { skip, limit }],
    queryFn: () => api.get('/schedules/', { params: { skip, limit } }).then(r => ({
      items: r.data,
      total: parseInt(r.headers['x-total-count'] || '0', 10),
    })),
    ...options,
  })
}
export function useCreateSchedule() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data) => api.post('/schedules/', data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['schedules'] }),
  })
}
export function useUpdateSchedule() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...data }) => api.patch(`/schedules/${id}`, data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['schedules'] }),
  })
}
export function useDeleteSchedule() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id) => api.delete(`/schedules/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['schedules'] }),
  })
}
export function useRunSchedule() {
  return useMutation({
    mutationFn: (id) => api.post(`/schedules/${id}/run`).then(r => r.data),
  })
}

// --- Export Definitions ---
export function useExportDefinitions(skip = 0, limit = 100) {
  return useQuery({
    queryKey: ['export-definitions', { skip, limit }],
    queryFn: () => api.get('/export-definitions/', { params: { skip, limit } }).then(r => ({
      items: r.data,
      total: parseInt(r.headers['x-total-count'] || '0', 10),
    })),
  })
}
export function useExportDefinition(id) {
  return useQuery({
    queryKey: ['export-definitions', id],
    queryFn: () => api.get(`/export-definitions/${id}`).then(r => r.data),
    enabled: !!id,
  })
}
export function useCreateExportDefinition() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data) => api.post('/export-definitions/', data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['export-definitions'] }),
  })
}
export function useUpdateExportDefinition() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...data }) => api.patch(`/export-definitions/${id}`, data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['export-definitions'] }),
  })
}
export function useDeleteExportDefinition() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id) => api.delete(`/export-definitions/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['export-definitions'] }),
  })
}

// Export run record (Task.export_id) — carries status/error_message/processed_objects
// for a single export execution, distinct from ExportDefinition (the reusable config).
export function useExportRecord(id) {
  return useQuery({
    queryKey: ['exports', id],
    queryFn: () => api.get(`/exports/${id}`).then(r => r.data),
    enabled: !!id,
  })
}
// DELETE /exports/{id} removes a terminal run (row + file). The history is
// derived from the task list, so both it and the export record are invalidated.
export function useDeleteExportRecord() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id) => api.delete('/exports/' + id).then(r => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tasks'] })
      qc.invalidateQueries({ queryKey: ['exports'] })
    },
  })
}

// --- Import Definitions ---
export function useImportDefinitions(skip = 0, limit = 100) {
  return useQuery({
    queryKey: ['import-definitions', { skip, limit }],
    queryFn: () => api.get('/import-definitions/', { params: { skip, limit } }).then(r => ({
      items: r.data,
      total: parseInt(r.headers['x-total-count'] || '0', 10),
    })),
  })
}
export function useImportDefinition(id) {
  return useQuery({
    queryKey: ['import-definitions', id],
    queryFn: () => api.get(`/import-definitions/${id}`).then(r => r.data),
    enabled: !!id,
  })
}
export function useCreateImportDefinition() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data) => api.post('/import-definitions/', data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['import-definitions'] }),
  })
}
export function useUpdateImportDefinition() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...data }) => api.patch(`/import-definitions/${id}`, data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['import-definitions'] }),
  })
}
export function useDeleteImportDefinition() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id) => api.delete(`/import-definitions/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['import-definitions'] }),
  })
}

// Import run record (Task.import_id) — carries status/error_message/processed_objects
// for a single import execution, distinct from ImportDefinition (the reusable config).
// error_message may hold a JSON-serialized per-row error report (see
// import_execution_service.execute_csv_import).
export function useImportRecord(id) {
  return useQuery({
    queryKey: ['imports', id],
    queryFn: () => api.get(`/imports/${id}`).then(r => r.data),
    enabled: !!id,
  })
}

// --- System Settings ---
export function useSystemSettings() {
  return useQuery({
    queryKey: ['system-settings'],
    queryFn: () => api.get('/settings/').then(r => r.data),
  })
}
export function useUpdateSetting() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ key, value }) => api.put(`/settings/${key}`, { value }),
    onSuccess: (_data, { key }) => {
      qc.invalidateQueries({ queryKey: ['system-settings'] })
      // history_enabled cascades onto the objects/object_values/object_refs
      // audit toggles server-side — keep the config view in sync too.
      if (key === 'password_policy') {
        qc.invalidateQueries({ queryKey: ['password-policy'] })
      }
      if (key === 'history_enabled') {
        qc.invalidateQueries({ queryKey: ['audit-log-config'] })
      }
    },
  })
}

// --- Runtime Log Levels (Phase 299) ---
// Dedicated query key and per-row mutation. Deliberately NOT routed through
// useUpdateSetting: the logging API has its own envelope and TTL validation
// (Plan 01), so a generic settings write must never carry a level change.
export function useLogLevels() {
  return useQuery({
    queryKey: ['logging-levels'],
    queryFn: () => api.get('/logging/levels').then(r => r.data),
  })
}
export function useUpdateLogLevel() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ category, value }) =>
      api.put('/logging/levels', { levels: { [category]: value } }).then(r => r.data),
    onSuccess: (data) => {
      // The PUT response is the authoritative fresh envelope (server_time +
      // categories); adopt it directly so rows never flash a skeleton while
      // the invalidation refetch catches up.
      qc.setQueryData(['logging-levels'], data)
      qc.invalidateQueries({ queryKey: ['logging-levels'] })
    },
  })
}

// --- Icons ---
export function useIcons(schemaId, skip = 0, limit = 50) {
  return useQuery({
    queryKey: ['icons', { schemaId, skip, limit }],
    queryFn: () => api.get('/icons/', { params: { schema_id: schemaId, skip, limit } }).then(r => r.data),
  })
}
export function useCreateIcon() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data) => api.post('/icons/', data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['icons'] }),
  })
}
export function useDeleteIcon() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id) => api.delete(`/icons/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['icons'] }),
  })
}
