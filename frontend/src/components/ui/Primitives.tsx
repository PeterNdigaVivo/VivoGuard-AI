// Hand-rolled UI primitives so we don't pull a UI library.
// Each component is intentionally small — read it like CSS classes.

import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes } from 'react'

export function Button(p: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'ghost' | 'danger' }) {
  const { variant = 'primary', className = '', ...rest } = p
  const styles = {
    primary: 'bg-sky-600 hover:bg-sky-500 text-white',
    ghost:   'bg-slate-200 hover:bg-slate-300 text-slate-800',
    danger:  'bg-red-600 hover:bg-red-500 text-white',
  }[variant]
  return (
    <button {...rest}
      className={`px-3 py-1.5 rounded text-sm font-medium disabled:opacity-50 ${styles} ${className}`} />
  )
}

export function Input(p: InputHTMLAttributes<HTMLInputElement>) {
  const { className = '', ...rest } = p
  return (
    <input {...rest}
      className={`bg-white border border-slate-300 rounded px-2 py-1 text-sm
                  focus:ring-2 ring-sky-500 outline-none ${className}`} />
  )
}

export function Select(p: SelectHTMLAttributes<HTMLSelectElement>) {
  const { className = '', ...rest } = p
  return (
    <select {...rest}
      className={`bg-white border border-slate-300 rounded px-2 py-1 text-sm ${className}`} />
  )
}

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`bg-white rounded-lg shadow-sm border border-slate-200 ${className}`}>{children}</div>
}

export function Badge({ children, color = 'slate' }: { children: ReactNode; color?: 'green' | 'red' | 'amber' | 'slate' | 'sky' }) {
  const colors = {
    green: 'bg-green-100 text-green-700',
    red:   'bg-red-100 text-red-700',
    amber: 'bg-amber-100 text-amber-700',
    sky:   'bg-sky-100 text-sky-700',
    slate: 'bg-slate-100 text-slate-700',
  }
  return <span className={`text-xs px-2 py-0.5 rounded-full ${colors[color]}`}>{children}</span>
}

export function PageHeader({ title, actions }: { title: string; actions?: ReactNode }) {
  return (
    <div className="flex items-center justify-between mb-6">
      <h1 className="text-2xl font-semibold text-slate-800">{title}</h1>
      <div className="flex gap-2">{actions}</div>
    </div>
  )
}

// Skeleton block — used while data is loading (Rule 9 of the overhaul).
export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`animate-pulse bg-slate-200 rounded ${className}`} />
}

// Traffic-light status pill for store dashboards (Rule 4).
export function StatusLight({ value }: { value: 'green' | 'amber' | 'red' }) {
  const map = {
    green: { color: 'bg-emerald-500', label: 'Normal' },
    amber: { color: 'bg-amber-500',   label: 'Attention' },
    red:   { color: 'bg-red-500',     label: 'Alert' },
  } as const
  const s = map[value] ?? map.green
  return (
    <span className="inline-flex items-center gap-2 text-sm">
      <span className={`inline-block w-2.5 h-2.5 rounded-full ${s.color}`} />
      {s.label}
    </span>
  )
}

// Trend arrow with delta percentage. direction: 'up' | 'down' | 'flat'.
export function Trend({ direction, deltaPct }:
  { direction: 'up' | 'down' | 'flat'; deltaPct: number | null }) {
  if (deltaPct === null) {
    return <span className="text-xs text-slate-400">no comparison</span>
  }
  const symbol = direction === 'up' ? '↑' : direction === 'down' ? '↓' : '→'
  const color  = direction === 'up' ? 'text-emerald-600'
               : direction === 'down' ? 'text-red-600' : 'text-slate-500'
  return (
    <span className={`text-xs ${color}`}>{symbol} {Math.abs(deltaPct).toFixed(1)}% vs yesterday</span>
  )
}
