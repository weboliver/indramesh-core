import { useQuery } from '@tanstack/react-query'
import api from './use-api'

// Phase 291 (FEEXT-05, D-08): the server task-type catalog is the single
// source for Create Task / Create Schedule and the Tasks type filter.
//
// The query key is top-level `['task-types']`, deliberately NOT under
// `['tasks']`: every task mutation invalidates `['tasks']` (see
// use-other-resources), and a catalog refetch on every task action would be
// noise. `staleTime: 0` + `refetchOnMount: 'always'` keep the catalog fresh so
// a newly activated plugin type appears without a page reload; the modal also
// calls `refetch()` each time it opens.
export function useTaskTypes({ enabled = true } = {}) {
  return useQuery({
    queryKey: ['task-types'],
    queryFn: () => api.get('tasks/types').then((r) => r.data),
    staleTime: 0,
    refetchOnMount: 'always',
    enabled,
  })
}