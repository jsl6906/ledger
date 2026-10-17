import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useId, useState, type KeyboardEvent } from 'react'
import { post, type Tag } from '../api'
import { useTags } from '../hooks'
import { ErrorNote } from './ui'

const MAX_OPTIONS = 8

/** Selected tags as removable chips plus a type-ahead to add existing tags or create new ones. */
export function TagPicker({ value, onChange }: { value: number[]; onChange: (ids: number[]) => void }) {
  const qc = useQueryClient()
  const tags = useTags()
  const listId = useId()
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)

  const all = tags.data ?? []
  const byId = new Map(all.map((t) => [t.id, t]))
  const selected = value.map((id) => byId.get(id)).filter((t): t is Tag => !!t)
  const needle = q.trim().toLowerCase()
  const matches = all.filter((t) => !value.includes(t.id) && t.name.toLowerCase().includes(needle))
  const shown = matches.slice(0, MAX_OPTIONS)
  const canCreate = needle !== '' && !all.some((t) => t.name.toLowerCase() === needle)
  const options: ({ kind: 'tag'; tag: Tag } | { kind: 'create' })[] = [
    ...shown.map((tag) => ({ kind: 'tag' as const, tag })),
    ...(canCreate ? [{ kind: 'create' as const }] : []),
  ]
  const idx = Math.min(active, Math.max(options.length - 1, 0))

  const create = useMutation({
    mutationFn: (name: string) => post<Tag>('/tags', { name, color: null }),
    onSuccess: (t) => {
      qc.setQueryData<Tag[]>(['tags'], (old) => [...(old ?? []), t].sort((a, b) => a.name.localeCompare(b.name)))
      qc.invalidateQueries({ queryKey: ['tags'] })
      add(t.id)
    },
  })

  const add = (id: number) => {
    if (!value.includes(id)) onChange([...value, id])
    setQ('')
    setActive(0)
  }
  const remove = (id: number) => onChange(value.filter((x) => x !== id))
  const choose = (i: number) => {
    const o = options[i]
    if (!o) return
    if (o.kind === 'tag') add(o.tag.id)
    else if (!create.isPending) create.mutate(q.trim())
  }

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setOpen(true); setActive((idx + 1) % Math.max(options.length, 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setOpen(true); setActive((idx - 1 + options.length) % Math.max(options.length, 1)) }
    else if (e.key === 'Enter') { if (open && options.length) { e.preventDefault(); choose(idx) } }
    else if (e.key === 'Escape') { if (open) { e.preventDefault(); setOpen(false) } }
    else if (e.key === 'Backspace' && q === '' && value.length) remove(value[value.length - 1])
  }

  const showList = open && (options.length > 0 || needle !== '')

  return (
    <div className="tag-picker">
      {selected.length > 0 && (
        <div className="row" style={{ gap: 6 }}>
          {selected.map((t) => (
            <button key={t.id} type="button" className="tag chip on tag-picker-chip" title={`Remove ${t.name}`}
              onClick={() => remove(t.id)}>
              {t.name}<span aria-hidden>×</span>
            </button>
          ))}
        </div>
      )}
      <input className="input" role="combobox" aria-expanded={showList} aria-controls={listId} aria-autocomplete="list"
        aria-activedescendant={showList && options.length ? `${listId}-${idx}` : undefined}
        placeholder={selected.length ? 'Add another tag…' : 'Add a tag…'} value={q}
        onChange={(e) => { setQ(e.target.value); setOpen(true); setActive(0) }}
        onFocus={() => setOpen(true)} onBlur={() => setOpen(false)} onKeyDown={onKey} />
      {showList && (
        <div id={listId} role="listbox" className="tag-picker-list">
          {options.map((o, i) => (
            <div key={o.kind === 'tag' ? o.tag.id : 'create'} id={`${listId}-${i}`} role="option" aria-selected={i === idx}
              className={`tag-picker-opt${i === idx ? ' active' : ''}`}
              onMouseDown={(e) => e.preventDefault()} onMouseEnter={() => setActive(i)} onClick={() => choose(i)}>
              {o.kind === 'tag' ? o.tag.name : <>{create.isPending ? 'Creating' : 'Create tag'} “{q.trim()}”</>}
            </div>
          ))}
          {matches.length > shown.length && (
            <div className="tag-picker-more small muted-2">{matches.length - shown.length} more — keep typing to narrow</div>
          )}
          {options.length === 0 && <div className="tag-picker-more small muted-2">Already tagged</div>}
        </div>
      )}
      <ErrorNote error={create.error} />
    </div>
  )
}
