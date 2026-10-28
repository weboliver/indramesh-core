import { useState, useRef, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import api from './use-api'

export function useAqlSearch(initialQuery = '') {
  const { t } = useTranslation()
  const [query, setQuery] = useState(initialQuery)
  const [results, setResults] = useState([])
  const [total, setTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState(null)

  const pageRef = useRef(1)
  const pageSizeRef = useRef(25)
  const controllerRef = useRef(null)

  const execute = useCallback(async () => {
    // Abort previous in-flight request
    if (controllerRef.current) {
      controllerRef.current.abort()
    }

    const controller = new AbortController()
    controllerRef.current = controller

    setIsLoading(true)
    setError(null)

    try {
      const res = await api.post(
        '/aql/objects',
        {
          qlQuery: query,
          page: pageRef.current,
          pageSize: pageSizeRef.current,
        },
        { signal: controller.signal }
      )

      if (!controller.signal.aborted) {
        setResults(res.data.items || [])
        setTotal(res.data.total || 0)
      }
    } catch (err) {
      if (
        err?.name === 'AbortError' ||
        err?.name === 'CanceledError' ||
        err?.code === 'ERR_CANCELED'
      ) {
        return // ignore aborted requests
      }
      if (!controller.signal.aborted) {
        // Extract error detail (422 validation errors with AQL position info)
        const detail = err?.response?.data?.detail
        if (detail?.error) {
          setError({
            code: detail.error.code,
            message: detail.error.message,
            line: detail.error.line,
            column: detail.error.column,
          })
        } else {
          setError({ message: t('common.unexpected_error', 'An unexpected error occurred.') })
        }
      }
    } finally {
      if (!controller.signal.aborted) {
        setIsLoading(false)
      }
    }
  }, [query, t])

  const setPage = useCallback((p) => {
    pageRef.current = p
  }, [])

  const setPageSize = useCallback((ps) => {
    pageSizeRef.current = ps
  }, [])

  return {
    query,
    setQuery,
    results,
    total,
    isLoading,
    error,
    execute,
    setPage,
    setPageSize,
  }
}
