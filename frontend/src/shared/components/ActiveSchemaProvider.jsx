import { createContext, useContext, useState, useCallback, useMemo } from 'react'

const ActiveSchemaContext = createContext(null)

export function ActiveSchemaProvider({ children }) {
  const [activeSourceId, setActiveSourceIdState] = useState(() => localStorage.getItem('indramesh_active_source_id'))
  const [activeSchemaId, setActiveSchemaIdState] = useState(() => localStorage.getItem('indramesh_active_schema_id'))
  const [activeObjectTypeId, setActiveObjectTypeIdState] = useState(() => localStorage.getItem('indramesh_active_object_type_id'))

  const setActiveSource = useCallback((sourceId) => {
    setActiveSourceIdState(sourceId)
    setActiveSchemaIdState(null)
    if (sourceId) localStorage.setItem('indramesh_active_source_id', sourceId)
    else localStorage.removeItem('indramesh_active_source_id')
    localStorage.removeItem('indramesh_active_schema_id')
    localStorage.removeItem('indramesh_active_object_type_id')
  }, [])

  // Schema-only setter — assumes the source is already correct. Used by the
  // Sidebar's own bootstrap/validation effects so they never clobber a
  // source that was just set atomically via setActiveSchema below.
  const setActiveSchemaId = useCallback((schemaId) => {
    setActiveSchemaIdState(schemaId)
    if (schemaId) localStorage.setItem('indramesh_active_schema_id', schemaId)
    else localStorage.removeItem('indramesh_active_schema_id')
    localStorage.removeItem('indramesh_active_object_type_id')
  }, [])

  // Sets source + schema together so a schema switch coming from outside the
  // Sidebar (e.g. following a search result into a different schema) lands
  // both values atomically — a schema-only update would leave activeSourceId
  // stale, breaking the Sidebar's source <select> and its schemas query.
  const setActiveSchema = useCallback((sourceId, schemaId) => {
    setActiveSourceIdState(sourceId)
    setActiveSchemaIdState(schemaId)
    if (sourceId) localStorage.setItem('indramesh_active_source_id', sourceId)
    if (schemaId) localStorage.setItem('indramesh_active_schema_id', schemaId)
    localStorage.removeItem('indramesh_active_object_type_id')
  }, [])

  const setActiveObjectTypeId = useCallback((objectTypeId) => {
    setActiveObjectTypeIdState(objectTypeId)
    if (objectTypeId) localStorage.setItem('indramesh_active_object_type_id', objectTypeId)
    else localStorage.removeItem('indramesh_active_object_type_id')
  }, [])

  const value = useMemo(
    () => ({ activeSourceId, activeSchemaId, activeObjectTypeId, setActiveSource, setActiveSchemaId, setActiveSchema, setActiveObjectTypeId }),
    [activeSourceId, activeSchemaId, activeObjectTypeId, setActiveSource, setActiveSchemaId, setActiveSchema, setActiveObjectTypeId]
  )

  return (
    <ActiveSchemaContext.Provider value={value}>
      {children}
    </ActiveSchemaContext.Provider>
  )
}

export function useActiveSchema() {
  const ctx = useContext(ActiveSchemaContext)
  if (!ctx) throw new Error('useActiveSchema must be used within an ActiveSchemaProvider')
  return ctx
}
