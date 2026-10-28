import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from '../../shared/hooks/use-api'

export function usePlugins() {
  return useQuery({
    queryKey: ['plugins'],
    queryFn: () => api.get('plugins/').then(r => r.data),
  })
}

export function useUpdatePluginState() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, state }) => api.put(`plugins/${name}/state`, { state }).then(r => r.data),
    // CORESEAM-09: the runtime shell discovers bundles from `plugins/runtime`
    // (PluginProvider), a SEPARATE query key from this admin list — invalidate
    // both so a kill-switch immediately drops the plugin's contributions.
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['plugins'] })
      qc.invalidateQueries({ queryKey: ['plugin-runtime'] })
    },
  })
}

export function usePluginConfig(name, { enabled = false } = {}) {
  return useQuery({
    queryKey: ['plugin-config', name],
    queryFn: () => api.get(`plugins/${name}/config`).then(r => r.data),
    enabled: !!name && enabled,
  })
}

export function useUpdatePluginConfig() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, config }) => api.put(`plugins/${name}/config`, { config }).then(r => r.data),
    onSuccess: (_data, variables) => {
      qc.invalidateQueries({ queryKey: ['plugins'] })
      qc.invalidateQueries({ queryKey: ['plugin-config', variables.name] })
    },
  })
}

export function usePluginHealth(name) {
  return useQuery({
    queryKey: ['plugin-health', name],
    queryFn: () => api.get(`plugins/${name}/health`).then(r => r.data),
    enabled: false,
    staleTime: Infinity,
    retry: false,
  })
}