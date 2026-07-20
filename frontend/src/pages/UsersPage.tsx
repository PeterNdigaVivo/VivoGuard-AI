// User management — admin only. Add users, change roles, disable
// accounts, reset passwords. Roles: admin / operator / viewer.

import { useEffect, useState } from 'react'
import {
  Badge, Button, Card, Input, PageHeader, Select, Skeleton, useToast,
} from '@/components/ui/Primitives'
import { api } from '@/api/client'
import { useAuth } from '@/auth/AuthContext'

interface UserRow {
  id: number
  email: string
  full_name: string | null
  role: 'admin' | 'operator' | 'viewer'
  is_active: boolean
  created_at: string
  last_login_at: string | null
}

const ROLE_COLORS: Record<UserRow['role'], 'sky' | 'amber' | 'slate'> = {
  admin: 'sky', operator: 'amber', viewer: 'slate',
}

export default function UsersPage() {
  const { user: me } = useAuth()
  const toast = useToast()
  const [rows, setRows] = useState<UserRow[] | null>(null)
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState({
    email: '', password: '', full_name: '',
    role: 'viewer' as UserRow['role'], is_active: true,
  })
  const [error, setError] = useState<string | null>(null)
  const [resetFor, setResetFor] = useState<UserRow | null>(null)
  const [resetPw, setResetPw] = useState('')

  const reload = () => api<UserRow[]>('/users').then(setRows).catch(e => setError(String(e)))
  useEffect(() => { reload() }, [])

  if (me?.role !== 'admin') {
    return (
      <div className="p-6">
        <PageHeader title="Users" />
        <Card className="p-8 text-center text-slate-500 dark:text-slate-300 dark:bg-slate-900 dark:border-slate-800">
          Only admins can manage users. Ask your administrator if you need access.
        </Card>
      </div>
    )
  }

  async function save() {
    setError(null)
    if (!form.email || !form.password) {
      setError('Email and password are required'); return
    }
    try {
      await api<UserRow>('/users', { method: 'POST', body: form })
      toast.push(`Added ${form.email}`)
      setAdding(false)
      setForm({ email: '', password: '', full_name: '', role: 'viewer', is_active: true })
      reload()
    } catch (e) { setError(String(e)) }
  }

  async function setRole(u: UserRow, role: UserRow['role']) {
    try {
      await api(`/users/${u.id}`, { method: 'PATCH', body: { role } })
      toast.push(`${u.email}: role → ${role}`)
      reload()
    } catch (e) { toast.push(String(e), 'err') }
  }

  async function toggleActive(u: UserRow) {
    try {
      await api(`/users/${u.id}`, { method: 'PATCH', body: { is_active: !u.is_active } })
      toast.push(`${u.email} ${u.is_active ? 'disabled' : 'enabled'}`)
      reload()
    } catch (e) { toast.push(String(e), 'err') }
  }

  async function remove(u: UserRow) {
    if (!confirm(`Delete user ${u.email}? This cannot be undone.`)) return
    try {
      await api(`/users/${u.id}`, { method: 'DELETE' })
      toast.push(`${u.email} deleted`)
      reload()
    } catch (e) { toast.push(String(e), 'err') }
  }

  async function resetPassword() {
    if (!resetFor || !resetPw) return
    try {
      await api(`/users/${resetFor.id}`, { method: 'PATCH', body: { password: resetPw } })
      toast.push(`Password reset for ${resetFor.email}`)
      setResetFor(null); setResetPw('')
    } catch (e) { toast.push(String(e), 'err') }
  }

  if (rows === null) return (
    <div className="p-6"><PageHeader title="Users" /><Skeleton className="h-64" /></div>
  )

  return (
    <div className="p-6">
      <PageHeader title="Users" actions={
        <Button onClick={() => setAdding(true)}>+ Add user</Button>
      } />

      {error && <div className="text-red-600 mb-2">{error}</div>}

      {adding && (
        <Card className="p-4 mb-4 dark:bg-slate-900 dark:border-slate-800">
          <div className="font-medium mb-3">New user</div>
          <div className="grid grid-cols-1 md:grid-cols-5 gap-2 items-end">
            <Input placeholder="email@vivofashiongroup.com"
                   value={form.email} onChange={e => setForm({ ...form, email: e.target.value })} />
            <Input type="password" placeholder="initial password"
                   value={form.password} onChange={e => setForm({ ...form, password: e.target.value })} />
            <Input placeholder="Full name (optional)"
                   value={form.full_name} onChange={e => setForm({ ...form, full_name: e.target.value })} />
            <Select value={form.role} onChange={e => setForm({ ...form, role: e.target.value as UserRow['role'] })}>
              <option value="viewer">viewer</option>
              <option value="operator">operator</option>
              <option value="admin">admin</option>
            </Select>
            <div className="flex gap-2">
              <Button onClick={save}>Create</Button>
              <Button variant="ghost" onClick={() => setAdding(false)}>Cancel</Button>
            </div>
          </div>
          <div className="text-xs text-slate-500 dark:text-slate-300 mt-2">
            <strong>viewer</strong>: read-only dashboards. <strong>operator</strong>:
            can add cameras, confirm/dismiss alerts. <strong>admin</strong>:
            full access including user management.
          </div>
        </Card>
      )}

      <Card className="dark:bg-slate-900 dark:border-slate-800">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600 dark:bg-slate-800 dark:text-slate-200">
            <tr>
              <th className="text-left p-3">Email</th>
              <th className="text-left p-3">Name</th>
              <th className="text-left p-3">Role</th>
              <th className="text-left p-3">Status</th>
              <th className="text-left p-3">Last login</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map(u => (
              <tr key={u.id} className="border-t dark:border-slate-800 hover:bg-slate-50 dark:hover:bg-slate-800/60">
                <td className="p-3 font-medium">{u.email}{u.id === me?.id && <span className="ml-2 text-xs text-slate-400 dark:text-slate-400">(you)</span>}</td>
                <td className="p-3">{u.full_name ?? '—'}</td>
                <td className="p-3">
                  <Select value={u.role}
                          disabled={u.id === me?.id}
                          onChange={e => setRole(u, e.target.value as UserRow['role'])}>
                    <option value="viewer">viewer</option>
                    <option value="operator">operator</option>
                    <option value="admin">admin</option>
                  </Select>
                </td>
                <td className="p-3">
                  {u.is_active
                    ? <Badge color="green">active</Badge>
                    : <Badge color="slate">disabled</Badge>}
                </td>
                <td className="p-3 text-xs text-slate-500 dark:text-slate-300">
                  {u.last_login_at ? new Date(u.last_login_at).toLocaleString() : 'never'}
                </td>
                <td className="p-3 text-right whitespace-nowrap space-x-2">
                  <button onClick={() => setResetFor(u)} className="text-sky-600 hover:underline text-xs">
                    Reset password
                  </button>
                  {u.id !== me?.id && (
                    <>
                      <button onClick={() => toggleActive(u)} className="text-amber-600 hover:underline text-xs">
                        {u.is_active ? 'Disable' : 'Enable'}
                      </button>
                      <button onClick={() => remove(u)} className="text-red-600 hover:underline text-xs">
                        Delete
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      {resetFor && (
        <Card className="p-4 mt-4 max-w-md dark:bg-slate-900 dark:border-slate-800">
          <div className="font-medium mb-2">Reset password for {resetFor.email}</div>
          <Input type="password" placeholder="new password"
                 value={resetPw} onChange={e => setResetPw(e.target.value)} />
          <div className="mt-3 flex gap-2 justify-end">
            <Button variant="ghost" onClick={() => { setResetFor(null); setResetPw('') }}>Cancel</Button>
            <Button onClick={resetPassword} disabled={!resetPw}>Reset</Button>
          </div>
        </Card>
      )}
    </div>
  )
}
