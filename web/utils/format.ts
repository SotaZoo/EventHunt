export function formatStartsAt(iso: string | null): string {
  if (!iso) return 'TBD'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString()
}
