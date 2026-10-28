import { useQuery } from '@tanstack/react-query'
import api from './use-api'

// --- Classification catalogue ---
// The catalogue is global (no schema scope) and read-only for the editor: the
// three families and their labels, names resolved in the UI language. Only the
// catalogue's own label `id` is written back on an attribute.
export function useClassificationFamilies(lang = 'en') {
  return useQuery({
    queryKey: ['classification-families', lang],
    queryFn: () => api.get('/classification-families/', {
      params: lang ? { lang } : {},
    }).then(r => r.data),
    staleTime: 60_000,
  })
}
