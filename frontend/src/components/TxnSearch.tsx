import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { get, type Transaction, type TransactionPage } from '../api'
import { money, shortDate } from '../format'
import { useDebounced } from '../hooks'

/** Search the ledger for any transaction to link, e.g. a payment the matcher didn't suggest. */
export function TxnSearch({ onPick, exclude = [], placeholder = 'Search transactions by description or amount…' }: {
  onPick: (t: Transaction) => void
  exclude?: number[]
  placeholder?: string
}) {
  const [q, setQ] = useState('')
  const term = useDebounced(q.trim(), 250)
  const found = useQuery({
    queryKey: ['transactions', 'pick', term],
    queryFn: () => get<TransactionPage>('/transactions', { q: term, limit: 8, include_hidden: true }),
    enabled: term.length >= 2,
  })
  const items = (found.data?.items ?? []).filter((t) => !exclude.includes(t.id))
  return (
    <div className="stack">
      <input className="input" value={q} placeholder={placeholder} onChange={(e) => setQ(e.target.value)} />
      {term.length >= 2 && !found.isLoading && items.length === 0 && <div className="small text-muted">No matching transactions.</div>}
      {items.map((t) => (
        <div key={t.id} className="row small" style={{ justifyContent: 'space-between', flexWrap: 'nowrap' }}>
          <span style={{ minWidth: 0 }}>
            {shortDate(t.txn_date)} · {t.description} · {money(t.amount)}
            {t.account_name && <span className="text-muted"> · {t.account_name}</span>}
          </span>
          <button type="button" className="btn btn-ghost small" onClick={() => { onPick(t); setQ('') }}>Link</button>
        </div>
      ))}
    </div>
  )
}
