// Shared, JSX-free task label helpers. Extracted from TaskDetail.jsx so the
// detail summary and the Phase 300 log panel resolve a step exactly the same
// way (Phase 286 PLUGTASK-01 / Phase 296 CLEAN-01).
//
// Recognized Core values keep their translated labels; any value outside the
// Core set (a plugin identifier or a retired sync name) stays literal raw
// diagnostic text — never a guessed label. `stepLabel` returns a plain object
// so callers decide the markup (mono for plugin/unknown values).

export const CORE_TASK_TYPES = new Set(['IMPORT', 'EXPORT', 'REINDEX'])

// Core-emitted steps without a dedicated translated key (STATUSES, set by
// sync_service) keep their plain presentation instead of being mistaken for an
// unknown plugin step.
const CORE_STEPS = new Set([
  'SCHEMA', 'METADATA', 'STATUSES', 'OBJECTTYPES', 'ATTRIBUTES', 'OBJECTS', 'VALUES', 'REFERENCES', 'POSTPROCESSING',
])
// `{ text, mono, empty }`:
// - empty: no step reported yet — callers show the muted fallback (the log
//   passes the `tasks.log.no_step` key, the summary keeps its existing key).
// - mono: the value is a literal plugin/unknown identifier.
export function stepLabel(t, step, {
  noStepKey = 'tasks.plugin.no_step',
  noStepDefault = 'No work step reported yet',
} = {}) {
  if (step === null || step === undefined || step === '') {
    return { text: t(noStepKey, noStepDefault), mono: false, empty: true }
  }
  switch (step) {
    case 'SCHEMA': return { text: t('tasks.step.schema', 'SCHEMA'), mono: false, empty: false }
    case 'METADATA': return { text: t('tasks.step.metadata', 'METADATA'), mono: false, empty: false }
    case 'OBJECTTYPES': return { text: t('tasks.step.object_types', 'OBJECTTYPES'), mono: false, empty: false }
    case 'ATTRIBUTES': return { text: t('tasks.step.attributes', 'ATTRIBUTES'), mono: false, empty: false }
    case 'OBJECTS': return { text: t('tasks.step.objects', 'OBJECTS'), mono: false, empty: false }
    case 'VALUES': return { text: t('tasks.step.values', 'VALUES'), mono: false, empty: false }
    case 'REFERENCES': return { text: t('tasks.step.references', 'REFERENCES'), mono: false, empty: false }
    case 'POSTPROCESSING': return { text: t('tasks.step.postprocessing', 'POSTPROCESSING'), mono: false, empty: false }
    default: break
  }
  if (CORE_STEPS.has(step)) return { text: step, mono: false, empty: false }
  return { text: step, mono: true, empty: false }
}
