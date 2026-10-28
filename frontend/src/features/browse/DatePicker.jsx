import { useState, useRef, useEffect } from 'react'
import { DayPicker } from 'react-day-picker'
import 'react-day-picker/style.css'
import { useTranslation } from 'react-i18next'

function parseLocalDate(isoStr) {
  if (!isoStr) return undefined
  const [y, m, d] = isoStr.slice(0, 10).split('-').map(Number)
  return new Date(y, m - 1, d)
}

function formatLocalDate(date) {
  const y = date.getFullYear()
  const m = String(date.getMonth() + 1).padStart(2, '0')
  const d = String(date.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

export default function DatePicker({ value, onChange, className, small }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  const selectedDate = parseLocalDate(value)
  const timeValue = value ? value.slice(11, 16) : ''

  const handleDaySelect = (date) => {
    if (!date) {
      onChange(null)
      setOpen(false)
      return
    }
    const [hours, minutes] = timeValue ? timeValue.split(':') : ['00', '00']
    const iso = `${formatLocalDate(date)}T${hours}:${minutes}:00`
    onChange(iso)
    setOpen(false)
  }

  const handleTimeChange = (e) => {
    const time = e.target.value
    if (!time || !value) return
    const date = value.slice(0, 10)
    onChange(`${date}T${time}:00`)
  }

  useEffect(() => {
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
    }
    if (open) document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const btnClass = small
    ? 'w-full rounded-md border border-input px-2 py-1 text-xs bg-background text-left focus:outline-none focus:ring-1 focus:ring-primary'
    : (className || 'w-full rounded-md border border-input px-3 py-2 text-sm bg-background text-left focus:outline-none focus:ring-1 focus:ring-primary')

  return (
    <div ref={ref} className="relative inline-flex items-center gap-1">
      <div className="relative">
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className={btnClass}
          style={small ? { minWidth: '110px' } : { minWidth: '140px' }}
        >
          {value ? value.slice(0, 10) : (small ? t('common.date', 'Date') : t('common.select_date', 'Select date'))}
        </button>
        {open && (
          <div className="absolute top-full left-0 mt-1 z-50 bg-card border rounded-lg shadow-lg" style={small ? { transform: 'scale(0.85)', transformOrigin: 'top left' } : undefined}>
            <DayPicker
              mode="single"
              selected={selectedDate}
              onSelect={handleDaySelect}
              defaultMonth={selectedDate || new Date()}
              className={small ? 'rdp-small' : undefined}
            />
          </div>
        )}
      </div>
      <input
        type="time"
        value={timeValue}
        onChange={handleTimeChange}
        className={small
          ? 'w-full rounded-md border border-input px-2 py-1 text-xs bg-background focus:outline-none focus:ring-1 focus:ring-primary'
          : (className || 'w-full rounded-md border border-input px-3 py-2 text-sm bg-background focus:outline-none focus:ring-1 focus:ring-primary')}
        style={small ? { width: '100px' } : { width: '130px' }}
      />
    </div>
  )
}
