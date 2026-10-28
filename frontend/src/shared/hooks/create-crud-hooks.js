import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from './use-api'

export function createCrudHooks(entityName, apiPath, queryKey, options = {}) {
  const {
    filterKey = null,
    filterParam = null,
    listEnabled = null,
    updateRefetchType = undefined,
  } = options

  function useList(filterId, skip = 0, limit = 50, options = {}) {
    const { includeTotal = false, extraParams = {}, ...queryOptions } = options
    const keyObj = filterKey !== null && filterKey !== undefined
      ? { [filterKey]: filterId, skip, limit, ...extraParams }
      : { skip, limit, ...extraParams }
    const params = filterParam !== null && filterParam !== undefined
      ? { [filterParam]: filterId, skip, limit, ...extraParams }
      : { skip, limit, ...extraParams }
    return useQuery({
      queryKey: [queryKey, keyObj, includeTotal],
      queryFn: () => api.get(apiPath, { params }).then(r =>
        includeTotal
          ? { items: r.data, total: parseInt(r.headers['x-total-count'] || '0', 10) }
          : r.data
      ),
      enabled: listEnabled === true ? true : (filterId !== null && filterId !== undefined ? !!filterId : false),
      ...queryOptions,
    })
  }

  function useGet(id) {
    return useQuery({
      queryKey: [queryKey, id],
      queryFn: () => api.get(`${apiPath}${id}`).then(r => r.data),
      enabled: !!id,
    })
  }

  function useCreate() {
    const qc = useQueryClient()
    return useMutation({
      mutationFn: (data) => api.post(apiPath, data).then(r => r.data),
      onSuccess: () => qc.invalidateQueries({ queryKey: [queryKey] }),
    })
  }

  function useUpdate() {
    const qc = useQueryClient()
    return useMutation({
      mutationFn: ({ id, ...data }) => api.patch(`${apiPath}${id}`, data).then(r => r.data),
      onSuccess: () => qc.invalidateQueries({ queryKey: [queryKey], refetchType: updateRefetchType }),
    })
  }

  function useDelete() {
    const qc = useQueryClient()
    return useMutation({
      mutationFn: (id) => api.delete(`${apiPath}${id}`),
      onSuccess: (data, id) => {
        qc.removeQueries({ queryKey: [queryKey, id] })
        return qc.invalidateQueries({ queryKey: [queryKey] })
      },
    })
  }

  return { useList, useGet, useCreate, useUpdate, useDelete }
}
