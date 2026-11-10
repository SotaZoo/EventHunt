// Calendar arithmetic uses UTC date-only values. The server supplies New York's
// current date and each event's local day; the browser timezone cannot shift them.
export const dayKey = (date: Date) => date.toISOString().slice(0, 10)
export const dayDate = (key: string) => new Date(`${key}T12:00:00Z`)
export function addDays(date: Date, count: number): Date {
  const next = new Date(date)
  next.setUTCDate(next.getUTCDate() + count)
  return next
}
const weekStart = (date: Date) => addDays(date, -date.getUTCDay())
const monthStart = (date: Date, offset = 0) => new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + offset, 1, 12))
export const formatDay = (key: string, options: Intl.DateTimeFormatOptions) => dayDate(key).toLocaleDateString('en-US', { ...options, timeZone: 'UTC' })

export class Calendar {
  private anchor: Date
  private eventDays = new Set<string>()
  private timer?: ReturnType<typeof setTimeout>
  private resetting = false
  private dragStart: number | null = null
  private dragged = false
  private picker = document.querySelector<HTMLSelectElement>('#monthPicker')!
  private pickerMonth = ''
  private handle = document.querySelector<HTMLButtonElement>('#calendarHandle')!
  private pages = document.querySelector<HTMLDivElement>('#calendarPages')!
  private resizeObserver: ResizeObserver
  private desktop = matchMedia('(min-width: 1024px)')

  private get showsWeek(): boolean { return this.folded && !this.desktop.matches }

  constructor(public today: string, public selected: string, public folded: boolean,
              private select: (day: string) => void, private save: () => void) {
    this.anchor = dayDate(selected)
    document.querySelector<HTMLButtonElement>('#previousMonth')!.onclick = () => this.browseMonth(-1)
    document.querySelector<HTMLButtonElement>('#nextMonth')!.onclick = () => this.browseMonth(1)
    this.desktop.addEventListener('change', () => {
      this.anchor = dayDate(this.selected)
      this.render()
    })
    this.picker.onchange = () => {
      if (/^\d{4}-\d{2}$/.test(this.picker.value)) this.select(`${this.picker.value}-01`)
    }
    this.handle.onclick = event => {
      if (this.dragged) { this.dragged = false; event.preventDefault(); return }
      this.toggle()
    }
    this.handle.onpointerdown = event => {
      this.dragStart = event.clientY
      this.dragged = false
      this.handle.setPointerCapture(event.pointerId)
    }
    this.handle.onpointerup = event => {
      if (this.dragStart === null) return
      const delta = event.clientY - this.dragStart
      this.dragged = Math.abs(delta) > 28
      if (this.dragged && ((delta < 0 && !this.folded) || (delta > 0 && this.folded))) this.toggle()
      this.dragStart = null
    }
    this.handle.onpointercancel = () => { this.dragStart = null; this.dragged = false }
    this.pages.addEventListener('scroll', () => {
      clearTimeout(this.timer)
      if (this.resetting) return
      this.timer = setTimeout(() => this.settle(), 150)
    })
    this.pages.addEventListener('keydown', event => {
      const button = (event.target as Element).closest<HTMLButtonElement>('.day')
      if (!button?.dataset.date) return
      const offsets: Record<string, number> = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }
      if (!(event.key in offsets)) return
      event.preventDefault()
      const day = dayKey(addDays(dayDate(button.dataset.date), offsets[event.key]))
      this.select(day)
      this.pages.querySelector<HTMLButtonElement>(`.calendar-page:not([aria-hidden="true"]) [data-date="${day}"]`)?.focus()
    })
    this.resizeObserver = new ResizeObserver(() => this.render())
    this.resizeObserver.observe(document.querySelector('#app')!)
    this.render()
  }

  update(today: string, days: Set<string>): void {
    this.today = today
    this.eventDays = days
    this.render()
  }

  follow(day: string): void {
    this.selected = day
    const active = this.pages.children[1]
    if (active?.querySelector(`[data-date="${day}"]:not(.outside)`)) {
      this.updateSelection()
      this.heading()
    } else {
      this.anchor = dayDate(day)
      this.render()
    }
  }

  private toggle(): void {
    this.folded = !this.folded
    this.anchor = dayDate(this.selected)
    this.render()
    this.save()
  }

  private browseMonth(offset: number): void {
    this.anchor = monthStart(this.anchor, offset)
    this.render()
  }

  private settle(): void {
    const index = Math.round(this.pages.scrollLeft / this.pages.clientWidth)
    if (index === 1 || !this.pages.children[index]) return
    this.anchor = dayDate((this.pages.children[index] as HTMLElement).dataset.anchor!)
    // Recenter a three-page window so browsing has no hard-coded date horizon.
    this.render()
  }

  private updateSelection(): void {
    this.pages.querySelectorAll<HTMLButtonElement>('.day').forEach(button => {
      const selected = button.dataset.date === this.selected
      button.classList.toggle('selected', selected)
      button.setAttribute('aria-pressed', String(selected))
    })
  }

  private heading(): void {
    const current = this.pages.children[1] as HTMLElement
    const date = current.querySelector(`[data-date="${this.selected}"]:not(.outside)`) ? this.selected : dayKey(this.anchor)
    document.querySelector('#monthName')!.textContent = formatDay(date, { month: 'long' })
    if (this.pickerMonth !== date.slice(0, 7)) {
      this.pickerMonth = date.slice(0, 7)
      this.picker.replaceChildren()
      // A moving window keeps the native chooser short without imposing a
      // fixed date horizon. Selecting a distant month extends it again.
      for (let offset = -24; offset <= 24; offset++) {
        const month = dayKey(monthStart(dayDate(date), offset))
        this.picker.add(new Option(formatDay(month, { month: 'long', year: 'numeric' }), month.slice(0, 7)))
      }
    }
    this.picker.value = date.slice(0, 7)
  }

  render(): void {
    const focused = (document.activeElement as HTMLElement | null)?.dataset.date
    this.resetting = true
    clearTimeout(this.timer)
    this.pages.replaceChildren()
    for (const offset of [-1, 0, 1]) {
      const start = this.showsWeek ? addDays(weekStart(this.anchor), offset * 7) : monthStart(this.anchor, offset)
      const first = this.showsWeek ? start : weekStart(start)
      const last = addDays(monthStart(start, 1), -1)
      const cells = this.showsWeek ? 7 : Math.ceil((start.getUTCDay() + last.getUTCDate()) / 7) * 7
      const page = document.createElement('div')
      page.className = 'calendar-page'
      page.dataset.anchor = dayKey(start)
      page.inert = offset !== 0
      page.setAttribute('aria-hidden', String(offset !== 0))
      for (let i = 0; i < cells; i++) {
        const date = addDays(first, i)
        const key = dayKey(date)
        const button = document.createElement('button')
        button.className = 'day'
        button.type = 'button'
        button.dataset.date = key
        button.classList.toggle('outside', !this.showsWeek && date.getUTCMonth() !== start.getUTCMonth())
        button.classList.toggle('is-today', key === this.today)
        button.classList.toggle('has-events', this.eventDays.has(key))
        button.setAttribute('aria-label', formatDay(key, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' }) + (key === this.today ? ', today' : '') + (this.eventDays.has(key) ? ', events' : ''))
        if (key === this.today) button.setAttribute('aria-current', 'date')
        const number = document.createElement('span')
        number.textContent = String(date.getUTCDate())
        button.append(number)
        button.onclick = () => this.select(key)
        page.append(button)
      }
      this.pages.append(page)
    }
    this.pages.scrollLeft = this.pages.clientWidth
    this.pages.scrollTop = 0
    const current = this.pages.children[1] as HTMLElement
    const app = document.querySelector('#app')!
    const header = document.querySelector('.calendar-shell')!
    const fixedHeight = header.scrollHeight - this.pages.offsetHeight
    const available = Math.max(72, this.desktop.matches
      ? app.getBoundingClientRect().bottom - header.getBoundingClientRect().top - fixedHeight - 24
      : app.clientHeight - document.querySelector('.appbar')!.clientHeight - fixedHeight - 130)
    this.pages.style.height = `${Math.min(current.offsetHeight, available)}px`
    this.pages.style.overflowY = current.offsetHeight > available ? 'auto' : 'hidden'
    this.handle.setAttribute('aria-expanded', String(!this.folded))
    this.handle.setAttribute('aria-label', this.folded ? 'Expand month calendar' : 'Fold month calendar')
    this.updateSelection()
    this.heading()
    if (focused) current.querySelector<HTMLButtonElement>(`[data-date="${focused}"]`)?.focus({ preventScroll: true })
    requestAnimationFrame(() => { this.resetting = false })
  }
}
