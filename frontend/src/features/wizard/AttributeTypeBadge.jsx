import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'

export default function AttributeTypeBadge({ typeString, referencedName }) {
    const { t } = useTranslation()
    const label = referencedName
        ? interpolateDefault(t('wizard.attribute.type_reference', '{{type}} → {{name}}'), { type: typeString, name: referencedName })
        : typeString
    return (
        <span className="inline-flex items-center rounded-full bg-muted text-muted-foreground text-xs px-1.5 py-0.5 font-normal">
            {label}
        </span>
    )
}
