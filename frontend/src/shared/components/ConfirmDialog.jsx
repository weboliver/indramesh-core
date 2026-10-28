import { useState, useEffect, useRef } from 'react'
import { Button } from './Button'
import { useTranslation } from 'react-i18next'

let request = null // { title, message, danger, okOnly, resolve }
let listeners = []

function notify() {
    listeners.forEach(fn => fn(request))
}

function settle(result) {
    const current = request // capture before nulling — settle exactly the request that was open
    request = null
    notify()
    current?.resolve(result)
}

export function confirmDialog({ title, message, danger = false, confirmLabel, cancelLabel } = {}) {
    return new Promise((resolve) => {
        request = { title, message, danger, okOnly: false, confirmLabel, cancelLabel, resolve }
        notify()
    })
}

export function alertDialog({ title, message } = {}) {
    return new Promise((resolve) => {
        request = { title, message, okOnly: true, resolve: () => resolve(true) }
        notify()
    })
}

function useConfirmDialogRequest() {
    const [state, setState] = useState(request)
    useEffect(() => {
        listeners.push(setState)
        return () => {
            listeners = listeners.filter(fn => fn !== setState)
        }
    }, [])
    return state
}

export function ConfirmDialogRoot() {
    const { t } = useTranslation()
    const activeRequest = useConfirmDialogRequest()
    const dialogRef = useRef(null)

    useEffect(() => {
        const dialog = dialogRef.current
        if (!dialog) return
        if (activeRequest) {
            if (!dialog.open) dialog.showModal()
        } else {
            if (dialog.open) dialog.close()
        }
    }, [activeRequest])

    return (
        <dialog
            ref={dialogRef}
            data-testid="confirm-dialog"
            className="confirm-dialog-panel backdrop:bg-black/30 backdrop:backdrop-blur-[2px] rounded-lg border border-border bg-popover text-popover-foreground p-6 shadow-xl max-w-sm w-full"
            role={activeRequest?.danger ? 'alertdialog' : 'dialog'}
            onCancel={(e) => { e.preventDefault(); settle(false) }}
            onClick={(e) => { if (e.target === dialogRef.current) settle(false) }}
        >
            <div onClick={(e) => e.stopPropagation()} className="space-y-4">
                <h2 className="text-lg font-semibold">
                    {activeRequest?.title || (activeRequest?.okOnly ? null : t('common.confirm', 'Confirm'))}
                </h2>
                <p className="text-sm whitespace-pre-wrap break-words max-h-[50vh] overflow-y-auto">
                    {activeRequest?.message}
                </p>
                <div className="flex justify-end gap-2">
                    {!activeRequest?.okOnly && (
                        <Button
                            variant="outline"
                            autoFocus={activeRequest?.danger}
                            data-testid="confirm-dialog-cancel"
                            onClick={() => settle(false)}
                        >
                            {activeRequest?.cancelLabel || t('common.cancel', 'Cancel')}
                        </Button>
                    )}
                    <Button
                        variant={activeRequest?.okOnly ? 'primary' : (activeRequest?.danger ? 'destructive' : 'primary')}
                        autoFocus={!activeRequest?.danger}
                        data-testid="confirm-dialog-confirm"
                        onClick={() => settle(true)}
                    >
                        {activeRequest?.okOnly
                            ? t('common.ok', 'OK')
                            : (activeRequest?.confirmLabel || t('common.confirm', 'Confirm'))}
                    </Button>
                </div>
            </div>
        </dialog>
    )
}
