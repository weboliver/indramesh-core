import { useMutation } from '@tanstack/react-query'
import api from './use-api'

export function useSchedulePreview() {
  return useMutation({
    mutationFn: (data) => api.post('/schedules/preview', data).then(r => r.data),
  })
}
