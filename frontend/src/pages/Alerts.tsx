import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  del, get, patch, post, put,
  type AlertEvent, type AlertKind, type AlertRecipient, type AlertRule, type AlertStatus, type DigestPreview, type Job,
  type MonthlyReadiness,
} from '../api'
import { Button, Card, ErrorNote, Seg } from '../components/ui'
import { useJob } from '../hooks'
import { shortDate } from '../format'

const DAYS: Record<string, string> = { mon: 'Monday', tue: 'Tuesday', wed: 'Wednesday', thu: 'Thursday', fri: 'Friday', sat: 'Saturday', sun: 'Sunday' }
const ORDER: AlertKind[] = ['budget_overspend', 'out_of_norm', 'large_transaction', 'weekly_digest', 'monthly_digest']
const TITLES: Record<AlertKind, string> = {
  budget_overspend: 'Budget overspend',
  out_of_norm: 'Out-of-norm spending',
  large_transaction: 'Large single transaction',
  weekly_digest: 'Weekly highlights',
  monthly_digest: 'Monthly report',
}
type DigestKind = 'weekly' | 'monthly'
const STATUS_TAG: Record<AlertEvent['status'], string> = { sent: 'tag-accent', pending: 'tag-outline', skipped: 'tag-neutral', failed: 'tag-outline' }

export function Alerts() {
  const qc = useQueryClient()
  const status = useQuery({ queryKey: ['alerts', 'status'], queryFn: () => get<AlertStatus>('/alerts/status') })
  const rules = useQuery({ queryKey: ['alerts', 'rules'], queryFn: () => get<AlertRule[]>('/alerts/rules') })
  const events = useQuery({ queryKey: ['alerts', 'events'], queryFn: () => get<AlertEvent[]>('/alerts/events', { limit: 12 }) })
  const readiness = useQuery({
    queryKey: ['alerts', 'monthly-readiness'],
    queryFn: () => get<MonthlyReadiness>('/alerts/digest/monthly/readiness'),
  })
  const [jobId, setJobId] = useState<number | null>(null)
  const job = useJob(jobId)
  const jobDone = job.data && ['succeeded', 'failed', 'cancelled'].includes(job.data.status)
  const [preview, setPreview] = useState<DigestPreview | null>(null)
  const [note, setNote] = useState<string | null>(null)

  const update = useMutation({
    mutationFn: ({ kind, body }: { kind: AlertKind; body: Record<string, unknown> }) => put<AlertRule>(`/alerts/rules/${kind}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['alerts', 'rules'] }),
  })
  const runJob = useMutation({
    mutationFn: (path: string) => post<Job>(path),
    onSuccess: (j) => { setJobId(j.id); setNote(null) },
  })
  const test = useMutation({
    mutationFn: () => post<{ sent_to: string[] }>('/alerts/test'),
    onSuccess: (r) => setNote(`Test email sent to ${r.sent_to.join(', ')}`),
  })
  const digest = useMutation({
    mutationFn: (kind: DigestKind) => get<DigestPreview>('/alerts/digest/preview', { kind, ai: true }),
    onSuccess: setPreview,
  })

  useEffect(() => {
    if (jobDone) {
      qc.invalidateQueries({ queryKey: ['alerts', 'events'] })
      qc.invalidateQueries({ queryKey: ['alerts', 'monthly-readiness'] })
    }
  }, [jobDone, jobId, qc])
  const byKind = new Map((rules.data ?? []).map((r) => [r.kind, r]))
  const s = status.data

  return (
    <section className="page" style={{ maxWidth: 900 }}>
      <header className="page-header">
        <div>
          <h2>Email alerts</h2>
          <div className="text-muted subtitle">
            Rules run after each import and nightly. Sent through the SMTP relay in your environment.
          </div>
        </div>
      </header>
      <ErrorNote error={status.error || rules.error || update.error || runJob.error || test.error || digest.error || readiness.error} />
      {s && !s.smtp_configured && (
        <div className="callout">
          SMTP isn’t configured, so alerts are recorded here but not emailed. Set <code>SMTP_HOST</code>,{' '}
          <code>SMTP_FROM</code> (and usually <code>SMTP_USERNAME</code>/<code>SMTP_PASSWORD</code>) in <code>.env</code>.
        </div>
      )}

      <Card style={{ padding: 0, gap: 0 }}>
        {ORDER.map((kind) => {
          const r = byKind.get(kind)
          if (!r) return null
          return (
            <div key={kind} style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) auto', gap: 'var(--space-4)',
              padding: 'var(--space-3) var(--space-4)', borderBottom: '1px solid var(--color-divider)', alignItems: 'center' }}>
              <div className="stack" style={{ gap: 4 }}>
                <div style={{ fontWeight: 500 }}>{TITLES[kind]}</div>
                <div className="muted-2" style={{ fontSize: 13 }}><RuleDescription rule={r} status={s} /></div>
                {kind === 'monthly_digest' && readiness.data && <Readiness r={readiness.data} />}
                {kind === 'budget_overspend' && (
                  <label className="row small" style={{ gap: 6 }}>
                    <input type="checkbox" checked={r.params.pace !== false}
                      onChange={(e) => update.mutate({ kind, body: { pace: e.target.checked } })} />
                    Also warn when on pace to overspend (after a third of the period)
                  </label>
                )}
                {kind === 'large_transaction' && <Threshold rule={r} onSave={(v) => update.mutate({ kind, body: { threshold: v } })} />}
              </div>
              <Seg name={kind} value={r.enabled ? 'on' : 'off'}
                options={[{ value: 'on', label: 'On' }, { value: 'off', label: 'Off' }]}
                onChange={(v) => update.mutate({ kind, body: { enabled: v === 'on' } })} />
            </div>
          )
        })}
      </Card>

      <div className="grid-2">
        <Recipients />
        <Card style={{ gap: 'var(--space-2)' }}>
          <div className="card-kicker">Last sent</div>
          <div className="stack" style={{ fontSize: 13, gap: 6 }}>
            {(events.data ?? []).map((e) => (
              <div key={e.id} style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) auto', gap: '2px 8px' }}
                title={e.error ?? e.recipients.join(', ')}>
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{e.title}</span>
                <span className="text-muted nowrap">{shortDate(e.created_at)}</span>
                {e.status !== 'sent' && (
                  <span className={`tag ${STATUS_TAG[e.status]}`} style={{ justifySelf: 'start', fontSize: 11 }}>
                    {e.status}{e.error ? ` · ${e.error}` : ''}
                  </span>
                )}
                {e.finding_status && e.finding_status !== 'open' && (
                  <span className="tag tag-neutral" style={{ justifySelf: 'start', fontSize: 11 }}>finding {e.finding_status}</span>
                )}
              </div>
            ))}
            {events.data?.length === 0 && <div className="text-muted">Nothing yet.</div>}
          </div>
        </Card>
      </div>

      <div className="row">
        <Button onClick={() => test.mutate()} disabled={test.isPending || !s?.smtp_configured}>Send test email</Button>
        <Button onClick={() => runJob.mutate('/alerts/run')} disabled={runJob.isPending}>Check rules now</Button>
        <Button onClick={() => digest.mutate('weekly')} disabled={digest.isPending}>Preview weekly</Button>
        <Button onClick={() => digest.mutate('monthly')} disabled={digest.isPending}>Preview monthly</Button>
        <Button onClick={() => runJob.mutate('/alerts/digest/send?kind=weekly')} disabled={runJob.isPending}>Send weekly now</Button>
        <Button onClick={() => runJob.mutate('/alerts/digest/send?kind=monthly')} disabled={runJob.isPending}>Send monthly now</Button>
        <span className="small text-muted">
          {digest.isPending ? 'Building preview…' : note ?? (job.data && (jobDone ? `${job.data.type.replace('_', ' ')}: ${job.data.status}` + resultNote(job.data) : 'Working…'))}
        </span>
      </div>

      {preview && (
        <Card style={{ gap: 'var(--space-2)' }}>
          <div className="row">
            <div className="card-kicker spacer">Digest preview · {preview.subject}</div>
            <Button variant="ghost" onClick={() => setPreview(null)}>Close</Button>
          </div>
          <iframe title="Digest preview" sandbox="" srcDoc={preview.html}
            style={{ width: '100%', height: 520, border: '1px solid var(--color-divider)', background: '#fff' }} />
        </Card>
      )}
    </section>
  )
}

function resultNote(j: Job): string {
  const r = j.result as { new?: number; status?: string } | null
  if (!r) return j.error ? ` — ${j.error}` : ''
  if (typeof r.new === 'number') return ` — ${r.new} new alert${r.new === 1 ? '' : 's'}${r.status ? ` (${r.status})` : ''}`
  return r.status ? ` — ${r.status.replace('_', ' ')}` : ''
}

function RuleDescription({ rule, status }: { rule: AlertRule; status?: AlertStatus }) {
  switch (rule.kind) {
    case 'budget_overspend':
      return <>When any category passes 100% of its period budget{rule.params.pace !== false ? ', or is on pace to by period end' : ''}.</>
    case 'out_of_norm':
      return <>Category or merchant spending well above its trailing 12-month norm, big first-time merchants, and bill jumps.</>
    case 'large_transaction':
      return <>Any single debit over the threshold that isn’t a known recurring payment.</>
    case 'monthly_digest': {
      const settle = status?.monthly_settle_days ?? 3
      return <>
        In-depth review of last month — income, spending and savings vs typical, every budget, categories, merchants,
        net worth and recommendations. Sent once every active account has loaded the month (a statement through month
        end, or transactions/balances {settle} day{settle === 1 ? '' : 's'} past it), and on the{' '}
        {ordinal(status?.monthly_cap_day ?? 8)} at the latest.
      </>
    }
    default: {
      const lagDays = status?.digest_lag_days
      return <>
        {DAYS[status?.digest_weekday ?? 'fri']} morning highlights of the 7 days ending {lagDays ? `${lagDays + 1} days earlier, once late-posting charges have landed` : 'the day before'}:
        spend vs a typical week, the largest charges, budgets running over, and items to review.
      </>
    }
  }
}

function Readiness({ r }: { r: MonthlyReadiness }) {
  const waiting = r.accounts.filter((a) => !a.loaded)
  let text: string
  if (r.sent) text = `${r.label}: report ${r.sent}.`
  else if (!r.accounts.length || r.ready) text = `${r.label}: ${r.accounts.length ? `all ${r.accounts.length} active accounts loaded` : 'no active accounts'} — sends at the next morning check.`
  else text = `${r.label}: waiting on ${waiting.map((a) => a.loaded_through ? `${a.name} (through ${shortDate(a.loaded_through)})` : a.name).join(', ')}; sends by ${shortDate(r.send_by)}.`
  return <div className="small text-muted">{text}</div>
}

function ordinal(n: number): string {
  const suffix = n % 100 >= 11 && n % 100 <= 13 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' } as Record<number, string>)[n % 10] ?? 'th'
  return `${n}${suffix}`
}

function Threshold({ rule, onSave }: { rule: AlertRule; onSave: (v: number) => void }) {
  const [value, setValue] = useState(String(rule.params.threshold ?? 500))
  return (
    <label className="row small" style={{ gap: 6 }}>
      Threshold $
      <input className="input compact" style={{ width: 100 }} inputMode="decimal" value={value}
        onChange={(e) => setValue(e.target.value)}
        onBlur={() => { const v = Number(value); if (v > 0 && v !== rule.params.threshold) onSave(v) }} />
    </label>
  )
}

function Recipients() {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: ['alerts', 'recipients'], queryFn: () => get<AlertRecipient[]>('/alerts/recipients') })
  const [email, setEmail] = useState('')
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['alerts', 'recipients'] })
    qc.invalidateQueries({ queryKey: ['alerts', 'status'] })
  }
  const add = useMutation({
    mutationFn: () => post<AlertRecipient>('/alerts/recipients', { email }),
    onSuccess: () => { setEmail(''); refresh() },
  })
  const toggle = useMutation({
    mutationFn: (r: AlertRecipient) => patch<AlertRecipient>(`/alerts/recipients/${r.id}`, { enabled: !r.enabled }),
    onSuccess: refresh,
  })
  const remove = useMutation({ mutationFn: (id: number) => del(`/alerts/recipients/${id}`), onSuccess: refresh })

  return (
    <Card style={{ gap: 'var(--space-2)' }}>
      <div className="card-kicker">Recipients</div>
      <div className="row" style={{ gap: 6 }}>
        {(list.data ?? []).map((r) => (
          <span key={r.id} className={`tag ${r.enabled ? 'tag-neutral' : 'tag-outline'}`} style={{ gap: 4, opacity: r.enabled ? 1 : 0.6 }}>
            <button className="btn-link" style={{ all: 'unset', cursor: 'pointer' }} title={r.enabled ? 'Pause' : 'Resume'}
              onClick={() => toggle.mutate(r)}>{r.email}</button>
            <button style={{ all: 'unset', cursor: 'pointer', paddingLeft: 4 }} title="Remove"
              onClick={() => remove.mutate(r.id)}>×</button>
          </span>
        ))}
        {list.data?.length === 0 && <span className="small text-muted">No recipients yet.</span>}
      </div>
      <form className="row" onSubmit={(e) => { e.preventDefault(); if (email.trim()) add.mutate() }}>
        <input className="input" style={{ flex: 1 }} type="email" placeholder="Add address" value={email}
          onChange={(e) => setEmail(e.target.value)} />
        <Button type="submit" disabled={add.isPending || !email.trim()}>Add</Button>
      </form>
      <ErrorNote error={add.error || toggle.error || remove.error || list.error} />
      <div className="small text-muted">Click an address to pause or resume it.</div>
    </Card>
  )
}
