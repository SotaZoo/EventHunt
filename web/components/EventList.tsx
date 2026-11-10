import type { EventSummary } from '@/types/events'

export function EventList({ events = [] }: { events?: EventSummary[] }) {
  if (events.length === 0) {
    return <p>No events to show yet.</p>
  }
  return (
    <ul>
      {events.map((event) => (
        <li key={event.id}>
          <strong>{event.title}</strong>
          {event.startsAt ? <span> — {event.startsAt}</span> : null}
        </li>
      ))}
    </ul>
  )
}
