import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../lib/i18n-defaults'
import { usePluginSections, usePluginText } from './PluginProvider'
import { PluginSlotBoundary, PluginSlotErrorRow } from './PluginSlotBoundary'

// One shared region/frame pair for every section host (object detail, schema
// settings, object type settings). With zero contributions the region renders
// nothing at all, so a host page stays pixel-identical when no plugin
// contributes (S1, D-01/D-02).
export function PluginSectionRegion({
  family,
  headingLevel = 'h3',
  gapClass = 'space-y-4',
  componentProps = {},
  presenceContext,
}) {
  const { t } = useTranslation()
  const sections = usePluginSections(family, presenceContext)

  if (sections.length === 0) return null

  return (
    <section
      aria-label={t('plugins.sections.region_label', 'Plugin extensions')}
      className={gapClass}
      data-testid={'plugin-section-region-' + family}
    >
      {sections.map((entry) => (
        <PluginSectionFrame
          key={`${entry.pluginName}:${entry.id}`}
          family={family}
          entry={entry}
          headingLevel={headingLevel}
          componentProps={componentProps}
        />
      ))}
    </section>
  )
}

function PluginSectionFrame({ family, entry, headingLevel, componentProps }) {
  const { t } = useTranslation()
  const pluginText = usePluginText()
  const Heading = headingLevel
  const slot = family + ':' + entry.id
  const Component = entry.component
  // User decision 2026-10-11: the schema settings region renders no attribution
  // line. The override is scoped to this family ONLY — every other section
  // family keeps its current frame and the shared provided_by key.
  const showAttribution = family !== 'schemaSettingsSections'

  return (
    <div
      className="rounded-lg border bg-card p-6 space-y-3"
      data-testid={'plugin-section-' + entry.pluginName + '-' + entry.id}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <Heading className="text-lg font-semibold break-words">
          {pluginText(entry.pluginName, entry.labelKey, entry.label)}
        </Heading>
        {showAttribution && (
          <p className="text-xs text-muted-foreground" data-testid="plugin-section-attribution">
            {interpolateDefault(
              t('plugins.sections.provided_by', 'Provided by plugin "{{plugin}}"'),
              { plugin: entry.pluginName }
            )}
          </p>
        )}
      </div>
      <PluginSlotBoundary
        key={entry.pluginName + ':' + entry.id}
        slot={slot}
        pluginName={entry.pluginName}
        fallback={<PluginSlotErrorRow slot={slot} />}
      >
        <Component {...componentProps} />
      </PluginSlotBoundary>
    </div>
  )
}