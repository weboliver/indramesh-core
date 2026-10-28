import { useMutation, useQueryClient } from '@tanstack/react-query'
import api from './use-api'

export function useTemplateImportSingle() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ schemaId, light, dark }) => {
      const body = {}
      if (light) body.light = light
      if (dark) body.dark = dark
      return api.post(`/schemas/${schemaId}/template`, body).then(r => r.data)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['schemas'] }),
  })
}
