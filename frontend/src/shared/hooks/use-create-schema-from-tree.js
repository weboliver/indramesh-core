import { useMutation } from '@tanstack/react-query'
import api from './use-api'

// R08-1: the strict Core final-save boundary for an accepted plugin schema
// draft. Modelled on use-atomic-create-schema.js but calling the strict
// `/schemas/from-tree` route (Plan 05), never the lenient `atomic-create` — an
// invalid tree is rejected in full with a concrete `{path, reason}` (422), a
// name/key conflict is 409. The existing Core AI/template/scratch paths keep
// calling `atomic-create` unchanged.
export function useCreateSchemaFromTree() {
  return useMutation({
    mutationFn: (data) => api.post('/schemas/from-tree', data).then((r) => r.data),
  })
}
