import { useSearchParams } from 'react-router-dom'
import { useCallback, useMemo } from 'react'

export function usePaginationParams(defaultPageSize = 25) {
  const [searchParams, setSearchParams] = useSearchParams()

  const page = useMemo(() => {
    const p = parseInt(searchParams.get('page'), 10)
    return Number.isFinite(p) && p >= 1 ? p : 1
  }, [searchParams])

  const pageSize = useMemo(() => {
    const s = parseInt(searchParams.get('page_size'), 10)
    return [10, 25, 50, 100].includes(s) ? s : defaultPageSize
  }, [searchParams, defaultPageSize])

  const setPage = useCallback((newPage) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.set('page', String(newPage))
      return next
    })
  }, [setSearchParams])

  const setPageSize = useCallback((newSize) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.set('page_size', String(newSize))
      next.set('page', '1')
      return next
    })
  }, [setSearchParams])

  const totalPages = useCallback((total) => {
    return Math.max(1, Math.ceil(total / pageSize))
  }, [pageSize])

  return { page, pageSize, setPage, setPageSize, totalPages }
}
