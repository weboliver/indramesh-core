import { useMutation } from '@tanstack/react-query'
import api from './use-api'

export function useCreateSchemaTemplate() {
  return useMutation({
    mutationFn: (data) => api.post('/schema-templates/', data).then(r => r.data),
  })
}
