import { useObjects } from './use-objects'
import { useObjectTypeAttributes } from './use-other-resources'

export function useObjectTypeGraphInfo(nodeId) {
  const objects = useObjects(nodeId, 0, 1)
  const attrs = useObjectTypeAttributes(nodeId)
  return {
    count: objects.data?.total ?? 0,
    attributes: attrs.data ?? [],
    isLoading: objects.isLoading || attrs.isLoading,
    isError: objects.isError || attrs.isError,
  }
}
