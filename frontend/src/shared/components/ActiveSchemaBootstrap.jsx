import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import api from '../hooks/use-api'
import { useSources } from '../hooks/use-sources'
import { useTemplate } from './TemplateProvider'
import { useActiveSchema } from './ActiveSchemaProvider'

const EMPTY_ARRAY = []

// Validates the persisted active source/schema and applies the active schema's
// template on every route, independent of whether the Sidebar is rendered. The
// queries share their keys with the Sidebar, so React Query dedupes them.
export default function ActiveSchemaBootstrap() {
  const { activeSourceId, activeSchemaId, setActiveSource, setActiveSchemaId } = useActiveSchema()
  const { setTemplate, clearTemplate } = useTemplate()

  const { data: sources = EMPTY_ARRAY } = useSources(0, 1000)
  const { data: schemas = EMPTY_ARRAY, isLoading: schemasLoading } = useQuery({
    queryKey: ['schemas', { source_id: activeSourceId }],
    queryFn: () => api.get('/schemas/', { params: { source_id: activeSourceId, skip: 0, limit: 100 } }).then(r => r.data),
    enabled: !!activeSourceId,
  })

  const activeSchema = schemas.find(s => s.id === activeSchemaId)

  useEffect(() => {
    if (activeSchema) setTemplate(activeSchema)
  }, [activeSchema, setTemplate])

  useEffect(() => {
    if (!activeSchemaId && !schemasLoading && sources.length > 0) clearTemplate()
  }, [activeSchemaId, schemasLoading, sources.length, clearTemplate])

  useEffect(() => {
    if (sources.length === 0) return
    if (!activeSourceId || !sources.find(s => s.id === activeSourceId)) {
      setActiveSource(sources[0].id)
    }
  }, [sources, activeSourceId, setActiveSource])

  useEffect(() => {
    if (schemas.length > 0) {
      const saved = localStorage.getItem('indramesh_active_schema_id')
      const stillExists = schemas.find(s => s.id === saved)
      if (saved && stillExists) {
        setActiveSchemaId(saved)
      } else {
        setActiveSchemaId(schemas[0].id)
      }
    } else {
      setActiveSchemaId(null)
    }
  }, [schemas, setActiveSchemaId])

  return null
}
