export interface DiscoveryEvent {
  id: number
  title: string
  url: string | null
  day: string
  hour: number
  time: string
  period: string
  startAt: string
  score: number | null
  fitIcon: string
  format: 'in-person' | 'online' | 'mixed' | null
  place: string
  highlight: string | null
  urgencyNote: string | null
  urgencyNoteExpiresAt: string | null
  food: string | null
  registrationAvailability: 'unknown' | 'open' | 'waitlist' | 'full' | 'closed' | 'canceled'
  timeWarning?: string | null
}

export interface DiscoveryCatalog {
  today: string
  timezone: string
  events: DiscoveryEvent[]
}

export async function fetchDiscovery(publicPreview = false): Promise<DiscoveryCatalog> {
  const response = await fetch(publicPreview ? '/api/preview' : '/api/discovery', { credentials: 'same-origin', cache: 'no-store' })
  if (response.redirected) {
    // Re-enter through the public preview after logout, expiry or revocation.
    location.assign('/events')
    throw new Error('Please sign in again.')
  }
  if (!response.ok) throw new Error('Events are temporarily unavailable.')
  return response.json()
}
