import { useState, useEffect } from 'react'
import { LucideIcon } from '../lib/lucide-icons'
import { useTranslation } from 'react-i18next'

let toastId = 0
let toasts = []
let listeners = []

function notifyListeners() {
    listeners.forEach(fn => fn([...toasts]))
}

export function showToast(message, options = {}) {
    const {
        type = 'success',
        duration = type === 'success' ? 6000 : 0,
        action = null,
    } = options

    const id = toastId++
    toasts.push({
        id,
        message,
        type,
        duration,
        action,
    })

    notifyListeners()
    return id
}

function dismissToast(id) {
    toasts = toasts.filter(toast => toast.id !== id)
    notifyListeners()
}

function useToast() {
    const [state, setState] = useState(toasts)
    useEffect(() => {
        listeners.push(setState)
        return () => {
            listeners = listeners.filter(fn => fn !== setState)
        }
    }, [])
    return { toasts: state, showToast, dismissToast }
}

function ToastItem({ id, message, type, duration, action }) {
    const { t } = useTranslation()
    useEffect(() => {
        if (duration === 0) return

        const timer = setTimeout(() => {
            dismissToast(id)
        }, duration)

        return () => clearTimeout(timer)
    }, [id, duration])

    const isSuccess = type === 'success'
    const icon = isSuccess ? 'check-circle' : 'x-circle'
    const iconColor = isSuccess ? 'text-green-500' : 'text-destructive'
    const borderColor = isSuccess ? 'border-l-green-500' : 'border-l-destructive'

    return (
        <div
            className={`rounded-lg border p-3 shadow-lg bg-card flex items-start gap-2 border-l-4 ${borderColor} transition-all duration-300 ease-out`}
        >
            <LucideIcon
                name={icon}
                size={16}
                className={`${iconColor} flex-shrink-0`}
            />
            <div className="flex-1 min-w-0">
                <p className="text-sm text-foreground">{message}</p>
                {action && (
                    <button
                        onClick={() => {
                            action.onClick()
                            dismissToast(id)
                        }}
                        className="text-xs text-primary hover:underline mt-1"
                    >
                        {action.label}
                    </button>
                )}
            </div>
            <button
                onClick={() => dismissToast(id)}
                className="text-muted-foreground hover:text-foreground flex-shrink-0 ml-auto"
                aria-label={t('common.dismiss_notification', 'Dismiss notification')}
            >
                <LucideIcon name="x" size={14} aria-hidden="true" />
            </button>
        </div>
    )
}

export function ToastContainer() {
    const { toasts } = useToast()

    return (
        <div
            className="fixed bottom-4 right-4 z-[100] flex flex-col gap-2 max-w-sm"
            role="status"
            aria-live="polite"
        >
            {toasts.map(toast => (
                <ToastItem
                    key={toast.id}
                    id={toast.id}
                    message={toast.message}
                    type={toast.type}
                    duration={toast.duration}
                    action={toast.action}
                />
            ))}
        </div>
    )
}
