import { useQuery } from '@tanstack/react-query'
import api from './use-api'
import { DEFAULT_PASSWORD_POLICY } from '../lib/password-policy'

export function usePasswordPolicy() {
  const { data } = useQuery({
    queryKey: ['password-policy'],
    queryFn: () => api.get('/users/password-policy').then((r) => r.data),
  })
  return data || DEFAULT_PASSWORD_POLICY
}
