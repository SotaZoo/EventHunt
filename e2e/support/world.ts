import { setWorldConstructor, World } from '@cucumber/cucumber'
import { Browser, Page, chromium } from '@playwright/test'

import { TEST_BASE_URL } from '../test-env'
import { readFileSync } from 'node:fs'

export class AppWorld extends World {
  browser?: Browser
  page?: Page
  errors: string[] = []
  eventActions: string[] = []

  baseUrl = TEST_BASE_URL

  async openPage(path: string) {
    this.browser = await chromium.launch()
    this.page = await (await this.browser.newContext()).newPage()
    await this.page.goto(`${this.baseUrl}${path}`)
  }

  async cleanup() {
    await this.browser?.close()
  }

  async openInvitedHome() {
    this.browser = await chromium.launch()
    this.page = await (await this.browser.newContext({ viewport: { width: 390, height: 844 }, timezoneId: 'Asia/Tokyo', hasTouch: true })).newPage()
    this.page.setDefaultTimeout(5000)
    this.page.on('pageerror', error => this.errors.push(error.message))
    this.page.on('request', request => {
      if (/\/events\/\d+\/(action|status|rating)/.test(request.url())) this.eventActions.push(request.method())
    })
    const file = process.env.E2E_SESSION_FILE
    if (!file) throw new Error('E2E_SESSION_FILE must point to the synthetic fixture session')
    const { cookie, supabase_access, supabase_refresh } = JSON.parse(readFileSync(file, 'utf8'))
    await this.page.context().addCookies([{ name: 'nyc_ai_events_session', value: cookie, url: this.baseUrl, httpOnly: true, sameSite: 'Lax' }])
    if (supabase_access && supabase_refresh) {
      await this.page.context().addCookies([
        { name: 'nyc_ai_events_sb_access', value: supabase_access, url: this.baseUrl, httpOnly: true, sameSite: 'Lax' },
        { name: 'nyc_ai_events_sb_refresh', value: supabase_refresh, url: this.baseUrl, httpOnly: true, sameSite: 'Lax' },
      ])
    }
    await this.page.route('https://example.test/**', route => route.fulfill({ contentType: 'text/html', body: '<h1>Original listing</h1>' }))
    await this.page.goto(`${this.baseUrl}/events`)
  }
}

setWorldConstructor(AppWorld)
