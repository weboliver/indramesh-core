import { useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import api from './use-api'
import {
  createTaskLogState,
  mergeTaskLogPage,
  minLevelFor,
  taskLogPollInterval,
} from '../../features/tasks/task-log-state.js'

// Page size matches the API's accepted maximum (Plan 03: 1..500). The hook
// loads every page up to `has_more === false`, then follows the server cursor.
const LOG_PAGE_LIMIT = 500
const MAX_PAGES_PER_FETCH = 1000

function queryKeyFor(taskId, filter) {
  return ['task-logs', taskId, filter]
}

async function fetchLogPage(taskId, after, filter) {
  const params = { after, limit: LOG_PAGE_LIMIT }
  if (filter && filter !== 'all') {
    params.min_level = minLevelFor(filter)
  }
  const response = await api.get(`/tasks/${taskId}/logs`, { params })
  return response.data
}

// Phase 300 (D-04): log polling over the server's `after` cursor. The cache is
// read back on every poll so an increment is one page, not a full reload; a
// changed `pruning_revision` discards the cache and reloads from zero. The
// filter is part of the query key, so a filter change starts a fresh cursor.
// No token is placed in the URL — the existing axios auth client is used.
export function useTaskLogs(taskId, status, filter = 'all') {
  const queryClient = useQueryClient()
  const queryKey = queryKeyFor(taskId, filter)
  const terminalFetchedRef = useRef(false)
  const isActive = status === 'PENDING' || status === 'RUNNING'

  useEffect(() => {
    if (isActive) terminalFetchedRef.current = false
  }, [isActive])

  return useQuery({
    queryKey,
    enabled: !!taskId,
    staleTime: 0,
    queryFn: async () => {
      let state = queryClient.getQueryData(queryKey) || createTaskLogState()
      let after = state.loaded ? state.nextAfter : 0
      for (let page = 0; page < MAX_PAGES_PER_FETCH; page += 1) {
        const payload = await fetchLogPage(taskId, after, filter)
        const merged = mergeTaskLogPage(state, payload)
        if (merged.reset) {
          state = createTaskLogState()
          after = 0
          continue
        }
        state = merged
        if (!payload.has_more) break
        after = payload.next_after
      }
      // For a terminal task the first successful fetch is the single
      // post-completion refetch; for an active task polling continues.
      terminalFetchedRef.current = !isActive
      return state
    },
    refetchInterval: () => taskLogPollInterval(status, terminalFetchedRef.current),
  })
}
