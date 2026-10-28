/**
 * Reports Web Vitals (LCP, INP, CLS) to the browser console.
 *
 * Designed for Phase 102 baseline establishment — values are logged
 * for developers to view in browser DevTools console.
 *
 * Usage:
 *   import { useWebVitals } from './shared/hooks/use-web-vitals'
 *   function App() { useWebVitals() }
 */

import { useEffect } from 'react'
import { onCLS, onINP, onLCP } from 'web-vitals'

// T-102-03: metrics are dev-only diagnostics — never logged in production builds.
function logMetric(metric) {
  if (!import.meta.env.DEV) return
  console.log(
    `%c[Web Vitals] ${metric.name}: ${metric.value.toFixed(2)} (${metric.rating})`,
    `color: ${metric.rating === 'good' ? '#16a34a' : metric.rating === 'needs-improvement' ? '#ca8a04' : '#dc2626'}; font-weight: bold;`,
    { metric }
  )
}

export function useWebVitals() {
  useEffect(() => {
    onLCP(logMetric)
    onINP(logMetric)
    onCLS(logMetric)
  }, [])
}
