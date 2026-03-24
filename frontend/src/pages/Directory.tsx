import { useState, useEffect } from 'react'
import { getCloudStatus } from '../api/client'
import DirectoryBrowser from '../components/DirectoryBrowser'
import ContactDetail from '../components/ContactDetail'
import DirectoryList from '../components/DirectoryList'

type Tab = 'contacts' | 'organizations'

export default function Directory() {
  const [connected, setConnected] = useState<boolean | null>(null)
  const [tab, setTab] = useState<Tab>('contacts')
  const [selectedContact, setSelectedContact] = useState<{ orgId: string; contactId: string } | null>(null)

  useEffect(() => {
    getCloudStatus()
      .then((res) => setConnected(res.status === 'connected'))
      .catch(() => setConnected(false))
  }, [])

  if (connected === null) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-blue-400 border-t-transparent" />
      </div>
    )
  }

  if (!connected) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <h2 className="mb-2 text-lg font-semibold text-slate-200">Shared Directory</h2>
          <p className="text-sm text-slate-400 max-w-sm">
            Connect to Mingly Cloud in Settings to access the shared contact directory and external organizations.
          </p>
        </div>
      </div>
    )
  }

  // Contact detail view
  if (selectedContact) {
    return (
      <ContactDetail
        orgId={selectedContact.orgId}
        contactId={selectedContact.contactId}
        onBack={() => setSelectedContact(null)}
      />
    )
  }

  return (
    <div className="flex flex-col h-full">
      {/* Tab bar */}
      <div className="flex border-b border-slate-700">
        <button
          onClick={() => setTab('contacts')}
          className={`px-4 py-2.5 text-xs font-medium transition-colors ${
            tab === 'contacts'
              ? 'border-b-2 border-blue-500 text-blue-400'
              : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          Shared Contacts
        </button>
        <button
          onClick={() => setTab('organizations')}
          className={`px-4 py-2.5 text-xs font-medium transition-colors ${
            tab === 'organizations'
              ? 'border-b-2 border-blue-500 text-blue-400'
              : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          External Organizations
        </button>
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-hidden">
        {tab === 'contacts' && (
          <DirectoryBrowser
            onSelectContact={(orgId, contactId) => setSelectedContact({ orgId, contactId })}
          />
        )}
        {tab === 'organizations' && <DirectoryList />}
      </div>
    </div>
  )
}
