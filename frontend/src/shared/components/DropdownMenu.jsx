import { useRef, useEffect, cloneElement } from 'react'
import { cn } from '../lib/utils'
import { Button } from './Button'

function DropdownMenu({ trigger, children, open, onOpenChange, align = 'left', className, ...props }) {
  const ref = useRef(null)

  useEffect(() => {
    const handleKeyDown = (e) => { if (e.key === 'Escape') onOpenChange(false) }
    const handleClickOutside = (e) => {
      if (ref.current && !ref.current.contains(e.target)) onOpenChange(false)
    }
    document.addEventListener('keydown', handleKeyDown)
    document.addEventListener('mousedown', handleClickOutside)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      document.removeEventListener('mousedown', handleClickOutside)
    }
  }, [onOpenChange])

  return (
    <div className="relative" ref={ref}>
      {cloneElement(trigger, { onClick: (event) => {
        trigger.props.onClick?.(event)
        onOpenChange(!open)
      } })}
      {open && (
        <div
          className={cn(
            'absolute top-full mt-1 rounded-md border bg-card text-card-foreground shadow-lg z-50 overflow-hidden',
            align === 'right' ? 'right-0' : 'left-0',
            className
          )}
          {...props}
        >
          {children}
        </div>
      )}
    </div>
  )
}

function DropdownMenuItem({ variant = 'ghost', size = 'sm', className, ...props }) {
  return (
    <Button
      variant={variant}
      size={size}
      className={cn('w-full justify-start rounded-none h-auto py-2', className)}
      {...props}
    />
  )
}

function DropdownMenuSeparator() {
  return <hr className="border-t border-border" />
}

export { DropdownMenu, DropdownMenuItem, DropdownMenuSeparator }
