// System-admin allowlist — frontend mirror of
// backend/app/utils/system_admins.py (the API guard is the real
// enforcement; this only controls menu/page visibility).
// Keep the two lists in sync when membership changes.

export const SYSTEM_ADMIN_EMAILS = [
  'itsupport@vivofashiongroup.com',
  'peter@vivofashiongroup.com',
  'stephen@vivofashiongroup.com',
] as const

export function isSystemAdmin(email: string | null | undefined): boolean {
  if (!email) return false
  return (SYSTEM_ADMIN_EMAILS as readonly string[])
    .includes(email.trim().toLowerCase())
}
