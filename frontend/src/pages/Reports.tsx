import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { get, type BalanceSeries, type CashflowMonth, type CategoryRow, type Merchant, type Trend } from '../api'
import { stackColor } from '../chartUtils'
import { BalanceChart, ESTIMATED_FILL, Legend, NET_DOWN, NET_UP, NetChart, SeriesHistory, StackedBars } from '../components/Charts'
import { Button, Card, ErrorNote, Seg, SortTh } from '../components/ui'
import { iso, money, moneyRound, monthEnd, parseIso, shortDate } from '../format'
import { useCategories, useGroups } from '../hooks'
import { categoryPath, groupPath, merchantPath, txnsPath } from '../links'
import { tipHandlers, useTip, type TipSpec } from '../tip'
import { numParam, oneOf, sortRows, useUrl, useUrlSort } from '../urlState'

type Range = '12m' | '24m' | 'ytd' | 'last_year' | 'all' | 'years'
const RANGES: Range[] = ['12m', '24m', 'ytd', 'last_year', 'all', 'years']
const LEVELS = ['group', 'category'] as const
type Level = (typeof LEVELS)[number]

// Net worth bands by account type; liabilities stack below zero.
const NW_GROUPS: { name: string; types: string[]; color: string }[] = [
  { name: 'Cash', types: ['checking', 'savings', 'cash'], color: '#416180' },
  { name: 'Investments', types: ['investment', 'retirement'], color: '#94bce3' },
  { name: 'Property & vehicles', types: ['property', 'vehicle'], color: '#c9a26b' },
  { name: 'Other assets', types: ['other'], color: '#6f9a8d' },
  { name: 'Credit cards', types: ['credit_card'], color: NET_DOWN },
  { name: 'Loans', types: ['loan', 'mortgage'], color: '#b77b7b' },
]
const CASH_TYPES = ['checking', 'savings']
const CASH_BANDS = 7
const signedRound = (v: number) => (v > 0 ? '+' : '') + moneyRound(v)
type BalAccount = BalanceSeries['accounts'][number]
const sumAt = (accts: BalAccount[], i: number) => accts.reduce((a, x) => a + (x.values[i] ?? 0), 0)

/** One balance chart's data for a set of accounts, starting at the first month any of them has a balance: bands, the
 * total line (null where none has a balance), and which months lean materially on estimates. */
function balanceView(months: string[], accts: BalAccount[], groups: { name: string; color: string; accts: BalAccount[] }[]) {
  const from = Math.max(0, months.findIndex((_, i) => accts.some((a) => a.values[i] !== null)))
  const cut = new Map(accts.map((a) => [a.id, { ...a, values: a.values.slice(from), estimated: a.estimated.slice(from) }]))
  const rows = [...cut.values()]
  const ms = months.slice(from)
  const has = ms.map((_, i) => rows.some((a) => a.values[i] !== null))
  const total = has.map((h, i) => (h ? sumAt(rows, i) : null))
  const estAmount = ms.map((_, i) => rows.reduce((s, a) => s + (a.estimated[i] ? Math.abs(a.values[i] ?? 0) : 0), 0))
  const gross = ms.map((_, i) => rows.reduce((s, a) => s + Math.abs(a.values[i] ?? 0), 0))
  return {
    months: ms, accts: rows, total, estAmount,
    estimated: estAmount.map((e, i) => e > 0.05 * gross[i]),
    first: has.indexOf(true),
    last: has.lastIndexOf(true),
    bands: groups.map((g) => ({ ...g, accts: g.accts.map((a) => cut.get(a.id)!) }))
      .filter((b) => b.accts.some((a) => a.values.some((v) => v)))
      .map((b) => ({ ...b, values: ms.map((_, i) => sumAt(b.accts, i)) })),
  }
}

function rangeDates(r: Range, firstYear: number, fromYear: number, toYear: number): { start: string; end: string } {
  const now = new Date()
  const endOfMonth = iso(new Date(now.getFullYear(), now.getMonth() + 1, 0))
  switch (r) {
    case '24m': return { start: iso(new Date(now.getFullYear(), now.getMonth() - 23, 1)), end: endOfMonth }
    case 'ytd': return { start: iso(new Date(now.getFullYear(), 0, 1)), end: endOfMonth }
    case 'last_year': return { start: iso(new Date(now.getFullYear() - 1, 0, 1)), end: iso(new Date(now.getFullYear() - 1, 11, 31)) }
    case 'all': return { start: iso(new Date(firstYear, 0, 1)), end: endOfMonth }
    case 'years': {
      const end = iso(new Date(toYear, 11, 31))
      return { start: iso(new Date(fromYear, 0, 1)), end: end < endOfMonth ? end : endOfMonth }
    }
    default: return { start: iso(new Date(now.getFullYear(), now.getMonth() - 11, 1)), end: endOfMonth }
  }
}

export function Reports() {
  const tip = useTip()
  const navigate = useNavigate()
  const allCats = useCategories()
  const allGroups = useGroups()
  const [params, set] = useUrl()
  const range = oneOf(params, 'range', RANGES, '12m')
  const histLevel = oneOf(params, 'hl', LEVELS, 'group')
  const histKind = oneOf(params, 'hk', ['expense', 'income'] as const, 'expense')
  const histAll = params.get('hall') === '1'
  const netMode = oneOf(params, 'net', ['waterfall', 'monthly'] as const, 'waterfall')
  const incLevel = oneOf(params, 'inc', LEVELS, 'category')
  const expLevel = oneOf(params, 'exp', LEVELS, 'group')
  const ms = useUrlSort({ merchant: 'asc', count: 'desc', spent: 'desc' }, null, 'msort')
  const [thisYear] = useState(() => new Date().getFullYear())
  const span = useQuery({ queryKey: ['analytics', 'span'], queryFn: () => get<{ start: string | null; end: string | null }>('/analytics/span') })
  const firstYear = span.data?.start ? Number(span.data.start.slice(0, 4)) : thisYear
  const years = Array.from({ length: thisYear - firstYear + 1 }, (_, i) => thisYear - i)
  const fromYear = numParam(params, 'from') ?? thisYear - 1
  const toYear = numParam(params, 'to') ?? thisYear
  const { start, end } = rangeDates(range, firstYear, fromYear, toYear)
  const month = params.get('month')
  const pickMonth = (m: string | null) => set({ month: m === month ? null : m })
  const focus = month ? { start: month, end: monthEnd(month) } : { start, end }

  const flow = useQuery({ queryKey: ['analytics', 'cashflow', start, end], queryFn: () => get<CashflowMonth[]>('/analytics/cashflow', { start, end }) })
  const cats = useQuery({
    queryKey: ['analytics', 'categories', focus.start, focus.end],
    queryFn: () => get<{ rows: CategoryRow[] }>('/analytics/categories', focus),
  })
  const hist = useQuery({
    queryKey: ['analytics', 'trend', 'history', start, end, histLevel, histKind],
    queryFn: () => get<Trend>('/analytics/category-trend', { start, end, level: histLevel, kind: histKind, top: 20 }),
  })
  const incStack = useQuery({
    queryKey: ['analytics', 'trend', 'income', start, end, incLevel],
    queryFn: () => get<Trend>('/analytics/category-trend', { start, end, level: incLevel, kind: 'income', top: 5, other: true }),
  })
  const expStack = useQuery({
    queryKey: ['analytics', 'trend', 'expense', start, end, expLevel],
    queryFn: () => get<Trend>('/analytics/category-trend', { start, end, level: expLevel, kind: 'expense', top: 7, other: true }),
  })
  const merchants = useQuery({
    queryKey: ['analytics', 'merchants', focus.start, focus.end],
    queryFn: () => get<Merchant[]>('/analytics/merchants', { ...focus, limit: 12 }),
  })
  const openGroup = params.get('open')
  const setOpenGroup = (g: string | null) => set({ open: g }, { replace: true })

  const bal = useQuery({ queryKey: ['analytics', 'balances', start, end], queryFn: () => get<BalanceSeries>('/analytics/balances', { start, end }) })
  const balMonths = bal.data?.months ?? []
  const allAccts = bal.data?.accounts ?? []
  const nw = balanceView(balMonths, allAccts,
    NW_GROUPS.map((g) => ({ ...g, accts: allAccts.filter((a) => g.types.includes(a.account_type)) })))
  const cashAccts = allAccts.filter((a) => CASH_TYPES.includes(a.account_type))
  const peak = (a: BalAccount) => Math.max(0, ...a.values.map((v) => Math.abs(v ?? 0)))
  const byPeak = [...cashAccts].sort((a, b) => peak(b) - peak(a))
  const shownCash = byPeak.length > CASH_BANDS ? byPeak.slice(0, CASH_BANDS - 1) : byPeak
  const otherCash = byPeak.slice(shownCash.length)
  const cash = balanceView(balMonths, cashAccts, [
    ...shownCash.map((a, i) => ({ name: a.name, color: stackColor(a.name, i), accts: [a] })),
    ...(otherCash.length ? [{ name: 'Other', color: stackColor('Other', 0), accts: otherCash }] : []),
  ])
  const change = (v: ReturnType<typeof balanceView>) => (v.first >= 0 ? (v.total[v.last] ?? 0) - (v.total[v.first] ?? 0) : 0)
  const lateJoiners = nw.accts
    .map((a) => ({ a, at: a.values.findIndex((v) => v !== null) }))
    .filter(({ at }) => nw.first >= 0 && at > nw.first)
    .sort((x, y) => Math.abs(y.a.values[y.at] ?? 0) - Math.abs(x.a.values[x.at] ?? 0))
  const balTip = (v: ReturnType<typeof balanceView>, label: string, perAccount: boolean) => (i: number): TipSpec | null => {
    const total = v.total[i]
    if (total === null) return null
    const prev = v.total[i - 1]
    const parts = perAccount
      ? v.bands.flatMap((b) => b.accts).filter((a) => a.values[i])
        .sort((a, b) => (b.values[i] ?? 0) - (a.values[i] ?? 0))
        .map((a) => `${a.name}: ${money(a.values[i])}${a.estimated[i] ? ' (est.)' : ''}`)
      : v.bands.filter((b) => b.values[i]).map((b) => `${b.name}: ${money(b.values[i])}`)
    const est = v.accts.filter((a) => a.estimated[i]).map((a) => a.name)
    return {
      title: `${monthName(v.months[i])} · ${label} ${money(total)}`,
      lines: [
        ...(prev !== null && prev !== undefined ? [`${money(total - prev, true)} vs prior month`] : []),
        ...parts,
        ...(est.length && !perAccount ? [`Estimated: ${est.join(', ')} (${money(v.estAmount[i])})`] : []),
      ],
    }
  }

  const totals = (flow.data ?? []).reduce((a, m) => ({ inc: a.inc + m.income, exp: a.exp + m.expenses }), { inc: 0, exp: 0 })
  const n = Math.max(1, flow.data?.length ?? 1)
  const net = totals.inc - totals.exp
  const byNet = [...(flow.data ?? [])].sort((a, b) => b.net - a.net)
  const upMonths = byNet.filter((m) => m.net > 0).length
  const monthName = (m: string) => parseIso(m).toLocaleString('en-US', { month: 'short', year: 'numeric' })
  const groups = new Map<string, { id: number | null; spent: number; cats: CategoryRow[] }>()
  for (const r of cats.data?.rows ?? []) {
    if (r.spent <= 0) continue
    const g = groups.get(r.group) ?? { id: r.group_id, spent: 0, cats: [] }
    g.spent += r.spent
    g.cats.push(r)
    groups.set(r.group, g)
  }
  const groupList = [...groups.entries()].sort((a, b) => b[1].spent - a[1].spent)
  const maxGroup = Math.max(1, ...groupList.map(([, g]) => g.spent))
  const exportUrl = `/api/transactions/export.csv?start=${focus.start}&end=${focus.end}`
  const histSeries = (hist.data?.series ?? []).slice(0, histAll ? undefined : 10)
  const sid = (s: { id: number | null }) => s.id ?? 0
  const stackTip = (q: typeof incStack, kind: 'income' | 'expense', level: 'group' | 'category') =>
    (i: number, si: number | null, ctx: { total: number; avg: number }): TipSpec | null => {
      const d = q.data
      if (!d) return null
      const m = d.months[i]
      const base = { start: m, end: monthEnd(m), kind, level }
      if (si === null) {
        return { title: `${monthName(m)} · ${kind === 'income' ? 'income' : 'expenses'}`,
          lines: [`${money(ctx.total)} · 12-mo avg ${money(ctx.avg)}`], contrib: base }
      }
      const s = d.series[si]
      const others = d.series.filter((x) => x.name !== 'Other').map(sid)
      return { title: `${s.name} · ${monthName(m)}`, lines: [money(s.values[i])],
        contrib: s.name === 'Other' ? { ...base, exclude: others } : { ...base, ids: [sid(s)] } }
    }
  const openSegment = (q: typeof incStack, kind: 'income' | 'expense', level: 'group' | 'category') =>
    (i: number, si: number) => {
      const d = q.data
      if (!d) return
      const m = d.months[i]
      const s = d.series[si]
      const when = { start: m, end: monthEnd(m) }
      const label = `${s.name} · ${monthName(m)}`
      const cats = allCats.data ?? []
      if (s.name === 'Other') {
        const shown = new Set(d.series.filter((x) => x.id !== null).map((x) => x.id))
        const hiddenGroups = new Set((allGroups.data ?? []).filter((g) => g.hide_from_reports).map((g) => g.id))
        const ids = cats.filter((c) => (c.type === kind || c.type === 'transfer') && !c.hide_from_reports && !hiddenGroups.has(c.group_id)
          && !shown.has(level === 'group' ? c.group_id : c.id)).map((c) => c.id)
        if (ids.length) navigate(txnsPath({ categories: ids, label: `Other ${kind} · ${monthName(m)}`, ...when }))
      } else if (s.id === null) {
        navigate(txnsPath({ status: 'uncategorized', ...when }))
      } else if (level === 'category') {
        navigate(txnsPath({ category: s.id, ...when }))
      } else {
        navigate(txnsPath({ categories: cats.filter((c) => c.group_id === s.id).map((c) => c.id), label, ...when }))
      }
    }

  return (
    <section className="page">
      <header className="page-header">
        <div>
          <h2>Reports</h2>
          <div className="text-muted subtitle">
            {shortDate(start)} {start.slice(0, 4)} – {shortDate(end)} {end.slice(0, 4)} · income {money(totals.inc)} · expenses {money(totals.exp)}
            · avg {money(totals.exp / n)}/mo
          </div>
        </div>
        <div className="row">
          <Seg name="range" value={range} onChange={(v) => set(v === 'years' ? { range: v } : { range: v === '12m' ? null : v, from: null, to: null })} options={[
            { value: '12m', label: '12 months' }, { value: '24m', label: '24 months' },
            { value: 'ytd', label: 'Year to date' }, { value: 'last_year', label: 'Last year' },
            { value: 'all', label: 'All time' }, { value: 'years', label: 'Years' },
          ]} />
          {range === 'years' && (
            <>
              <select className="input compact" aria-label="From year" value={fromYear}
                onChange={(e) => { const y = Number(e.target.value); set({ from: y, to: Math.max(y, toYear) }) }}>
                {years.map((y) => <option key={y} value={y}>{y}</option>)}
              </select>
              <span className="text-muted">to</span>
              <select className="input compact" aria-label="To year" value={toYear}
                onChange={(e) => { const y = Number(e.target.value); set({ to: y, from: Math.min(y, fromYear) }) }}>
                {years.map((y) => <option key={y} value={y}>{y}</option>)}
              </select>
            </>
          )}
          <a className="btn btn-secondary" href={exportUrl}>Export CSV</a>
        </div>
      </header>
      <ErrorNote error={span.error || flow.error || cats.error || hist.error || merchants.error || incStack.error || expStack.error || bal.error} />

      {([
        { key: 'inc', kind: 'income', kicker: 'Income', total: totals.inc, q: incStack, lvl: incLevel, dflt: 'category' },
        { key: 'exp', kind: 'expense', kicker: 'Expenses', total: totals.exp, q: expStack, lvl: expLevel, dflt: 'group' },
      ] as const).map((c) => (
        <Card key={c.key}>
          <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
            <div>
              <div className="card-kicker">{c.kicker}</div>
              <div className="card-title">{moneyRound(c.total)} · {moneyRound(c.total / n)}/mo by {c.lvl === 'group' ? 'group' : 'category'}</div>
            </div>
            <Seg<Level> name={`${c.key}-level`} value={c.lvl} onChange={(v) => set({ [c.key]: v === c.dflt ? null : v })}
              options={[{ value: 'group', label: 'Groups' }, { value: 'category', label: 'Categories' }]} />
          </div>
          <StackedBars months={c.q.data?.months ?? []} series={c.q.data?.series ?? []} highlight={month}
            onPick={pickMonth} onSegment={openSegment(c.q, c.kind, c.lvl)} tip={stackTip(c.q, c.kind, c.lvl)} />
          <Legend items={[
            ...(c.q.data?.series ?? []).map((s, i) => ({ label: `${s.name} ${moneyRound(s.total / n)}/mo`, color: stackColor(s.name, i) })),
            { label: '12-mo avg', color: 'var(--color-text)', dashed: true },
          ]} />
          {c.key === 'exp' && <div className="card-meta">Hover a bar or segment for its top merchants; click a segment to see its transactions, or click beside the bar to break that month down below (again to clear).</div>}
        </Card>
      ))}

      <Card>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
          <div>
            <div className="card-kicker">Net income</div>
            <div className="card-title">{netMode === 'waterfall' ? 'Running total of monthly changes' : 'Monthly net with 12-month average'}</div>
          </div>
          <div className="row">
            <Legend items={[
              { label: 'Net increase', color: NET_UP }, { label: 'Net decrease', color: NET_DOWN },
              ...(netMode === 'monthly' ? [{ label: '12-mo avg', color: 'var(--color-text)' }] : []),
            ]} />
            <Seg name="netmode" value={netMode} onChange={(v) => set({ net: v === 'waterfall' ? null : v })} options={[
              { value: 'waterfall', label: 'Waterfall' }, { value: 'monthly', label: 'Monthly' },
            ]} />
          </div>
        </div>
        <NetChart data={flow.data ?? []} mode={netMode} onPick={pickMonth}
          tip={(m, ctx) => {
            const f = flow.data?.find((x) => x.month === m)
            return { title: `${monthName(m)} · net ${money(ctx.net, true)}`,
              lines: [`Income ${money(f?.income ?? 0)} · expenses ${money(f?.expenses ?? 0)}`,
                netMode === 'waterfall' ? `Running total ${money(ctx.running, true)}` : `12-mo avg ${money(ctx.avg ?? 0, true)}`],
              contrib: { start: m, end: monthEnd(m) } }
          }} />
        {byNet.length > 0 && (
          <div className="card-meta">
            Net {moneyRound(net)} over {n} months · avg {moneyRound(net / n)}/mo · {upMonths} of {n} months positive
            · best {monthName(byNet[0].month)} ({moneyRound(byNet[0].net)})
            · worst {monthName(byNet[byNet.length - 1].month)} ({moneyRound(byNet[byNet.length - 1].net)})
          </div>
        )}
      </Card>

      {([
        { key: 'nw', kicker: 'Net worth', label: 'net worth', v: nw, perAccount: false, total: 'Net worth' },
        { key: 'cash', kicker: 'Checking + savings', label: 'checking + savings', v: cash, perAccount: true, total: 'Total' },
      ] as const).map((c) => (
        <Card key={c.key}>
          <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
            <div>
              <div className="card-kicker">{c.kicker}</div>
              <div className="card-title">
                {c.v.last >= 0
                  ? `${moneyRound(c.v.total[c.v.last] ?? 0)} · ${signedRound(change(c.v))} since ${monthName(c.v.months[c.v.first])}`
                  : bal.isPending ? 'Loading…' : 'No balances in this period'}
              </div>
            </div>
            <Legend items={[
              ...c.v.bands.map((b) => ({ label: b.name, color: b.color })),
              { label: c.total, color: 'var(--color-text)' },
              ...(c.v.estimated.some(Boolean) ? [{ label: 'Estimated', color: ESTIMATED_FILL }] : []),
            ]} />
          </div>
          <BalanceChart months={c.v.months} bands={c.v.bands} total={c.v.total} estimated={c.v.estimated}
            tip={balTip(c.v, c.label, c.perAccount)} />
          <div className="card-meta">
            Month-end balances{c.key === 'nw' ? '; debts shown below zero, homes and vehicles valued along a straight line from their purchase price until their first valuation' : ''}.
            {c.v.estimated.some(Boolean) && ' Shaded months lean on balances rebuilt from transactions, working back from the first known balance.'}
            {c.key === 'nw' && lateJoiners.length > 0 && ` Balance history starts later for ${lateJoiners.slice(0, 4)
              .map(({ a, at }) => `${a.name} (${monthName(c.v.months[at])})`).join(', ')}${lateJoiners.length > 4 ? ` and ${lateJoiners.length - 4} more` : ''}.`}
          </div>
        </Card>
      ))}

      <div className="grid-2" style={{ alignItems: 'start' }}>
        <Card style={{ gap: 'var(--space-2)' }}>
          <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
            <div>
              <div className="card-kicker">Spending by group</div>
              <div className="card-title">{month ? parseIso(month).toLocaleString('en-US', { month: 'long', year: 'numeric' }) : 'Whole range'}</div>
            </div>
            {month && <Button variant="ghost" onClick={() => pickMonth(null)}>Whole range</Button>}
          </div>
          {groupList.map(([name, g]) => (
            <div key={name} className="stack" style={{ gap: 2 }}>
              <div className="row" style={{ justifyContent: 'space-between', cursor: 'pointer', flexWrap: 'nowrap' }}
                onClick={() => setOpenGroup(openGroup === name ? null : name)}
                {...tipHandlers(tip, () => ({ title: name, lines: [money(g.spent)],
                  contrib: { ...focus, kind: 'expense', level: 'group', ids: [g.id ?? 0] } }))}>
                <span style={{ fontSize: 13 }}>
                  {openGroup === name ? '▾' : '▸'} {g.id
                    ? <Link to={groupPath(g.id)} onClick={(e) => e.stopPropagation()}>{name}</Link>
                    : name}
                </span>
                <span className="small" style={{ fontVariantNumeric: 'tabular-nums' }}>{money(g.spent)}</span>
              </div>
              <div className="progress"><div style={{ width: `${(g.spent / maxGroup) * 100}%` }} /></div>
              {openGroup === name && (
                <div className="stack small" style={{ paddingLeft: 14, marginTop: 4 }}>
                  {g.cats.map((c) => (
                    <div key={c.category} className="row" style={{ justifyContent: 'space-between' }}
                      {...tipHandlers(tip, () => ({ title: c.category, lines: [money(c.spent)],
                        contrib: { ...focus, kind: 'expense', level: 'category', ids: [c.category_id ?? 0] } }))}>
                      <Link to={c.category_id ? categoryPath(c.category_id) : '/transactions'}>{c.category}</Link>
                      <span>{money(c.spent)} · {c.count}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
          {groupList.length === 0 && <div className="small text-muted">No spending in this period.</div>}
        </Card>
        <Card style={{ gap: 'var(--space-2)' }}>
          <div className="card-kicker">Top merchants</div>
          <table className="table" style={{ fontSize: 13 }}>
            <thead><tr><SortTh s={ms} k="merchant">Merchant</SortTh><SortTh s={ms} k="count" right>Count</SortTh>
              <SortTh s={ms} k="spent" right>Spent</SortTh></tr></thead>
            <tbody>
              {sortRows(merchants.data ?? [], ms, { merchant: (m) => m.example, count: (m) => m.count, spent: (m) => m.spent }).map((m) => (
                <tr key={m.merchant} {...tipHandlers(tip, () => ({ title: m.example, lines: [`${money(m.spent)} · ${m.count} transactions`],
                  contrib: { ...focus, merchant: m.merchant }, show: 'transactions' }))}>
                  <td><Link to={merchantPath(m.merchant)}>{m.example}</Link></td>
                  <td className="num">{m.count}</td><td className="num">{money(m.spent)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>

      <Card>
        <div className="row" style={{ justifyContent: 'space-between', alignItems: 'baseline' }}>
          <div>
            <div className="card-kicker">Monthly history</div>
            <div className="card-title">{histKind === 'expense' ? 'Expenses' : 'Income'} by {histLevel === 'group' ? 'group' : 'category'}</div>
          </div>
          <div className="row">
            <Seg name="hist-kind" value={histKind} onChange={(v) => set({ hk: v === 'expense' ? null : v })}
              options={[{ value: 'expense', label: 'Expenses' }, { value: 'income', label: 'Income' }]} />
            <Seg name="hist-level" value={histLevel} onChange={(v) => set({ hl: v === 'group' ? null : v })}
              options={[{ value: 'group', label: 'Groups' }, { value: 'category', label: 'Categories' }]} />
          </div>
        </div>
        <SeriesHistory months={hist.data?.months ?? []} series={histSeries} highlight={month}
          onPick={pickMonth}
          label={(si, avg) => {
            const s = histSeries[si]
            const to = s.id ? (histLevel === 'group' ? groupPath(s.id) : categoryPath(s.id)) : null
            return (
              <>
                {to ? <Link to={to}>{s.name}</Link> : <span>{s.name}</span>}
                <span className="small text-muted">avg {moneyRound(avg)}/mo</span>
              </>
            )
          }}
          tip={(i, si, avg) => {
            const s = histSeries[si]
            const m = hist.data?.months[i] ?? ''
            return { title: `${s.name} · ${monthName(m)}`, lines: [`${money(s.values[i])} · avg ${money(avg)}/mo`],
              contrib: { start: m, end: monthEnd(m), kind: histKind, level: histLevel, ids: [sid(s)] } }
          }} />
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <div className="card-meta">Each row has its own scale; the dashed line is the average for the range. Hover a bar for its top merchants.</div>
          {(hist.data?.series.length ?? 0) > 10 && (
            <Button variant="ghost" onClick={() => set({ hall: !histAll })}>
              {histAll ? 'Show top 10' : `Show all ${hist.data?.series.length}`}
            </Button>
          )}
        </div>
      </Card>
    </section>
  )
}
