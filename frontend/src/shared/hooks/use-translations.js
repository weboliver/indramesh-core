import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from './use-api'

export function useTranslations(translationType, entityId, options = {}) {
  return useQuery({
    queryKey: ['translations', { translationType, entityId }],
    queryFn: () => api.get('/translations/', {
      params: { translation_type: translationType, entity_id: entityId },
    }).then(r => r.data),
    enabled: !!translationType && !!entityId,
    ...options,
  })
}

// A translated name shows in every list of its entity (sidebar tree, tables,
// detail panels), so a saved translation refreshes those lists as well.
const TRANSLATED_ENTITY_QUERY_KEYS = {
  object_type: ['object-types'],
  object_type_attribute: ['object-type-attributes'],
  select_option: ['object-type-attributes'],
  object_value: ['objects'],
  status: ['statuses'],
  reference_type: ['reference-types'],
}

export function invalidateTranslatedEntity(qc, translationType, entityId) {
  qc.invalidateQueries({ queryKey: ['translations', { translationType, entityId }] })
  const entityKey = TRANSLATED_ENTITY_QUERY_KEYS[translationType]
  if (entityKey) qc.invalidateQueries({ queryKey: entityKey })
}

export function useUpsertTranslation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data) => api.put('/translations/', data).then(r => r.data),
    onSuccess: (data, vars) => invalidateTranslatedEntity(qc, vars.translation_type, vars.entity_id),
  })
}
