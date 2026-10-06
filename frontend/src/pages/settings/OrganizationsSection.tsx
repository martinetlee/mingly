import { useEffect, useState } from 'react'
import { getOrganizations, getUserOrganizations, setUserOrganizations } from '../../api/client'
import { useSettingField } from '../../hooks/useSettingField'
import { reportError } from '../../lib/toast'
import { Card, Section } from './shared'

const sameList = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i])

export default function OrganizationsSection() {
  const [allOrgs, setAllOrgs] = useState<string[]>([])
  const userOrgs = useSettingField<string[]>({
    initial: [],
    get: () => getUserOrganizations().then((r) => r.organizations),
    set: setUserOrganizations,
    echo: (r) => (r as { organizations?: string[] } | undefined)?.organizations,
    equals: sameList,
    label: 'your organizations',
  })

  useEffect(() => {
    getOrganizations()
      .then((orgs) => setAllOrgs(orgs.map((o) => o.name)))
      .catch((e) => reportError(e, 'Failed to load organizations'))
  }, [])

  const toggle = (org: string) => {
    const current = userOrgs.value
    const next = current.includes(org) ? current.filter((o) => o !== org) : [...current, org]
    userOrgs.setValue(next, { immediate: true })
  }

  return (
    <Section
      title="Your Organizations"
      description="Select which organizations you belong to. This helps AI features identify your colleagues in conversations."
    >
      <Card className="space-y-3">
        {allOrgs.length === 0 ? (
          <p className="text-xs text-slate-500">No organizations found. Add organizations in the Orgs page first.</p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {allOrgs.map((org) => {
              const selected = userOrgs.value.includes(org)
              return (
                <button
                  key={org}
                  type="button"
                  aria-pressed={selected}
                  disabled={!userOrgs.loaded}
                  onClick={() => toggle(org)}
                  className={`rounded-full px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50 ${
                    selected
                      ? 'bg-blue-500/20 text-blue-400 border border-blue-500/40'
                      : 'bg-slate-700 text-slate-400 border border-slate-600 hover:border-slate-500'
                  }`}
                >
                  {org}
                </button>
              )
            })}
          </div>
        )}
        {userOrgs.value.length > 0 && (
          <p className="text-xs text-slate-500">
            Selected: {userOrgs.value.join(', ')}
          </p>
        )}
      </Card>
    </Section>
  )
}
