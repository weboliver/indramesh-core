import { useObjectType, useObjectTypes } from '../../shared/hooks/use-object-types'
import { useSchema } from '../../shared/hooks/use-schemas'
import { useSource } from '../../shared/hooks/use-sources'
import Breadcrumb from '../../shared/components/Breadcrumb'
import { buildTypeChain, buildObjectTypeBreadcrumbItems } from './object-type-breadcrumb-items'

/**
 * Browse hierarchy breadcrumb adapter (Phase 252, UX-07).
 *
 * Retains the Source → Schema → ObjectType parent-chain data loading, but
 * delegates presentation to the generic Breadcrumb component: it builds the
 * D-03 explicit `items` array from loaded display data only (never a raw
 * identifier fallback, T-252-13) and lets Breadcrumb render the semantic
 * nav/ol structure, links, separators, and aria-current current item.
 */
export default function ObjectTypeBreadcrumb({ objectTypeId, trailing }) {
  const { data: objectType } = useObjectType(objectTypeId)
  const { data: schema } = useSchema(objectType?.schema_id)
  const { data: source } = useSource(schema?.source_id)
  const { data: schemaObjectTypes = [] } = useObjectTypes(objectType?.schema_id)

  const typeChain = buildTypeChain(objectType, schemaObjectTypes)

  const items = buildObjectTypeBreadcrumbItems({ source, schema, typeChain, trailing })

  return <Breadcrumb items={items} />
}