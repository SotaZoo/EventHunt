import { defineCustomElement as button } from '@ionic/core/components/ion-button.js'
import { initialize } from '@ionic/core/components'
import { defineCustomElement as label } from '@ionic/core/components/ion-label.js'
import { defineCustomElement as range } from '@ionic/core/components/ion-range.js'
import { defineCustomElement as segment } from '@ionic/core/components/ion-segment.js'
import { defineCustomElement as segmentButton } from '@ionic/core/components/ion-segment-button.js'
import { fetchDiscovery, type DiscoveryEvent } from '../service/discovery'
import { Calendar, formatDay } from './calendar'
import { attachInvitationForm } from './invitation'
import './styles.css'

initialize({ mode: 'ios' })
button(); label(); range(); segment(); segmentButton()

type Filters = { format: 'all' | 'in-person' | 'online'; score: number; start: number; end: number }
type ScrollAnchor = { event: string; offset: number }
type BrowseState = { version: 1; filters: Filters; selected: string; folded: boolean; scroll: number; anchor?: ScrollAnchor }
const defaults: Filters = { format: 'all', score: 0, start: 0, end: 24 }
const byId = <T extends HTMLElement>(id: string) => document.getElementById(id) as T
const agenda = byId<HTMLDivElement>('agenda')
const publicPreview = byId('app').dataset.publicPreview === 'true'
const accessPanel = publicPreview ? byId('earlyAccess') : null
if (accessPanel?.querySelector('#inviteForm')) {
  attachInvitationForm(accessPanel)
}
function showAccess(): void {
  const target = accessPanel || byId('signInError')
  target?.scrollIntoView({ behavior: motion(), block: 'start' })
  const focusTarget = target?.querySelector<HTMLElement>('#accessTitle') || target
  focusTarget?.focus({ preventScroll: true })
}
const sheet = byId<HTMLDialogElement>('filters')
const openFilters = byId<HTMLButtonElement>('openFilters')
const scoreRange = byId<HTMLIonRangeElement>('score')
const timeRange = byId<HTMLIonRangeElement>('timeRange')
const formatSegment = byId<HTMLIonSegmentElement>('format')
const applyButton = byId<HTMLIonButtonElement>('applyFilters')
const browseKey = 'eventHuntDiscovery'
function previousBrowse(): BrowseState | undefined {
  if (publicPreview) { try { sessionStorage.removeItem(browseKey) } catch { /* Storage may be disabled. */ } return }
  if (history.state?.eventHuntDiscovery) return history.state.eventHuntDiscovery
  try { return JSON.parse(sessionStorage.getItem(browseKey) || 'null') || undefined }
  catch { return undefined }
}
const previous = previousBrowse()
const restored = !publicPreview && previous?.version === 1 && /^\d{4}-\d{2}-\d{2}$/.test(previous.selected) ? previous : undefined
let filters = { ...defaults, ...restored?.filters }
let draft = { ...filters }
let events: DiscoveryEvent[] = []
let calendar: Calendar | undefined
let suppressFollow = 0
let loading = false
let catalogReady = false
let followFrame = 0
let urgencyTimer = 0
let today = ''
let scrollAnchor: ScrollAnchor | undefined = restored?.anchor
let agendaWidth = agenda.clientWidth
const motion = () => matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'

function save(): void {
  if (!calendar) return
  captureAnchor()
  const state: BrowseState = { version: 1, filters, selected: calendar.selected, folded: calendar.folded, scroll: agenda.scrollTop, anchor: scrollAnchor }
  // Only navigation state, never catalog data, user identity or credentials.
  history.replaceState({ ...history.state, eventHuntDiscovery: state }, '')
  try { sessionStorage.setItem(browseKey, JSON.stringify(state)) } catch { /* History still restores Back when storage is unavailable. */ }
}

function captureAnchor(): void {
  const top = agenda.getBoundingClientRect().top
  const visible = [...agenda.querySelectorAll<HTMLElement>('.event-card')].find(card => card.getBoundingClientRect().bottom > top)
  scrollAnchor = visible ? { event: visible.dataset.eventId!, offset: visible.getBoundingClientRect().top - top } : undefined
}

function restoreAnchor(): void {
  if (!scrollAnchor) return
  const visible = [...agenda.querySelectorAll<HTMLElement>('.event-card')].find(card => card.dataset.eventId === scrollAnchor!.event)
  if (visible) agenda.scrollTop += visible.getBoundingClientRect().top - agenda.getBoundingClientRect().top - scrollAnchor.offset
}

if (!publicPreview) new ResizeObserver(() => {
  if (agendaWidth === agenda.clientWidth) return
  agendaWidth = agenda.clientWidth
  suppressFollow = performance.now() + 150
  restoreAnchor()
}).observe(agenda)

function matches(event: DiscoveryEvent, choice = filters): boolean {
  return event.day >= today && (choice.format === 'all' || event.format === choice.format || event.format === 'mixed') &&
    event.hour >= choice.start && event.hour <= choice.end &&
    (choice.score === 0 || (event.score !== null && event.score >= choice.score))
}

// Official Lucide 0.468.0 paths: map-pin, monitor, utensils and clock-3.
// The build embeds the notices in icons/LICENSE in the generated JavaScript.
const paths = {
  pin: '<path d="M20 10c0 4.993-5.539 10.193-7.399 11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0"/><circle cx="12" cy="10" r="3"/>',
  online: '<rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/>',
  food: '<path d="M3 2v7c0 1.1.9 2 2 2h4a2 2 0 0 0 2-2V2"/><path d="M7 2v20"/><path d="M21 15V2a5 5 0 0 0-5 5v6c0 1.1.9 2 2 2h3Zm0 0v7"/>',
  clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16.5 12"/>',
}

function textElement(tag: string, className: string, value: string): HTMLElement {
  const element = document.createElement(tag)
  element.className = className
  element.textContent = value
  return element
}

function fact(className: string, icon: keyof typeof paths, text: string): HTMLElement {
  const line = textElement('p', className, '')
  // Only constant icon markup enters innerHTML. Source/model text uses textContent.
  line.innerHTML = `<svg class="fact-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[icon]}</svg>`
  line.append(textElement('span', '', text))
  return line
}

function card(event: DiscoveryEvent): HTMLElement {
  const element = document.createElement(event.url ? 'a' : 'article')
  element.className = 'event-card'
  element.dataset.eventId = String(event.id)
  const descriptions: string[] = []
  if (element instanceof HTMLAnchorElement && event.url) {
    element.href = event.url
    element.rel = 'noopener noreferrer'
    element.setAttribute('aria-label', `${event.title} — open source`)
    element.onclick = save
  }
  const timeColumn = textElement('div', 'time-column', '')
  const time = document.createElement('time')
  time.dateTime = event.startAt
  time.textContent = event.time
  time.append(textElement('small', '', event.period))
  timeColumn.append(time)
  const body = document.createElement('div')
  body.append(textElement('h3', 'event-title', event.title))
  if (event.timeWarning) {
    const warning = fact('time-warning', 'clock', event.timeWarning)
    warning.id = `time-warning-${event.id}`
    descriptions.push(warning.id)
    body.append(warning)
  }
  if (event.place) body.append(fact('place-line', event.format === 'online' ? 'online' : 'pin', event.place))
  if (event.food) body.append(fact('food-line', 'food', event.food))
  const fit = textElement('p', 'fit-value', '')
  fit.title = 'Shared internal fit score; not personalized'
  const score = textElement('span', 'fit-score', '')
  if (event.score !== null && event.score >= .90) score.classList.add('excellent-fit')
  if (event.fitIcon) {
    const icon = textElement('span', 'fit-icon', event.fitIcon)
    icon.setAttribute('aria-hidden', 'true')
    score.append(icon)
  }
  score.append(textElement('span', '', `Fit ${event.score === null ? '—' : event.score.toFixed(2)}`))
  fit.append(score)
  if (event.highlight) {
    fit.append(document.createTextNode(' '), textElement('span', 'card-highlight', event.highlight))
  }
  if (event.registrationAvailability === 'waitlist') {
    const label = textElement('span', 'availability-label', 'Waitlist')
    label.id = `availability-${event.id}`
    descriptions.push(label.id)
    fit.append(label)
  }
  body.append(fit)
  const urgencyExpiry = Date.parse(event.urgencyNoteExpiresAt || '')
  if (event.urgencyNote && urgencyExpiry > Date.now()) {
    const note = fact('urgency-note', 'clock', event.urgencyNote)
    note.dataset.expiresAt = String(urgencyExpiry)
    body.append(note)
  }
  element.append(timeColumn, body)
  if (descriptions.length) element.setAttribute('aria-describedby', descriptions.join(' '))
  return element
}

function renderAgenda(): void {
  if (!catalogReady) return
  const visible = events.filter(event => matches(event))
  agenda.replaceChildren()
  const days = [...new Set([...visible.map(event => event.day), ...(publicPreview ? [] : [calendar!.selected])])].sort()
  const grouped = new Map<string, DiscoveryEvent[]>()
  for (const event of visible) grouped.set(event.day, [...(grouped.get(event.day) || []), event])
  for (const day of days) {
    const section = textElement('section', 'day-section', '')
    section.dataset.day = day
    const heading = textElement('h2', '', `${formatDay(day, { weekday: 'long' })}, `)
    heading.append(textElement('span', '', formatDay(day, { month: 'short', day: 'numeric' })))
    section.append(heading)
    const items = grouped.get(day) || []
    if (!items.length) {
      section.append(textElement('p', 'no-day', visible.length ? 'No matching events for this date.' : 'No matching upcoming events.'))
      if (!visible.length) {
        const adjust = document.createElement('ion-button')
        adjust.mode = 'ios'
        adjust.textContent = 'Adjust filters'
        adjust.onclick = () => openFilters.click()
        section.append(adjust)
      }
    }
    for (const event of items) section.append(card(event))
    agenda.append(section)
  }
  if (publicPreview && !visible.length) agenda.append(textElement('p', 'status', 'No upcoming events.'))
  calendar?.update(today, new Set(visible.map(event => event.day)))
  refreshUrgency()
}

function refreshUrgency(): void {
  clearTimeout(urgencyTimer)
  const now = Date.now()
  let next = Infinity
  for (const note of agenda.querySelectorAll<HTMLElement>('.urgency-note')) {
    const expires = Number(note.dataset.expiresAt)
    if (!Number.isFinite(expires) || expires <= now) note.remove()
    else next = Math.min(next, expires)
  }
  // Browser timers cap at a signed 32-bit delay; normal notes expire within a day.
  if (next !== Infinity) urgencyTimer = window.setTimeout(refreshUrgency, Math.min(next - now, 2 ** 31 - 1))
}

function selectDay(day: string, animate = true): void {
  if (!calendar || !catalogReady) return
  if (publicPreview && !events.some(event => event.day === day)) { showAccess(); return }
  suppressFollow = performance.now() + (animate ? 750 : 80)
  calendar.follow(day)
  let section = agenda.querySelector<HTMLElement>(`[data-day="${day}"]`)
  if (!section) { renderAgenda(); section = agenda.querySelector(`[data-day="${day}"]`) }
  if (section) agenda.scrollTo({ top: agenda.scrollTop + section.getBoundingClientRect().top - agenda.getBoundingClientRect().top - 18, behavior: animate ? motion() : 'instant' })
  save()
}

function timeLabel(hour: number): string {
  if (hour === 24) return '12am (end of day)'
  const whole = Math.floor(hour)
  return `${whole % 12 || 12}${hour % 1 ? ':30' : ''}${whole < 12 ? 'am' : 'pm'}`
}

function preview(): void {
  const count = events.filter(event => matches(event, draft)).length
  byId('scoreValue').textContent = draft.score ? `${draft.score.toFixed(2)}+` : 'Any score'
  byId('timeValue').textContent = draft.start === 0 && draft.end === 24 ? 'All day' : `${timeLabel(draft.start)}–${timeLabel(draft.end)}`
  applyButton.textContent = `Show ${count} event${count === 1 ? '' : 's'}`
}

function setDraft(value: Filters): void {
  draft = { ...value }
  formatSegment.value = draft.format
  scoreRange.value = draft.score
  timeRange.value = { lower: draft.start, upper: draft.end }
  preview()
}

function filterState(): void {
  const active = Object.keys(defaults).some(key => filters[key as keyof Filters] !== defaults[key as keyof Filters])
  openFilters.classList.toggle('active', active)
  openFilters.setAttribute('aria-label', active ? 'Open filters, filters active' : 'Open filters')
}

if (!publicPreview) {
openFilters.onclick = () => {
  setDraft(filters); sheet.showModal(); byId('closeFilters').focus()
}
byId('closeFilters').onclick = () => sheet.close()
sheet.addEventListener('close', () => openFilters.focus())
sheet.addEventListener('click', event => {
  if (event.target !== sheet) return
  const box = sheet.getBoundingClientRect()
  if (event.clientY < box.top || event.clientX < box.left || event.clientX > box.right || event.clientY > box.bottom) sheet.close()
})
formatSegment.addEventListener('ionChange', event => {
  draft.format = (event as CustomEvent).detail.value
  preview()
})
scoreRange.addEventListener('ionInput', event => {
  draft.score = Number((event as CustomEvent).detail.value)
  preview()
})
timeRange.addEventListener('ionInput', event => {
  let { lower, upper } = (event as CustomEvent).detail.value
  if (upper - lower < 0.5) {
    if (lower !== draft.start) lower = Math.max(0, upper - 0.5)
    else upper = Math.min(24, lower + 0.5)
    timeRange.value = { lower, upper }
  }
  draft.start = lower
  draft.end = upper
  preview()
})
byId('resetDraft').onclick = () => setDraft(defaults)
applyButton.onclick = () => {
  if (!catalogReady) return
  filters = { ...draft }
  renderAgenda()
  selectDay(calendar!.selected, false)
  filterState()
  sheet.close()
  save()
}
byId('today').onclick = () => selectDay(today)
}

// Ionic exposes one label for both range thumbs. Name the public shadow parts
// separately and keep their accessible values in sync after Ionic renders.
function nameTimeHandles(): void {
  for (const [part, name, value] of [
    ['knob-handle-lower', 'Earliest event start', draft.start],
    ['knob-handle-upper', 'Latest event start', draft.end],
  ] as const) {
    const handle = timeRange.shadowRoot?.querySelector(`[part~="${part}"]`)
    handle?.setAttribute('aria-label', name)
    handle?.setAttribute('aria-valuetext', timeLabel(value))
  }
}
requestAnimationFrame(() => {
  if (timeRange?.shadowRoot) new MutationObserver(nameTimeHandles).observe(timeRange.shadowRoot, { subtree: true, childList: true, attributes: true, attributeFilter: ['aria-valuenow'] })
  if (timeRange) nameTimeHandles()
})

agenda.addEventListener('scroll', () => {
  cancelAnimationFrame(followFrame)
  followFrame = requestAnimationFrame(() => {
    if (!calendar || agendaWidth !== agenda.clientWidth) return
    // Date-tap scrolling keeps the chosen day, but its final visible event must
    // still be captured for a subsequent resize or Account return.
    if (performance.now() < suppressFollow) { save(); return }
    const top = agenda.getBoundingClientRect().top
    const sections = [...agenda.querySelectorAll<HTMLElement>('.day-section')]
    const current = sections.filter(section => section.getBoundingClientRect().top <= top + 45).at(-1) || sections[0]
    if (current && current.dataset.day !== calendar.selected) calendar.follow(current.dataset.day!)
    save()
  })
})
// A user's gesture takes over immediately, even while a date-tap scroll is
// settling. Otherwise a quick wheel/touch gesture can be ignored entirely.
for (const event of ['wheel', 'touchstart', 'pointerdown', 'keydown']) {
  agenda.addEventListener(event, () => { suppressFollow = 0 }, { passive: true })
}
addEventListener('pagehide', save)

async function load(): Promise<void> {
  if (loading) return
  loading = true
  catalogReady = false
  if (openFilters) openFilters.disabled = true
  agenda.setAttribute('aria-busy', 'true')
  const scroll = calendar ? agenda.scrollTop : restored?.scroll || 0
  const focused = accessPanel?.contains(document.activeElement) ? document.activeElement as HTMLElement : null
  const revealDenied = !calendar && byId('app').dataset.deniedAccount === 'true'
  try {
    const catalog = await fetchDiscovery(publicPreview)
    today = catalog.today
    events = catalog.events
    catalogReady = true
    if (!publicPreview && !calendar) calendar = new Calendar(today, restored?.selected || today, restored?.folded ?? true, selectDay, save)
    renderAgenda()
    if (!publicPreview) filterState()
    suppressFollow = performance.now() + 100
    agenda.scrollTop = scroll
    if (!publicPreview) { restoreAnchor(); captureAnchor() }
    if (openFilters) openFilters.disabled = false
  } catch (error) {
    events = []
    calendar?.update(today, new Set())
    sheet?.close()
    agenda.replaceChildren(textElement('p', 'status', error instanceof Error ? error.message : 'Unable to load events.'))
    const retry = document.createElement('ion-button')
    retry.mode = 'ios'
    retry.textContent = 'Try again'
    retry.onclick = () => void load()
    agenda.append(retry)
  } finally {
    loading = false
    agenda.setAttribute('aria-busy', 'false')
    focused?.focus({ preventScroll: true })
    if (revealDenied) showAccess()
  }
}
addEventListener('pageshow', event => { if (event.persisted) void load() })
document.addEventListener('visibilitychange', () => { if (!document.hidden) void load() })
setInterval(() => {
  const current = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date())
  if (!document.hidden && today && current !== today) void load()
}, 60_000)
void load()
