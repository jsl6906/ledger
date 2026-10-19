import { useId, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { post, put, type CategoryRule, type RuleMatch, type RulePreview } from '../api'
import { money, shortDate } from '../format'
import { useAccounts, useDebounced, useMerchantSearch } from '../hooks'
import { txnPath } from '../links'
import { MATCH_LABELS, ruleBody, specOf, type RuleDraft } from '../rules'
import { CategorySelect } from './CategorySelect'
import { Button, ErrorNote, Field } from './ui'

function MerchantKeyInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const listId = useId()
  const hits = useMerchantSearch(value)
  return (
    <>
      <input className="input" list={listId} value={value} placeholder="merchant key, e.g. costco" onChange={(e) => onChange(e.target.value)} />
      <datalist id={listId}>
        {(hits.data ?? []).map((m) => <option key={m.key} value={m.key}>{m.name} · {m.count} transactions</option>)}
      </datalist>
    </>
  )
}

type Props = {
  initial: RuleDraft
  /** Existing rule being edited (PUT); otherwise a new rule is created. */
  ruleId?: number
  /** Prompt mode: match, category, amount range and "this account"; identical rules are updated instead of rejected. */
  compact?: boolean
  /** In compact mode: the account the "only this account" checkbox refers to. */
  seedAccount?: { id: number; name: string } | null
  /** In compact mode: the amount of the transaction the rule is based on. */
  seedAmount?: string | null
  intro?: ReactNode
  saveLabel?: string
  cancelLabel?: string
  onChange?: (d: RuleDraft) => void
  onDone: (msg: string) => void
  onCancel: () => void
}

/** Which existing transactions a newly saved rule recategorizes. */
type ApplyScope = 'auto' | 'uncategorized' | 'none'

export function RuleEditor({
  initial, ruleId, compact = false, seedAccount, seedAmount, intro, saveLabel, cancelLabel = 'Cancel', onChange, onDone, onCancel,
}: Props) {
  const qc = useQueryClient()
  const accounts = useAccounts()
  const [d, setD] = useState<RuleDraft>(initial)
  const [scope, setScope] = useState<ApplyScope>('auto')
  const [includeUser, setIncludeUser] = useState(false)
  const radioName = useId()
  const update = (patch: Partial<RuleDraft>) => {
    const next = { ...d, ...patch }
    setD(next)
    onChange?.(next)
  }
  const set = <K extends keyof RuleDraft>(k: K, v: RuleDraft[K]) => update({ [k]: v } as Partial<RuleDraft>)

  const spec = useDebounced(specOf(d), 350)
  const preview = useQuery({
    queryKey: ['rules', 'preview', spec],
    queryFn: () => post<RulePreview>('/rules/preview', spec),
    enabled: spec !== null,
    retry: false,
  })
  const p = preview.data
  const fixable = p ? p.uncategorized + p.auto : 0

  const save = useMutation({
    mutationFn: async () => {
      const body = ruleBody(d)
      const rule = ruleId
        ? await put<CategoryRule>(`/rules/${ruleId}`, body)
        : await post<CategoryRule>('/rules', { ...body, replace_existing: compact })
      let updated = 0
      const wanted = scope === 'uncategorized' ? (p?.uncategorized ?? 0)
        : scope === 'auto' ? fixable + (includeUser ? p?.user ?? 0 : 0) : 0
      if (p && wanted > 0) {
        const apply = scope === 'uncategorized' ? { only_uncategorized: true } : { include_user: includeUser }
        updated = (await post<{ updated: number }>(`/rules/${rule.id}/apply`, apply)).updated
      }
      return { rule, updated }
    },
    onSuccess: ({ rule, updated }) => {
      for (const k of ['rules', 'transactions', 'summary', 'details', 'analytics']) qc.invalidateQueries({ queryKey: [k] })
      onDone(`Rule ${ruleId ? 'saved' : 'created'}: ${rule.description} → ${rule.category_name}` +
        (updated ? ` · ${updated.toLocaleString()} transaction${updated === 1 ? '' : 's'} recategorized` : ''))
    },
  })

  const patternInput = d.match_type === 'merchant'
    ? <MerchantKeyInput value={d.pattern} onChange={(v) => set('pattern', v)} />
    : <input className="input" value={d.pattern} placeholder={d.match_type === 'regex' ? 'e.g. ^(shell|exxon|bp)\\b' : 'e.g. netflix'}
        onChange={(e) => set('pattern', e.target.value)} />

  const amountFields = (
    <>
      <Field label="Amount at least">
        <input className="input" type="number" min={0} step="0.01" value={d.amount_min} placeholder="any"
          onChange={(e) => set('amount_min', e.target.value)} />
      </Field>
      <Field label="Amount at most">
        <input className="input" type="number" min={0} step="0.01" value={d.amount_max} placeholder="any"
          onChange={(e) => set('amount_max', e.target.value)} />
      </Field>
    </>
  )
  const seedAbs = seedAmount ? Math.abs(Number(seedAmount)).toFixed(2) : null

  return (
    <div className="stack">
      {intro}
      <div className="grid-form">
        <Field label="Match">
          <select className="input" value={d.match_type} onChange={(e) => set('match_type', e.target.value as RuleMatch)}>
            {(Object.keys(MATCH_LABELS) as RuleMatch[]).map((m) => <option key={m} value={m}>{MATCH_LABELS[m]}</option>)}
          </select>
        </Field>
        <Field label={d.match_type === 'merchant' ? 'Merchant key' : d.match_type === 'regex' ? 'Regex (case-insensitive)' : 'Text (case-insensitive)'}>
          {patternInput}
        </Field>
      </div>
      <Field label="Set category to">
        <CategorySelect value={d.category_id} onChange={(v) => set('category_id', v)} emptyLabel="Choose…" />
      </Field>
      {compact ? (
        <>
          <div className="grid-form">{amountFields}</div>
          {seedAbs && (
            <div className="row small muted-2" style={{ gap: 8 }}>
              <span>This transaction: {money(seedAbs)}</span>
              <button type="button" className="link-btn"
                onClick={() => update({ amount_min: seedAbs, amount_max: seedAbs })}>Exactly this amount</button>
              {(d.amount_min !== '' || d.amount_max !== '') && (
                <button type="button" className="link-btn" onClick={() => update({ amount_min: '', amount_max: '' })}>Any amount</button>
              )}
            </div>
          )}
          {seedAccount && (
            <label className="row small">
              <input type="checkbox" checked={d.account_id === seedAccount.id}
                onChange={(e) => set('account_id', e.target.checked ? seedAccount.id : null)} />
              Only in {seedAccount.name}
            </label>
          )}
        </>
      ) : (
        <>
          <div className="grid-form">
            <Field label="Account (optional)">
              <select className="input" value={d.account_id ?? ''} onChange={(e) => set('account_id', e.target.value ? Number(e.target.value) : null)}>
                <option value="">Any account</option>
                {(accounts.data ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
            </Field>
            {amountFields}
          </div>
          <div className="grid-form">
            <Field label="Priority (lower runs first)">
              <input className="input" type="number" min={0} max={1000} value={d.priority}
                onChange={(e) => set('priority', Math.max(0, Math.min(1000, Number(e.target.value) || 0)))} />
            </Field>
            <Field label="Note">
              <input className="input" value={d.note} onChange={(e) => set('note', e.target.value)} />
            </Field>
          </div>
          <label className="row small">
            <input type="checkbox" checked={d.is_active} onChange={(e) => set('is_active', e.target.checked)} />
            Active
          </label>
        </>
      )}
      <div className="small muted-2">Amounts compare the absolute value, so a range works for charges and refunds alike.</div>

      <ErrorNote error={preview.error} />
      {p && spec && (
        <div className="stack small" style={{ borderLeft: '2px solid var(--color-divider)', paddingLeft: 8 }}>
          <div>
            <strong>Matches {p.matches.toLocaleString()} transaction{p.matches === 1 ? '' : 's'} today</strong>
            {p.matches > 0 && <> · {p.uncategorized} uncategorized · {p.auto} categorized automatically · {p.user} set by you · {p.same} already in this category</>}
          </div>
          {p.samples.length > 0 && (
            <div className="stack" style={{ gap: 2 }}>
              {p.samples.map((s) => (
                <div key={s.id} className="row muted-2" style={{ flexWrap: 'nowrap', gap: 8 }}>
                  <span className="nowrap">{shortDate(s.txn_date)}</span>
                  <Link to={txnPath(s.id)} style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.description}</Link>
                  <span className="num">{money(s.amount)}</span>
                  <span className="nowrap">{s.category_name ?? 'Uncategorized'}{s.category_source === 'user' ? ' (you)' : ''}</span>
                </div>
              ))}
              {p.uncategorized + p.auto + p.user > p.samples.length && <div className="muted-2">…and more</div>}
            </div>
          )}
          {(fixable > 0 || p.user > 0) && (
            <div className="stack" style={{ gap: 2 }}>
              <div className="muted-2">Existing transactions:</div>
              <label className="radio" style={{ fontSize: 'inherit' }}>
                <input type="radio" name={`${radioName}-scope`} checked={scope === 'auto'} onChange={() => setScope('auto')} />
                <span className="dot" />Recategorize all uncategorized and automatically categorized ({fixable.toLocaleString()})
              </label>
              {p.user > 0 && (
                <label className="row" style={{ paddingLeft: 24 }}>
                  <input type="checkbox" checked={includeUser} disabled={scope !== 'auto'}
                    onChange={(e) => setIncludeUser(e.target.checked)} />
                  Also change the {p.user.toLocaleString()} you categorized by hand
                </label>
              )}
              <label className="radio" style={{ fontSize: 'inherit' }}>
                <input type="radio" name={`${radioName}-scope`} checked={scope === 'uncategorized'} onChange={() => setScope('uncategorized')} />
                <span className="dot" />Only uncategorized ({p.uncategorized.toLocaleString()})
              </label>
              <label className="radio" style={{ fontSize: 'inherit' }}>
                <input type="radio" name={`${radioName}-scope`} checked={scope === 'none'} onChange={() => setScope('none')} />
                <span className="dot" />Leave them; only apply going forward
              </label>
            </div>
          )}
          {fixable === 0 && p.user === 0 && <div className="muted-2">Nothing to change today; the rule applies to future transactions.</div>}
        </div>
      )}

      <ErrorNote error={save.error} />
      <div className="row">
        <Button variant="primary" onClick={() => save.mutate()} disabled={!specOf(d) || save.isPending}>
          {save.isPending ? 'Saving…' : saveLabel ?? (ruleId ? 'Save rule' : 'Create rule')}
        </Button>
        <Button variant="ghost" onClick={onCancel}>{cancelLabel}</Button>
      </div>
    </div>
  )
}
