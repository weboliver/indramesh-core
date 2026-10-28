import React from 'react'
import { Link } from 'react-router-dom'

function ReferenceChip({ to, label, onClick }) {
  return (
    <Link
      to={to}
      onClick={onClick}
      className="inline-flex items-center bg-muted rounded px-2 py-0 text-xs font-semibold text-primary/80 hover:text-primary hover:underline transition-colors"
    >
      {label}
    </Link>
  )
}

export default React.memo(ReferenceChip)
