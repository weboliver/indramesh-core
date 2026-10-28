import api from '../hooks/use-api'

export async function downloadExportFile(exportId) {
  const response = await api.get(`/exports/csv/${exportId}/download`, { responseType: 'blob' })
  const disposition = response.headers['content-disposition'] || ''
  const match = disposition.match(/filename="?([^"]+)"?/)
  const filename = match ? match[1] : `export-${exportId}.csv`
  const url = URL.createObjectURL(new Blob([response.data], { type: 'text/csv' }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}
