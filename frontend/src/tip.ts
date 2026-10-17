import { createContext, useContext, type MouseEvent } from 'react'
import type { BudgetRow, BudgetStatus, ContribParams } from './api'
import { money } from './format'

/** What a chart shows when hovering an aggregate; `contrib` loads the transactions behind it. */
export interface TipSpec {
  title: string
  lines?: string[]
  contrib?: ContribParams
  show?: 'merchants' | 'transactions'
}

export interface TipApi {
  show: (spec: TipSpec, e: { clientX: number; clientY: number }) => void
  hide: () => void
}

export const TipContext = createContext<TipApi>({ show: () => {}, hide: () => {} })
export const useTip = () => useContext(TipContext)

/** Hover handlers for an SVG/HTML element; `make` runs lazily so charts don't build specs for every bar on render. */
export function tipHandlers(tip: TipApi, make: () => TipSpec | null) {
  return {
    onMouseMove: (e: MouseEvent) => { const s = make(); if (s) tip.show(s, e); else tip.hide() },
    onMouseLeave: () => tip.hide(),
  }
}

/** Hover summary for a budget bar; the transactions are those in the budget's categories during the period. */
export function budgetTip(b: { name: string; spent: number | string; budget: number | string; pct: number; status: BudgetRow['status'];
  projected?: number | string; spread?: number | string; categoryIds: number[] }, period: BudgetStatus['period'], elapsed = period.elapsed): TipSpec {
  const left = Number(b.budget) - Number(b.spent)
  return {
    title: `${b.name} · ${period.label}`,
    lines: [
      `${money(b.spent)} of ${money(b.budget)} · ${Math.round(b.pct)}%`,
      left < 0 ? `Over by ${money(-left)}` : `${money(left)} left`,
      ...(b.status === 'pace' && b.projected != null ? [`On pace for ${money(b.projected)}`] : []),
      ...(Number(b.spread) ? [`Incl. ${money(b.spread)} spread from other months`] : []),
      ...(elapsed > 0 && elapsed < 1 ? [`${Math.round(elapsed * 100)}% through the period`] : []),
    ],
    contrib: b.categoryIds.length ? { start: period.start, end: period.end, basis: 'budget', level: 'category', ids: b.categoryIds } : undefined,
  }
}
