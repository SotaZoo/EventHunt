import { After, Given, Then, When, setDefaultTimeout } from '@cucumber/cucumber'
import { expect, type Page } from '@playwright/test'

import { AppWorld } from './world'
import { mkdirSync, readFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'

setDefaultTimeout(20_000)

Then('a reviewed time conflict is visible alongside its waitlist status', async function (this: AppWorld) {
  if (!process.env.DATABASE_PATH?.includes('eventscout-verify-')) throw new Error('Expected a disposable fixture database')
  const page = this.page!
  const url = 'https://example.test/mixed'
  const review = (clear = false) => execFileSync('uv', ['run', '--no-sync', 'python', '-m', 'api.commands.main',
    'event', 'time-warning', '--url', url, '--evidence', 'Synthetic source review.', ...(clear ? ['--clear'] : [])],
    { cwd: resolve('..'), env: process.env })
  review()
  try {
    await page.reload()
    const card = page.getByRole('link', { name: 'Evening demos — open source', exact: true })
    await card.scrollIntoViewIfNeeded()
    await expect(card.locator('.time-warning')).toHaveText('Date/time unconfirmed. Check with the organizer.')
    await expect(card.locator('.availability-label')).toHaveText('Waitlist')
    const warningId = await card.locator('.time-warning').getAttribute('id')
    const waitlistId = await card.locator('.availability-label').getAttribute('id')
    await expect(card).toHaveAttribute('aria-describedby', `${warningId} ${waitlistId}`)
    await expect(card).toHaveAttribute('href', url)
    for (const theme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' })
      const contrast = await card.locator('.time-warning').evaluate(element => {
        const luminance = (rgb: string) => {
          const channels = rgb.match(/[\d.]+/g)!.slice(0, 3).map(Number).map(v => v / 255)
            .map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
          return channels[0] * .2126 + channels[1] * .7152 + channels[2] * .0722
        }
        const text = luminance(getComputedStyle(element).color)
        const background = luminance(getComputedStyle(document.querySelector('.app')!).backgroundColor)
        return (Math.max(text, background) + .05) / (Math.min(text, background) + .05)
      })
      expect(contrast).toBeGreaterThanOrEqual(4.5)
      mkdirSync('test-results', { recursive: true })
      await page.screenshot({ path: `test-results/time-warning-${theme}.png` })
    }
  } finally { review(true) }
  await page.reload()
  await expect(page.locator('.time-warning')).toHaveCount(0)
})

Then('the desktop calendar preserves selection across month browsing and resizing', async function (this: AppWorld) {
  const page = this.page!
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const selected = page.locator('.calendar-page:not([aria-hidden="true"]) .selected')
  const today = await selected.getAttribute('data-date')
  await page.setViewportSize({ width: 1440, height: 1024 })
  await expect(page.getByRole('button', { name: 'Next month', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Expand month calendar' })).not.toBeVisible()
  expect(await page.locator('.calendar-page').nth(1).locator('.day').count()).toBeGreaterThan(27)
  const calendarBox = await page.locator('.calendar-shell').boundingBox()
  const agendaBox = await page.locator('#agenda').boundingBox()
  expect(calendarBox!.x).toBeGreaterThan(agendaBox!.x + agendaBox!.width)
  await page.locator('#monthPicker').selectOption(`${Number(today!.slice(0, 4))}-12`)
  const december = await selected.getAttribute('data-date')
  await page.getByRole('button', { name: 'Next month', exact: true }).click()
  await expect(page.locator('#monthName')).toHaveText('January')
  await page.getByRole('button', { name: 'Previous month', exact: true }).click()
  await expect(selected).toHaveAttribute('data-date', december!)
  await page.getByRole('button', { name: 'Today', exact: true }).click()
  await expect(selected).toHaveAttribute('data-date', today!)
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('button', { name: 'Expand month calendar' })).toBeVisible()
  await page.getByRole('button', { name: 'Expand month calendar' }).click()
  await page.setViewportSize({ width: 1440, height: 1024 })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('button', { name: 'Fold month calendar' })).toBeVisible()
  await expect(selected).toHaveAttribute('data-date', today!)
})

Then('desktop source and account returns preserve my filters and visible event', async function (this: AppWorld) {
  const page = this.page!
  await page.setViewportSize({ width: 1440, height: 1024 })
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  const catalog = await (await page.request.get(this.baseUrl + '/api/discovery')).json()
  const eventDay = catalog.events.find((event: { title: string }) => event.title === 'Online workshop').day
  await page.getByRole('button', { name: 'Open filters', exact: true }).click()
  await page.locator('ion-segment-button[value="online"]').click()
  await page.getByRole('button', { name: 'Show 2 events', exact: true }).click()
  await page.locator(`#calendarPages [aria-hidden="false"] [data-date="${eventDay}"]`).click()
  const selected = page.locator('.calendar-page:not([aria-hidden="true"]) .selected')
  const source = page.getByRole('link', { name: 'Online workshop — open source' })
  // Exercise native smooth scrolling through its completed animation, not the
  // instant reduced-motion path used by the other calendar checks.
  await page.waitForTimeout(900)
  const initialOffset = await source.evaluate(element => element.getBoundingClientRect().top - document.querySelector('#agenda')!.getBoundingClientRect().top)
  expect(initialOffset).toBeLessThan(100)
  await expect(source).toBeInViewport()
  await expect(selected).toHaveAttribute('data-date', eventDay)
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(source).toBeInViewport()
  await expect(selected).toHaveAttribute('data-date', eventDay)
  await expect.poll(() => source.evaluate((element, offset) => Math.abs(element.getBoundingClientRect().top - document.querySelector('#agenda')!.getBoundingClientRect().top - offset), initialOffset)).toBeLessThan(3)
  await page.setViewportSize({ width: 1440, height: 1024 })
  await source.click()
  await expect(page.getByRole('heading', { name: 'Original listing' })).toBeVisible()
  await page.goBack()
  await expect(selected).toHaveAttribute('data-date', eventDay)
  await expect(source).toBeInViewport()
  await page.getByRole('link', { name: 'Open profile', exact: true }).click()
  await page.getByRole('link', { name: 'AI integration', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Copy setup prompt' })).toBeVisible()
  await page.getByRole('link', { name: 'Account', exact: true }).click()
  await page.getByRole('link', { name: 'Events', exact: true }).click()
  await expect(selected).toHaveAttribute('data-date', eventDay)
  await expect(page.getByRole('button', { name: 'Open filters, filters active' })).toBeVisible()
  await expect(page.locator('.event-card')).toHaveCount(2)
  await expect(source).toBeInViewport()
  const offset = () => source.evaluate(element => element.getBoundingClientRect().top - document.querySelector('#agenda')!.getBoundingClientRect().top)
  const before = await offset()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('button', { name: 'Fold month calendar' })).toBeVisible()
  await expect(selected).toHaveAttribute('data-date', eventDay)
  await expect(source).toBeInViewport()
  await expect.poll(async () => Math.abs(await offset() - before)).toBeLessThan(3)
})

Then('the desktop layout works in both appearances and enlarged short windows', async function (this: AppWorld) {
  const page = this.page!
  mkdirSync('test-results', { recursive: true })
  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme, reducedMotion: 'reduce' })
    await page.setViewportSize({ width: 1440, height: 1024 })
    await expect(page.locator('#agenda')).toHaveAttribute('aria-busy', 'false')
    await page.screenshot({ path: `test-results/desktop-discovery-${colorScheme}.png` })
    await page.getByRole('button', { name: 'Open filters', exact: true }).click()
    const dialog = await page.getByRole('dialog').boundingBox()
    expect(Math.abs(dialog!.x + dialog!.width / 2 - 720)).toBeLessThan(2)
    expect(Math.abs(dialog!.y + dialog!.height / 2 - 512)).toBeLessThan(2)
    await page.screenshot({ path: `test-results/desktop-filters-${colorScheme}.png` })
    await page.keyboard.press('Escape')
    await expect(page.getByRole('button', { name: 'Open filters', exact: true })).toBeFocused()
    await page.setViewportSize({ width: 1024, height: 600 })
    await page.evaluate(() => { document.documentElement.style.fontSize = '200%' })
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false)
    const timesFit = await page.locator('.event-card').evaluateAll(cards => cards.every(card => {
      const time = card.querySelector('time')!.getBoundingClientRect()
      const title = card.querySelector('.event-title')!.getBoundingClientRect()
      return time.right < title.left
    }))
    expect(timesFit).toBe(true)
    const picker = page.locator('#monthPicker')
    const available = await picker.locator('option').evaluateAll(options => options.map(option => (option as HTMLOptionElement).value))
    const sixWeeks = available.find(month => {
      const [year, value] = month.split('-').map(Number)
      return new Date(Date.UTC(year, value - 1, 1)).getUTCDay() + new Date(Date.UTC(year, value, 0)).getUTCDate() > 35
    })!
    await picker.selectOption(sixWeeks)
    const lastDate = page.locator('.calendar-page[aria-hidden="false"] .day:not(.outside)').last()
    await lastDate.scrollIntoViewIfNeeded()
    await expect(lastDate).toBeInViewport()
    await lastDate.click()
    await expect(lastDate).toHaveAttribute('aria-pressed', 'true')
    await page.getByRole('button', { name: 'Today', exact: true }).click()
    await page.getByRole('button', { name: 'Open filters', exact: true }).click()
    await page.getByRole('button', { name: 'Show 4 events', exact: true }).click()
    await page.evaluate(() => { document.documentElement.style.fontSize = '' })
  }
})

Then('the desktop invitation journey retains retry and receipt states', async function (this: AppWorld) {
  const page = this.page!
  await page.setViewportSize({ width: 1440, height: 1120 })
  await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' })
  await expect(page.locator('#agenda')).toHaveAttribute('aria-busy', 'false')
  const input = await page.locator('#requestEmail').boundingBox()
  const button = await page.getByRole('button', { name: 'Request an invite', exact: true }).boundingBox()
  expect(Math.abs(input!.y - button!.y)).toBeLessThan(2)
  expect(button!.x).toBeGreaterThan(input!.x + input!.width)
  mkdirSync('test-results', { recursive: true })
  await page.screenshot({ path: 'test-results/desktop-public-entry.png', fullPage: true })
  await page.locator('#requestEmail').fill('desktop@example.test')
  await page.route('**/api/invitation-requests', route => route.fulfill({ status: 503, json: { error: "Your request wasn't saved. Please try again." } }))
  await page.getByRole('button', { name: 'Request an invite', exact: true }).click()
  await expect(page.getByRole('status')).toContainText("wasn't saved")
  await expect(page.locator('#requestEmail')).toHaveValue('desktop@example.test')
  await page.unroute('**/api/invitation-requests')
  await page.getByRole('button', { name: 'Request an invite', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Request received', exact: true })).toBeFocused()
  await expect(page.locator('#inviteForm')).not.toBeVisible()
  await expect(page.locator('#earlyAccess').getByRole('link', { name: 'Sign in', exact: true })).toBeVisible()
  await page.screenshot({ path: 'test-results/desktop-invite-receipt.png', fullPage: true })
  await page.context().addCookies([{ name: 'synthetic-uninvited-signin', value: '1', url: this.baseUrl }])
  await page.locator('#earlyAccess').getByRole('link', { name: 'Sign in', exact: true }).click()
  await expect(page.getByText("This account isn't invited yet.", { exact: true })).toBeInViewport()
  await expect(page.locator('#requestEmail')).toHaveValue('uninvited@example.test')
  await page.getByRole('link', { name: 'Use another Google account', exact: true }).click()
  await expect(page.locator('#app')).toHaveAttribute('data-public-preview', 'false')
})

async function touchDrag(page: Page, x: number, y: number, dx: number, dy: number): Promise<void> {
  const session = await page.context().newCDPSession(page)
  try {
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] })
    for (let step = 1; step <= 6; step++) {
      await session.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x + dx * step / 6, y: y + dy * step / 6 }] })
    }
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
  } finally {
    await session.detach()
  }
}

Given('the web app is running', function () {
  // The harness assumes the API server is reachable at TEST_BASE_URL.
})

When('I open the events page', async function (this: AppWorld) {
  await this.openPage('/events')
})

Then('the invitation form fails safely without JavaScript', async function (this: AppWorld) {
  const context = await this.page!.context().browser()!.newContext({ javaScriptEnabled: false })
  try {
    const page = await context.newPage()
    await page.goto(this.baseUrl + '/events')
    await page.getByRole('textbox', { name: 'Email address' }).fill('private@example.test')
    await expect(page.getByRole('button', { name: 'Request an invite' })).toBeDisabled()
    await page.getByRole('textbox', { name: 'Email address' }).press('Enter')
    await expect(page).toHaveURL(this.baseUrl + '/events')
    await expect(page.getByRole('link', { name: 'Sign in', exact: true }).first()).toBeVisible()
  } finally { await context.close() }
})

Then('I should see the bounded public preview', async function (this: AppWorld) {
  const page = this.page!
  await expect(page).toHaveURL(/\/events$/)
  await expect(page.locator('#app')).toHaveAttribute('data-public-preview', 'true')
  await expect(page.locator('#agenda')).toHaveAttribute('aria-busy', 'false')
  const publicResponse = await page.request.get(this.baseUrl + '/api/preview?limit=999&offset=3')
  const catalog = await publicResponse.json()
  expect(catalog.events.length).toBeLessThanOrEqual(3)
  await expect(page.locator('.event-card')).toHaveCount(catalog.events.length)
  const privateResponse = await page.request.get(this.baseUrl + '/api/discovery', { maxRedirects: 0 })
  expect(privateResponse.status()).toBe(303)
  await expect(page.locator('#earlyAccess')).toHaveCount(1)
  await expect(page.getByText('Google account email', { exact: true })).toHaveCount(0)
})

When('I use the profile entry to sign in', async function (this: AppWorld) {
  await this.page!.getByRole('link', { name: 'Sign in', exact: true }).first().click()
})

Then('I reach the full invited discovery', async function (this: AppWorld) {
  await expect(this.page!.locator('#app')).toHaveAttribute('data-public-preview', 'false')
  await expect(this.page!.locator('#earlyAccess')).toHaveCount(0)
  await expect(this.page!.locator('#agenda')).toHaveAttribute('aria-busy', 'false')
  expect(await this.page!.locator('.event-card').count()).toBeGreaterThan(3)
})

Then('wrong-account sign-in opens the request and account-switch controls', async function (this: AppWorld) {
  const page = this.page!
  await page.setViewportSize({ width: 390, height: 844 })
  await page.context().addCookies([{ name: 'synthetic-uninvited-signin', value: '1', url: this.baseUrl }])
  await page.getByRole('link', { name: 'Sign in', exact: true }).first().click()
  await expect(page.locator('#app')).toHaveAttribute('data-public-preview', 'true')
  await expect(page.locator('#accessTitle')).toBeFocused()
  await expect(page.getByText("This account isn't invited yet.", { exact: true })).toBeInViewport()
  await expect(page.locator('#requestEmail')).toHaveValue('uninvited@example.test')
  await expect(page.getByRole('link', { name: 'Use another Google account', exact: true })).toBeInViewport()
  const cookies = await page.context().cookies()
  expect(cookies.some(cookie => cookie.name === 'nyc_ai_events_session')).toBe(false)
  await page.getByRole('link', { name: 'Use another Google account', exact: true }).click()
  await expect(page.locator('#app')).toHaveAttribute('data-public-preview', 'false')
})

Then('the landing exposes no restricted controls and its footer is public', async function (this: AppWorld) {
  const page = this.page!
  await expect(page.locator('#calendarPages, #openFilters, #monthPicker, a[href="/settings"]')).toHaveCount(0)
  for (const name of ['About', 'Privacy', 'Terms']) {
    await page.getByRole('link', { name, exact: true }).click()
    await expect(page.getByRole('heading', { name, exact: true })).toBeVisible()
    await page.getByRole('link', { name: 'Back to Event Hunt', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Event Hunt', exact: true })).toBeVisible()
  }
})

Then('preview source links return to the same public app', async function (this: AppWorld) {
  const page = this.page!
  await page.route('https://example.test/**', route => route.fulfill({ contentType: 'text/html', body: '<h1>Original listing</h1>' }))
  await page.locator('.event-card').first().click()
  await expect(page.getByRole('heading', { name: 'Original listing' })).toBeVisible()
  await page.goBack()
  await expect(page.locator('#app')).toHaveAttribute('data-public-preview', 'true')
  await expect(page.locator('#earlyAccess')).toHaveCount(1)
})

Then('I can retry an invitation request without losing my address', async function (this: AppWorld) {
  const page = this.page!
  await page.locator('#requestEmail').fill('newfriend@example.test')
  await page.route('**/api/invitation-requests', route => route.fulfill({ status: 503, json: { error: "Your request wasn't saved. Please try again." } }))
  await page.getByRole('button', { name: 'Request an invite', exact: true }).click()
  await expect(page.locator('#requestStatus')).toContainText("wasn't saved")
  await expect(page.locator('#requestEmail')).toHaveValue('newfriend@example.test')
  await page.unroute('**/api/invitation-requests')
  await page.getByRole('button', { name: 'Request an invite', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Request received', exact: true })).toBeVisible()
  await expect(page.locator('#inviteForm')).not.toBeVisible()
  await expect(page.locator('#earlyAccess').getByRole('link', { name: 'Sign in', exact: true })).toBeVisible()
})

Then('empty and failed preview responses keep the request form and sign in', async function (this: AppWorld) {
  const page = this.page!
  const catalog = await (await page.request.get(this.baseUrl + '/api/preview')).json()
  for (const failure of [false, true]) {
    await page.route('**/api/preview', route => route.fulfill(failure
      ? { status: 503, json: { error: 'Events are temporarily unavailable' } }
      : { json: { ...catalog, events: [] } }))
    await page.reload()
    await expect(page.locator('#agenda')).toHaveAttribute('aria-busy', 'false')
    await expect(page.locator('.event-card')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Request an invite', exact: true })).toBeVisible()
    await expect(page.locator('#earlyAccess').getByRole('link', { name: 'Sign in', exact: true })).toBeVisible()
    await page.unroute('**/api/preview')
  }
})

Then('the public access form fits small mobile and desktop in both appearances', async function (this: AppWorld) {
  const page = this.page!
  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme, reducedMotion: 'reduce' })
    for (const width of [320, 390, 1280]) {
      await page.setViewportSize({ width, height: width === 320 ? 568 : 844 })
      await page.locator('#requestEmail').fill('friend@example.test')
      await page.locator('#requestEmail').focus()
      await page.getByRole('button', { name: 'Request an invite', exact: true }).scrollIntoViewIfNeeded()
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
      expect(overflow).toBe(false)
      await page.evaluate(() => { document.documentElement.style.fontSize = '200%' })
      expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false)
      await page.evaluate(() => { document.documentElement.style.fontSize = '' })
      await expect(page.getByRole('button', { name: 'Request an invite', exact: true })).toBeInViewport()
      if (width === 390) {
        mkdirSync('test-results', { recursive: true })
        await page.screenshot({ path: `test-results/public-entry-${colorScheme}.png`, fullPage: true })
      }
    }
  }
})

Then('the invite form remains reachable in a simulated software-keyboard viewport', async function (this: AppWorld) {
  const page = this.page!
  await page.setViewportSize({ width: 320, height: 568 })
  await page.locator('#requestEmail').fill('keyboard@example.test')
  await page.evaluate(() => {
    Object.defineProperty(window.visualViewport!, 'height', { configurable: true, value: 268 })
    window.visualViewport!.dispatchEvent(new Event('resize'))
  })
  await expect(page.locator('#app')).toHaveAttribute('data-email-keyboard', '')
  await expect(page.locator('#requestEmail')).toBeInViewport()
  await page.getByRole('button', { name: 'Request an invite', exact: true }).scrollIntoViewIfNeeded()
  const form = await page.getByRole('button', { name: 'Request an invite', exact: true }).boundingBox()
  expect(form!.y + form!.height).toBeLessThanOrEqual(269)
  await page.evaluate(() => {
    delete (window.visualViewport! as unknown as { height?: number }).height
    window.visualViewport!.dispatchEvent(new Event('resize'))
  })
})

Then('revoked access cannot retain the private catalog across Back and other tabs', async function (this: AppWorld) {
  const page = this.page!
  const context = page.context()
  const { revocation_cookie } = JSON.parse(readFileSync(process.env.E2E_SESSION_FILE!, 'utf8'))
  await context.clearCookies()
  await context.addCookies([{ name: 'nyc_ai_events_session', value: revocation_cookie, url: this.baseUrl, httpOnly: true, sameSite: 'Lax' }])
  await page.reload()
  await expect(page.locator('.event-card')).toHaveCount(4)
  const other = await context.newPage()
  await other.goto(this.baseUrl + '/events')
  await expect(other.locator('.event-card')).toHaveCount(4)
  await page.locator('.event-card').first().click()
  await expect(page.getByRole('heading', { name: 'Original listing' })).toBeVisible()
  // Real operator CLI, against only the harness-owned synthetic database.
  if (!process.env.DATABASE_PATH?.includes('eventscout-verify-')) throw new Error('Expected a disposable fixture database')
  execFileSync('uv', ['run', '--no-sync', 'python', '-m', 'api.commands.main', 'user', 'deactivate', '--email', 'revocation@example.test'], { cwd: resolve('..'), env: process.env })
  await page.goBack()
  await expect(page.locator('#app')).toHaveAttribute('data-public-preview', 'true')
  expect(await page.locator('.event-card').count()).toBeLessThanOrEqual(3)
  await other.bringToFront() // Headless, task-owned browser only.
  await other.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await expect(other).toHaveURL(/\/events$/)
  await expect(other.locator('#app')).toHaveAttribute('data-public-preview', 'true')
  await expect(other.getByRole('heading', { name: 'Early Access', exact: true })).toBeAttached()
  expect(await other.locator('.event-card').count()).toBeLessThanOrEqual(3)
})

Then('signing out also clears private results in another tab', async function (this: AppWorld) {
  const page = this.page!
  const other = await page.context().newPage()
  await other.goto(this.baseUrl + '/events')
  await expect(other.locator('.event-card')).toHaveCount(4)
  await page.goto(this.baseUrl + '/settings')
  await page.getByRole('button', { name: 'Sign out', exact: true }).click()
  await expect(page).toHaveURL(/\/events$/)
  await expect(page.locator('#app')).toHaveAttribute('data-public-preview', 'true')
  await expect(page.getByRole('heading', { name: 'Early Access', exact: true })).toBeAttached()
  await expect(page.getByRole('link', { name: 'Open profile', exact: true })).toHaveCount(0)
  await other.bringToFront() // Headless, task-owned browser only.
  await other.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await expect(other).toHaveURL(/\/events$/)
  await expect(other.locator('#app')).toHaveAttribute('data-public-preview', 'true')
  await expect(other.getByRole('heading', { name: 'Early Access', exact: true })).toBeAttached()
  expect(await other.locator('.event-card').count()).toBeLessThanOrEqual(3)
})

After(async function (this: AppWorld) {
  const errors = [...this.errors]
  const actions = [...this.eventActions]
  await this.cleanup()
  expect(errors).toEqual([])
  expect(actions).toEqual([])
})

Given('I am an invited friend on the mobile home', async function (this: AppWorld) {
  await this.openInvitedHome()
  await expect(this.page!.getByRole('heading', { name: 'EventHunt', exact: true })).toBeVisible()
  await expect(this.page!.getByRole('button', { name: 'Expand month calendar' })).toBeVisible()
})

Then('I can scan two events on the same day', async function (this: AppWorld) {
  const section = this.page!.locator('.day-section').filter({ hasText: 'Morning builders' })
  await expect(section.getByRole('link')).toHaveCount(2)
  await expect(section.getByText('Evening demos', { exact: true })).toBeVisible()
  await expect(section.getByText('Build working agents with mentors', { exact: true })).toBeVisible()
  await expect(section.getByText('Last places available', { exact: true })).toHaveCount(0)
  await expect(section.getByText('Fit 0.90', { exact: true })).toBeVisible()
  await expect(section.getByText('🔥', { exact: true })).toBeVisible()
})

Then('event facts follow the approved card order', async function (this: AppWorld) {
  const page = this.page!
  const morning = page.getByRole('link', { name: 'Morning builders — open source' })
  const facts = await morning.evaluate(card => [...card.children[1].children].map(child => child.className))
  expect(facts).toEqual(['event-title', 'place-line', 'food-line', 'fit-value', 'urgency-note'])
  await expect(morning.locator('.fit-value .card-highlight')).toHaveText('Build working agents with mentors')
  await expect(morning.locator('.urgency-note')).toHaveText('At last check: Limited spots')
  await expect(morning.locator('.fact-icon')).toHaveCount(3)
  const evening = page.getByRole('link', { name: 'Evening demos — open source' })
  await expect(evening.locator('.urgency-note')).toHaveCount(0)
  await expect(evening.getByText('Waitlist', { exact: true })).toBeVisible()
  const online = page.getByRole('link', { name: 'Online workshop — open source' })
  await expect(online.locator('.card-highlight, .food-line, .urgency-note')).toHaveCount(0)
  await expect(online.locator('.place-line')).toHaveText('Online')
  await expect(online.locator('.fit-value')).toHaveText('Fit —')
})

Then('expired urgency disappears without a reload or date change', async function (this: AppWorld) {
  const page = this.page!
  const instant = Date.now()
  await page.clock.install({ time: instant })
  await page.route('**/api/discovery', async route => {
    const response = await route.fetch()
    const catalog = await response.json()
    const event = catalog.events.find((event: { title: string }) => event.title === 'Morning builders')
    event.urgencyNoteExpiresAt = new Date(instant + 10_000).toISOString()
    const later = catalog.events.find((event: { title: string }) => event.title === 'Evening demos')
    later.urgencyNote = 'At last check: Limited spots'
    later.urgencyNoteExpiresAt = new Date(instant + 30_000).toISOString()
    await route.fulfill({ json: catalog })
  })
  await page.reload()
  const card = page.getByRole('link', { name: 'Morning builders — open source' })
  const later = page.getByRole('link', { name: 'Evening demos — open source' })
  const selected = await page.locator('.calendar-page:not([aria-hidden="true"]) .selected').getAttribute('data-date')
  await expect(card.locator('.urgency-note')).toHaveCount(1)
  await page.clock.runFor(11_000)
  await expect(card.locator('.urgency-note')).toHaveCount(0)
  await expect(later.locator('.urgency-note')).toHaveCount(1)
  await page.clock.runFor(21_000)
  await expect(later.locator('.urgency-note')).toHaveCount(0)
  await expect(page.locator('.calendar-page:not([aria-hidden="true"]) .selected')).toHaveAttribute('data-date', selected!)
  await page.getByRole('button', { name: 'Open filters', exact: true }).click()
  await page.getByRole('button', { name: /^Show \d+ events/ }).click()
  await expect(card.locator('.urgency-note')).toHaveCount(0)
})

Then('only scores at or above 0.90 receive quiet emphasis', async function (this: AppWorld) {
  const page = this.page!
  for (const score of [.8999, .9, .99]) {
    await page.route('**/api/discovery', async route => {
      const response = await route.fetch()
      const catalog = await response.json()
      for (const event of catalog.events) {
        if (event.score !== null) event.score = score
      }
      await route.fulfill({ json: catalog })
    })
    await page.reload()
    await expect(page.locator('.event-card')).toHaveCount(4)
    await expect(page.locator('.excellent-fit')).toHaveCount(score >= .9 ? 3 : 0)
    await expect(page.locator('.excellent-fit[role="button"]')).toHaveCount(0)
    const fit = page.locator('.fit-score').first()
    await expect(fit).toHaveText(new RegExp(`Fit ${score.toFixed(2)}`))
    await page.unroute('**/api/discovery')
  }
  await page.reload()
})

Then('long highlights wrap naturally at enlarged mobile text sizes', async function (this: AppWorld) {
  const page = this.page!
  for (const theme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' })
    for (const width of [320, 390]) {
      await page.setViewportSize({ width, height: 844 })
      const scaling = await page.addStyleTag({ content: 'html { font-size: 200%; }' })
      try {
        const result = await page.locator('.event-card').filter({ hasText: 'Evening demos' }).evaluate(card => {
          const highlight = card.querySelector('.card-highlight')!
          const range = document.createRange()
          range.selectNodeContents(highlight)
          const rects = [...range.getClientRects()]
          const body = card.children[1].getBoundingClientRect()
          const score = card.querySelector('.fit-score')!.getBoundingClientRect()
          const styles = getComputedStyle(highlight)
          return { lines: rects.length, fontSize: styles.fontSize, overflow: styles.overflow,
            clamp: styles.webkitLineClamp, scoreBefore: score.top <= rects[0].top,
            inside: rects.every(rect => rect.left >= body.left - 1 && rect.right <= body.right + 1),
            pageFits: document.documentElement.scrollWidth <= innerWidth }
        })
        expect(result.lines).toBeGreaterThan(1)
        expect(result).toMatchObject({ fontSize: '30px', overflow: 'visible', clamp: 'none',
          scoreBefore: true, inside: true, pageFits: true })
      } finally { await scaling.evaluate(element => element.parentNode?.removeChild(element)) }
    }
  }
})

Then('duplicate and terminal listings do not consume discovery results', async function (this: AppWorld) {
  const response = await this.page!.request.get(`${this.baseUrl}/api/discovery`)
  expect(response.ok()).toBeTruthy()
  const catalog = await response.json()
  expect(catalog.events).toHaveLength(4)
  expect(catalog.events.filter((event: {title: string}) => event.title === 'Morning builders')).toHaveLength(1)
  expect(catalog.events.some((event: {title: string}) => event.title.startsWith('Unavailable'))).toBeFalsy()
  await expect(this.page!.getByRole('link', {name: 'Morning builders — open source'})).toHaveCount(1)
})

Then('the waitlist card remains available with its source link', async function (this: AppWorld) {
  const card = this.page!.getByRole('link', {name: 'Evening demos — open source'})
  await expect(card.getByText('Waitlist', {exact:true})).toBeVisible()
  await expect(card).toHaveAttribute('href', 'https://example.test/mixed')
})

When('I expand the month and choose another day', async function (this: AppWorld) {
  await this.page!.getByRole('button', { name: 'Expand month calendar' }).click()
  await this.page!.locator('.calendar-page:not([aria-hidden="true"]) .day').filter({ hasText: /^15$/ }).click()
  const selected = this.page!.locator('.calendar-page:not([aria-hidden="true"]) .selected')
  const day = await selected.getAttribute('data-date')
  const next = new Date(`${day}T12:00:00Z`)
  next.setUTCDate(next.getUTCDate() + 1)
  await selected.press('ArrowRight')
  await expect(this.page!.locator(`.calendar-page:not([aria-hidden="true"]) [data-date="${next.toISOString().slice(0, 10)}"]`)).toBeFocused()
})

Then('the calendar remains expanded', async function (this: AppWorld) {
  await expect(this.page!.getByRole('button', { name: 'Fold month calendar' })).toHaveAttribute('aria-expanded', 'true')
})

When('I return to Today', async function (this: AppWorld) {
  await this.page!.getByRole('button', { name: 'Today', exact: true }).click()
})

When('I open an event source and return', async function (this: AppWorld) {
  await this.page!.getByRole('link', { name: 'Morning builders — open source' }).click()
  await expect(this.page!.getByRole('heading', { name: 'Original listing' })).toBeVisible()
  await this.page!.goBack()
})

Then('my calendar and browsing context are preserved', async function (this: AppWorld) {
  await expect(this.page!.getByRole('button', { name: 'Fold month calendar' })).toBeVisible()
  await expect(this.page!.getByRole('link', { name: 'Morning builders — open source' })).toBeVisible()
})

Then('I can preview and cancel a format filter', async function (this: AppWorld) {
  const page = this.page!
  await page.getByRole('button', { name: 'Open filters', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Show 4 events', exact: true })).toBeVisible()
  await page.locator('ion-segment-button[value="online"]').click()
  await expect(page.locator('#applyFilters')).toHaveText('Show 2 events')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).not.toBeVisible()
  await expect(page.getByRole('button', { name: 'Open filters', exact: true })).toBeFocused()
  await expect(page.getByRole('link', { name: 'Morning builders — open source' })).toBeVisible()
  await page.getByRole('button', { name: 'Open filters', exact: true }).click()
  await page.locator('ion-segment-button[value="in-person"]').click()
  await expect(page.getByRole('button', { name: 'Show 2 events', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Show 2 events', exact: true }).click()
  await expect(page.locator('.event-card')).toHaveCount(2)
  await expect(page.getByRole('button', { name: 'Open filters, filters active' })).toBeVisible()
})

Then('I can combine score and start-time filters', async function (this: AppWorld) {
  const page = this.page!
  await page.getByRole('button', { name: 'Open filters, filters active' }).click()
  const score = page.getByRole('slider', { name: 'Minimum score', exact: true })
  for (let i = 0; i < 18; i++) await score.press('ArrowRight')
  await expect(page.getByRole('button', { name: 'Show 1 event', exact: true })).toBeVisible()
  const earliest = page.getByRole('slider', { name: 'Earliest event start', exact: true })
  for (let i = 0; i < 17; i++) await earliest.press('ArrowRight')
  await expect(earliest).toHaveAttribute('aria-valuenow', '8.5')
  await expect(page.getByRole('button', { name: 'Show 1 event', exact: true })).toBeVisible()
  const latest = page.getByRole('slider', { name: 'Latest event start', exact: true })
  for (let i = 0; i < 30; i++) await latest.press('ArrowLeft')
  await expect(latest).toHaveAttribute('aria-valuenow', '9')
  await earliest.press('ArrowRight')
  await expect(earliest).toHaveAttribute('aria-valuenow', '8.5')
  await latest.press('ArrowLeft')
  await expect(latest).toHaveAttribute('aria-valuenow', '9')
  await page.getByRole('button', { name: 'Show 1 event', exact: true }).click()
  await expect(page.locator('.event-card')).toHaveCount(1)
  await expect(page.getByRole('link', { name: 'Morning builders — open source' })).toBeVisible()
  await page.getByRole('button', { name: 'Open filters, filters active' }).click()
  await score.press('ArrowRight')
  await expect(page.getByRole('button', { name: 'Show 0 events', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Show 0 events', exact: true }).click()
  await expect(page.getByText('No matching upcoming events.', { exact: true })).toBeVisible()
  await expect(page.locator('.has-events')).toHaveCount(0)
})

Then('I can reset only the draft or apply the reset', async function (this: AppWorld) {
  const page = this.page!
  await page.getByRole('button', { name: 'Open filters, filters active' }).click()
  await page.getByRole('button', { name: 'Reset', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Show 4 events', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Close filters without applying' }).click()
  await expect(page.locator('.event-card')).toHaveCount(0)
  await page.getByRole('button', { name: 'Open filters, filters active' }).click()
  await page.getByRole('button', { name: 'Reset', exact: true }).click()
  await page.getByRole('button', { name: 'Show 4 events', exact: true }).click()
  await expect(page.locator('.event-card')).toHaveCount(4)
  await expect(page.getByRole('button', { name: 'Open filters', exact: true })).toBeVisible()
})

Then('I can drag the calendar open and closed with touch', async function (this: AppWorld) {
  const page = this.page!
  for (const [dy, label] of [[100, 'Fold month calendar'], [-100, 'Expand month calendar'], [100, 'Fold month calendar']] as const) {
    const handle = await page.locator('#calendarHandle').boundingBox()
    if (!handle) throw new Error('Calendar handle is missing')
    await touchDrag(page, handle.x + handle.width / 2, handle.y + handle.height / 2, 0, dy)
    await expect(page.getByRole('button', { name: label })).toBeVisible()
  }
  const agenda = await page.locator('#agenda').boundingBox()
  if (!agenda) throw new Error('Agenda is missing')
  await touchDrag(page, agenda.x + agenda.width / 2, agenda.y + agenda.height - 20, 0, -180)
  await expect.poll(() => page.locator('#agenda').evaluate(element => element.scrollTop)).toBeGreaterThan(0)
  await expect(page.getByRole('button', { name: 'Fold month calendar' })).toBeVisible()
  await page.getByRole('button', { name: 'Today', exact: true }).click()
  await page.getByRole('button', { name: 'Fold month calendar' }).click()
})

Then('I can browse weeks continuously and choose a leap date', async function (this: AppWorld) {
  const page = this.page!
  const pages = page.locator('#calendarPages')
  const first = await pages.locator('.calendar-page').nth(1).getAttribute('data-anchor')
  const box = await pages.boundingBox()
  if (!box) throw new Error('Calendar is missing')
  await touchDrag(page, box.x + box.width - 30, box.y + 25, -300, 0)
  await expect.poll(() => pages.locator('.calendar-page').nth(1).getAttribute('data-anchor')).not.toBe(first)
  const picker = page.getByRole('combobox', { name: 'Change month' })
  const lastMonth = await picker.locator('option').last().getAttribute('value')
  // Activate the visible month label and use the browser's native chooser.
  const label = await page.locator('#monthName').boundingBox()
  if (!label) throw new Error('Month label is missing')
  await page.mouse.click(label.x + label.width / 2, label.y + label.height / 2)
  await expect.poll(() => picker.evaluate(element => element.matches(':open'))).toBe(true)
  await page.keyboard.press('Escape')
  await picker.selectOption(lastMonth!)
  await expect(picker).toHaveValue(lastMonth!)
  let leapYear = new Date(`${first}T12:00:00Z`).getUTCFullYear() + 1
  while (leapYear % 4 || (leapYear % 100 === 0 && leapYear % 400 !== 0)) leapYear++
  await picker.selectOption(`${leapYear}-02`)
  await page.getByRole('button', { name: 'Expand month calendar' }).click()
  await page.locator(`.calendar-page:not([aria-hidden="true"]) [data-date="${leapYear}-02-29"]`).click()
  await expect(page.locator(`.day-section[data-day="${leapYear}-02-29"] h2`)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Fold month calendar' })).toBeVisible()
  await picker.selectOption(`${leapYear - 1}-12`)
  await pages.hover()
  await page.mouse.wheel(390, 0)
  await expect(page.locator('#monthName')).toHaveText('January')
  await page.getByRole('button', { name: 'Today', exact: true }).click()
})

Then('scrolling the agenda follows the day without folding', async function (this: AppWorld) {
  const page = this.page!
  await page.locator('#agenda').hover()
  await page.mouse.wheel(0, 1200)
  await expect.poll(async () => {
    const selected = page.locator('.calendar-page').nth(1).locator('.selected')
    return (await selected.getAttribute('aria-current')) !== 'date'
  }).toBe(true)
  await expect(page.getByRole('button', { name: 'Fold month calendar' })).toBeVisible()
})

Then('light and dark appearances use the same readable text roles', async function (this: AppWorld) {
  const page = this.page!
  for (const theme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' })
    const styles = await page.evaluate(() => {
      const size = (selector: string) => getComputedStyle(document.querySelector(selector)!).fontSize
      return { title: size('h1'), event: size('.event-title'), highlight: size('.card-highlight'), fit: size('.fit-value'), background: getComputedStyle(document.querySelector('.calendar-shell')!).backgroundColor }
    })
    expect(styles).toEqual({ title: '22px', event: '17px', highlight: '15px', fit: '15px', background: theme === 'dark' ? 'rgb(36, 36, 39)' : 'rgb(255, 255, 255)' })
    mkdirSync('test-results', { recursive: true })
    await page.screenshot({ path: `test-results/discovery-${theme}.png` })
  }
  for (const width of [320, 390, 1024]) {
    await page.setViewportSize({ width, height: 844 })
    const textSize = await page.addStyleTag({ content: 'html { font-size: 100%; }' })
    try {
      for (const percent of [100, 150, 200]) {
        await textSize.evaluate((element, size) => { element.textContent = `html { font-size: ${size}%; }` }, percent)
        const times = await page.locator('.event-card').evaluateAll(cards => cards.map(card => {
          const column = card.querySelector('.time-column')!
          const time = column.querySelector('time')!
          const digits = [...time.childNodes].find(node => node.nodeType === Node.TEXT_NODE)!
          const range = document.createRange()
          range.selectNodeContents(digits)
          const lines = [...range.getClientRects()]
          const bounds = range.getBoundingClientRect()
          const columnBounds = column.getBoundingClientRect()
          const periodBounds = time.querySelector('small')!.getBoundingClientRect()
          const bodyBounds = column.nextElementSibling!.getBoundingClientRect()
          return {
            label: time.textContent, lines: lines.length,
            digitsFit: bounds.left >= columnBounds.left - 1 && bounds.right <= columnBounds.right + 1,
            periodBelow: periodBounds.top >= bounds.bottom - 1,
            noOverlap: columnBounds.right < bodyBounds.left,
            bodyLeft: bodyBounds.left,
          }
        }))
        expect(times.map(time => time.label)).toEqual(expect.arrayContaining(['7:31AM', '11:59AM']))
        for (const time of times) {
          expect(time, `${width}px / ${percent}% text: ${time.label}`).toMatchObject({
            lines: 1, digitsFit: true, periodBelow: true, noOverlap: true,
          })
          expect(time.bodyLeft).toBeCloseTo(times[0].bodyLeft, 0)
        }
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
        if (width === 390 && percent === 150) {
          await page.screenshot({ path: 'test-results/discovery-time-enlarged.png' })
        }
      }
    } finally {
      await textSize.evaluate(element => element.parentNode?.removeChild(element))
    }
  }
})

Then('the expanded calendar works on a short enlarged viewport', async function (this: AppWorld) {
  const page = this.page!
  await page.setViewportSize({ width: 320, height: 568 })
  await page.addStyleTag({ content: 'html { font-size: 200%; }' })
  await expect(page.getByRole('button', { name: 'Open filters', exact: true })).toBeInViewport({ ratio: 1 })
  await expect(page.getByRole('link', { name: 'Open profile' })).toBeInViewport({ ratio: 1 })
  const year = new Date().getFullYear()
  await page.getByLabel('Change month').selectOption(`${year + 1}-01`)
  await page.getByRole('button', { name: 'Expand month calendar' }).click()
  await expect(page.getByRole('button', { name: 'Fold month calendar' })).toBeInViewport()
  await expect(page.locator('#agenda')).toBeInViewport()
  await page.getByRole('button', { name: 'Open filters', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Close filters without applying' })).toBeInViewport()
  await page.keyboard.press('Escape')
})

Then('I can reach account and agent settings', async function (this: AppWorld) {
  await this.page!.getByRole('link', { name: 'Open profile' }).click()
  await expect(this.page!).toHaveURL(/\/settings$/)
  await expect(this.page!.getByRole('heading', { name: 'Account', exact: true })).toBeVisible()
  await expect(this.page!.getByRole('button', { name: 'Sign out' })).toBeVisible()
  await expect(this.page!.getByRole('link', { name: 'AI integration' })).toBeVisible()
})

When('I open AI integration through Account', async function (this: AppWorld) {
  await this.page!.getByRole('link', { name: 'Open profile' }).click()
  await expect(this.page!.getByRole('heading', { name: 'Account', exact: true })).toBeVisible()
  await expect(this.page!.getByText('Test Friend', { exact: true })).toBeVisible()
  await expect(this.page!.getByText('discovery@example.test', { exact: true })).toBeVisible()
  await expect(this.page!.getByText('Interest flags', { exact: true })).toHaveCount(0)
  await this.page!.getByRole('link', { name: 'AI integration' }).click()
  await expect(this.page!.getByRole('heading', { name: 'AI integration', exact: true })).toBeVisible()
})

Then('I can copy the complete generic MCP setup prompt', async function (this: AppWorld) {
  await this.page!.context().grantPermissions(['clipboard-read', 'clipboard-write'])
  await this.page!.getByRole('button', { name: 'Copy setup prompt' }).click()
  await expect(this.page!.getByRole('status')).toHaveText('Copied')
  const copied = await this.page!.evaluate(() => navigator.clipboard.readText())
  expect(copied).toBe(`Help me connect Event Hunt via remote MCP at ${process.env.E2E_MCP_RESOURCE_URL}. Use OAuth and guide me through sign-in.`)
  await expect(this.page!.getByRole('status')).toHaveText('Paste into your AI agent')
})

Then('I can return through Account to Events', async function (this: AppWorld) {
  await this.page!.getByRole('link', { name: 'Account', exact: true }).click()
  await this.page!.getByRole('link', { name: 'Events', exact: true }).click()
  await expect(this.page!.getByRole('heading', { name: 'EventHunt', exact: true })).toBeVisible()
})

Then('the account flow is readable in both appearances and enlarged text', async function (this: AppWorld) {
  mkdirSync('test-results', { recursive: true })
  for (const colorScheme of ['light', 'dark'] as const) {
    await this.page!.emulateMedia({ colorScheme, reducedMotion: 'reduce' })
    const colors = await this.page!.locator('.prompt-box').evaluate(element => {
      const css = getComputedStyle(element)
      return { background: css.backgroundColor, text: css.color, size: css.fontSize }
    })
    expect(colors.size).toBe('17px')
    expect(colors.background).toBe(colorScheme === 'light' ? 'rgb(255, 255, 255)' : 'rgb(36, 36, 39)')
    expect(colors.text).toBe(colorScheme === 'light' ? 'rgb(28, 28, 30)' : 'rgb(245, 245, 247)')
    await this.page!.screenshot({ path: `test-results/ai-integration-${colorScheme}.png`, fullPage: true })
  }
  for (const width of [320, 390, 768]) {
    await this.page!.setViewportSize({ width, height: 844 })
    await this.page!.evaluate(() => { document.documentElement.style.fontSize = '200%' })
    expect(await this.page!.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    for (const selector of ['#copy-prompt', '.client-row button', '.account-back']) {
      const box = await this.page!.locator(selector).first().boundingBox()
      expect(box!.height).toBeGreaterThanOrEqual(44)
    }
  }
  for (const path of ['/settings', '/about', '/oauth/consent?authorization_id=browser-layout-request']) {
    for (const colorScheme of ['light', 'dark'] as const) {
      await this.page!.emulateMedia({ colorScheme, reducedMotion: 'reduce' })
      for (const width of [320, 390, 768]) {
        await this.page!.setViewportSize({ width, height: 844 })
        await this.page!.goto(`${this.baseUrl}${path}`)
        // Stress the rendered layout without changing account/provider behavior.
        await this.page!.evaluate(() => {
          document.documentElement.style.fontSize = '200%'
          const email = document.querySelector('.account-email, .signed-in p:last-child')
          if (email) email.textContent = 'a'.repeat(63) + '@' + 'b'.repeat(63) + '.' + 'c'.repeat(63) + '.test'
        })
        expect(await this.page!.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
        const backLines = await this.page!.locator('.account-back').evaluateAll(elements => elements.flatMap(element =>
          Array.from(element.childNodes).filter(node => node.nodeType === 3 && node.textContent?.trim()).map(node => {
            const range = document.createRange()
            range.selectNodeContents(node)
            return range.getClientRects().length
          }),
        ))
        expect(backLines.every(lines => lines === 1)).toBe(true)
        await this.page!.keyboard.press('Tab')
        const focus = await this.page!.evaluate(() => {
          const active = document.activeElement!
          const css = getComputedStyle(active)
          return { tag: active.tagName, style: css.outlineStyle, width: css.outlineWidth }
        })
        expect(['A', 'BUTTON']).toContain(focus.tag)
        expect(focus.style).toBe('solid')
        expect(focus.width).toBe('3px')
      }
    }
  }
  await this.page!.goto(`${this.baseUrl}/settings`)
  await this.page!.screenshot({ path: 'test-results/account.png', fullPage: true })
})

Then('failed copying leaves a visible manual-copy instruction', async function (this: AppWorld) {
  await this.page!.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: async () => { throw new Error('Clipboard unavailable') } } })
  })
  await this.page!.reload()
  await this.page!.getByRole('button', { name: 'Copy setup prompt' }).click()
  await expect(this.page!.getByRole('status')).toHaveText('Could not copy. Select the prompt and copy it.')
  expect(await this.page!.evaluate(() => window.getSelection()?.toString())).toContain('Help me connect Event Hunt')
})

Then('removal confirmation remains readable with a long client name', async function (this: AppWorld) {
  await this.page!.context().addCookies([{ name: 'nyc_ai_events_sb_access', value: 'synthetic-web-access-long-name', url: this.baseUrl }])
  await this.page!.reload()
  await this.page!.setViewportSize({ width: 320, height: 568 })
  await this.page!.evaluate(() => { document.documentElement.style.fontSize = '200%' })
  const clientName = await this.page!.locator('.client-name').innerText()
  expect(clientName.length).toBeGreaterThan(70)
  await this.page!.locator('.client-row').getByRole('button', { name: 'Remove access' }).click()
  const dialog = this.page!.getByRole('dialog')
  await expect(dialog.getByRole('heading')).toHaveText(`Remove access for ${clientName}?`)
  const layout = await dialog.evaluate(element => ({
    overflow: getComputedStyle(element).overflowY,
    height: element.clientHeight,
    contentHeight: element.scrollHeight,
    controls: [...element.querySelectorAll('button')].map(button => ({
      height: button.clientHeight, contentHeight: button.scrollHeight,
    })),
  }))
  expect(layout.overflow).toBe('auto')
  expect(layout.height).toBeLessThanOrEqual(568)
  for (const control of layout.controls) {
    expect(control.height).toBeGreaterThanOrEqual(48)
    expect(control.contentHeight).toBeLessThanOrEqual(control.height + 1)
  }
  await dialog.getByRole('heading').scrollIntoViewIfNeeded()
  await expect(dialog.getByRole('heading')).toBeInViewport()
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(dialog).not.toBeVisible()
  await expect(this.page!.getByText(clientName, { exact: true })).toBeVisible()
})

Then('I can cancel removal and keep the client connected', async function (this: AppWorld) {
  const remove = this.page!.locator('.client-row').getByRole('button', { name: 'Remove access' })
  await remove.click()
  const dialog = this.page!.getByRole('dialog')
  await expect(dialog.getByRole('heading')).toHaveText('Remove access for Example AI?')
  await expect(dialog.getByRole('button', { name: 'Cancel', exact: true })).toBeFocused()
  await dialog.press('Escape')
  await expect(dialog).not.toBeVisible()
  await expect(remove).toBeFocused()
  await expect(this.page!.getByText('Example AI', { exact: true })).toBeVisible()
})

Then('confirming removal shows the empty authorized-client state', async function (this: AppWorld) {
  await this.page!.locator('.client-row').getByRole('button', { name: 'Remove access' }).click()
  await this.page!.getByRole('dialog').getByRole('button', { name: 'Remove access', exact: true }).click()
  await expect(this.page!.getByText('No authorized clients.', { exact: true })).toBeVisible()
})

Then('allowing access returns to the AI app', async function (this: AppWorld) {
  await this.page!.goto(`${this.baseUrl}/oauth/consent?authorization_id=browser-request`)
  await expect(this.page!.getByRole('heading', { name: 'Authorize Example AI' })).toBeVisible()
  await expect(this.page!.getByText('discovery@example.test', { exact: true })).toBeVisible()
  await expect(this.page!.getByText('Read event recommendations', { exact: true })).toBeVisible()
  await expect(this.page!.getByText('View your email address', { exact: true })).toBeVisible()
  await this.page!.screenshot({ path: 'test-results/authorize-ai.png', fullPage: true })
  await this.page!.getByRole('button', { name: 'Allow access' }).click()
  await expect(this.page!).toHaveURL(`${this.baseUrl}/_test/ai/callback?decision=approve`)
  await expect(this.page!.getByRole('heading', { name: 'Your AI app' })).toBeVisible()
})

Then('cancelling access returns to the AI app', async function (this: AppWorld) {
  await this.page!.goto(`${this.baseUrl}/oauth/consent?authorization_id=browser-cancel-request`)
  await this.page!.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(this.page!).toHaveURL(`${this.baseUrl}/_test/ai/callback?decision=deny`)
})

Then('I can retry cancelled sign-in and review consent before returning to my AI app', async function (this: AppWorld) {
  await this.page!.context().clearCookies()
  await this.page!.context().addCookies([{ name: 'synthetic-cancel-signin', value: 'once', url: this.baseUrl }])
  await this.page!.goto(`${this.baseUrl}/oauth/consent?authorization_id=browser-retry-request`)
  await expect(this.page!.getByRole('alert')).toHaveText('Google sign-in was cancelled or incomplete')
  const signIn = this.page!.getByRole('link', { name: 'Log in with Google' })
  await expect(signIn).toHaveAttribute('href', '/auth/supabase/start?authorization_id=browser-retry-request')
  await signIn.click()
  await expect(this.page!).toHaveURL(`${this.baseUrl}/oauth/consent?authorization_id=browser-retry-request`)
  await this.page!.getByRole('button', { name: 'Allow access' }).click()
  await expect(this.page!).toHaveURL(`${this.baseUrl}/_test/ai/callback?decision=approve`)
})

Then('an expired website session signs in again without automatically approving access', async function (this: AppWorld) {
  await this.page!.goto(`${this.baseUrl}/oauth/consent?authorization_id=browser-expiry-request`)
  await this.page!.context().clearCookies({ name: 'nyc_ai_events_session' })
  await this.page!.getByRole('button', { name: 'Allow access' }).click()
  await expect(this.page!).toHaveURL(`${this.baseUrl}/oauth/consent?authorization_id=browser-expiry-request`)
  await expect(this.page!.getByRole('button', { name: 'Allow access' })).toBeVisible()
})

Then('unavailable access information is not shown as an empty client list', async function (this: AppWorld) {
  await this.page!.context().addCookies([{ name: 'nyc_ai_events_sb_access', value: 'synthetic-web-access-grants-error', url: this.baseUrl }])
  await this.page!.goto(`${this.baseUrl}/settings/ai`)
  await expect(this.page!.getByText('Access information is unavailable.', { exact: false })).toBeVisible()
  await expect(this.page!.getByText('No authorized clients.', { exact: true })).toHaveCount(0)
  await this.page!.context().addCookies([{ name: 'nyc_ai_events_sb_access', value: 'synthetic-web-access', url: this.baseUrl }])
  await this.page!.getByRole('link', { name: 'Try again' }).click()
  await expect(this.page!.getByText('Example AI', { exact: true })).toBeVisible()
})

Then('failed removal keeps the client available for retry', async function (this: AppWorld) {
  await this.page!.context().addCookies([{ name: 'nyc_ai_events_sb_access', value: 'synthetic-web-access-remove-error', url: this.baseUrl }])
  await this.page!.reload()
  await this.page!.locator('.client-row').getByRole('button', { name: 'Remove access' }).click()
  await this.page!.getByRole('dialog').getByRole('button', { name: 'Remove access', exact: true }).click()
  await expect(this.page!.getByRole('alert')).toHaveText('Could not remove access. Try again.')
  await expect(this.page!.getByText('Example AI', { exact: true })).toBeVisible()
})

Then('an expired authorization explains how to restart Connect', async function (this: AppWorld) {
  await this.page!.goto(`${this.baseUrl}/oauth/consent?authorization_id=expired-request`)
  await expect(this.page!.getByText('Restart Connect in your AI app to get a new authorization request.')).toBeVisible()
})

Then('I can read About and sign out from Account', async function (this: AppWorld) {
  await this.page!.getByRole('link', { name: 'Open profile' }).click()
  await expect(this.page!.getByRole('link', { name: 'Feedback', exact: true })).toHaveAttribute('href', 'mailto:feedback@example.test?subject=Event+Hunt+feedback')
  await this.page!.getByRole('link', { name: 'About', exact: true }).click()
  await expect(this.page!.getByText('Browse on the web or explore events with your AI assistant.', { exact: false })).toBeVisible()
  await this.page!.getByRole('link', { name: 'Back to Event Hunt', exact: true }).click()
  await this.page!.getByRole('link', { name: 'Open profile' }).click()
  await this.page!.getByRole('button', { name: 'Sign out' }).click()
  await expect(this.page!).toHaveURL(`${this.baseUrl}/events`)
  await expect(this.page!.locator('#app')).toHaveAttribute('data-public-preview', 'true')
  await expect(this.page!.getByRole('heading', { name: 'Early Access', exact: true })).toBeAttached()
})

Given('the discovery catalog is temporarily unavailable', async function (this: AppWorld) {
  await this.openInvitedHome()
  await this.page!.route('**/api/discovery', route => route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"unavailable"}' }))
  await this.page!.reload()
  await expect(this.page!.getByText('Events are temporarily unavailable.', { exact: true })).toBeVisible()
})

Then('I can retry loading events', async function (this: AppWorld) {
  await this.page!.unroute('**/api/discovery')
  await this.page!.getByRole('button', { name: 'Try again', exact: true }).click()
  await expect(this.page!.getByRole('link', { name: 'Morning builders — open source' })).toBeVisible()
})

Then('a failed refresh cannot be dismissed by calendar navigation', async function (this: AppWorld) {
  const page = this.page!
  await expect(page.locator('.event-card')).toHaveCount(4)
  await page.route('**/api/discovery', route => route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"unavailable"}' }))
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })))
  await expect(page.getByRole('button', { name: 'Try again', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Today', exact: true }).click()
  await page.locator('.calendar-page:not([aria-hidden="true"]) .day').last().click()
  await expect(page.getByRole('button', { name: 'Try again', exact: true })).toBeVisible()
  await expect(page.locator('.event-card')).toHaveCount(0)
  await expect(page.locator('.has-events')).toHaveCount(0)
})

Then('source content is displayed only as text', async function (this: AppWorld) {
  const page = this.page!
  // Transport-fault fixture at the HTTP boundary, not a mock of UI internals.
  // Real database handling of unsafe URLs is covered by HTTP integration tests.
  await page.route('**/api/discovery', async route => {
    const response = await route.fetch()
    const catalog = await response.json()
    catalog.events[0].title = '<img src=x onerror=alert(1)>'
    catalog.events[0].highlight = '<script>alert(1)</script>'
    catalog.events[0].place = '<b>Not markup</b>'
    catalog.events[0].food = '<svg onload=alert(1)>'
    catalog.events[0].url = null
    await route.fulfill({ json: catalog })
  })
  await page.reload()
  await expect(page.getByText('<img src=x onerror=alert(1)>', { exact: true })).toBeVisible()
  await expect(page.getByText('<script>alert(1)</script>', { exact: true })).toBeVisible()
  await expect(page.locator('#agenda img, #agenda script, #agenda svg[onload]')).toHaveCount(0)
  await expect(page.getByRole('link', { name: /<img/ })).toHaveCount(0)
})
