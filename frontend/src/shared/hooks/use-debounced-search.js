import { useState, useEffect, useRef } from 'react'

export default function useDebouncedSearch({ fetcher, debounceMs = 300, minChars = 2, enabled = true, initialQuery = '', refreshKey = 0 }) {
  const [query, setQuery] = useState(initialQuery)
  const [results, setResults] = useState([])
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState(null)

  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const controllerRef = useRef(null)

  useEffect(() => {
    if (!enabled || query.length < minChars) {
      setResults([])
      setError(null)
      return
    }

    const timer = setTimeout(async () => {
      if (controllerRef.current) {
        controllerRef.current.abort()
      }
      const controller = new AbortController()
      controllerRef.current = controller

      setIsLoading(true)
      try {
        const data = await fetcherRef.current(query, controller.signal)
        if (!controller.signal.aborted) {
          setResults(data)
          setError(null)
        }
      } catch (err) {
        if (err?.name === 'AbortError' || err?.name === 'CanceledError' || err?.code === 'ERR_CANCELED') {
          // aborted — ignore
        } else if (!controller.signal.aborted) {
          setError(err)
        }
      } finally {
        if (!controller.signal.aborted) {
          setIsLoading(false)
        }
      }
    }, debounceMs)

    return () => clearTimeout(timer)
  }, [query, enabled, minChars, debounceMs, refreshKey])

  useEffect(() => {
    return () => {
      if (controllerRef.current) {
        controllerRef.current.abort()
      }
    }
  }, [])

  return { query, setQuery, results, isLoading, error }
}
