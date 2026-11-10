import { attachInvitationForm } from '../discovery/invitation'

function panel(): HTMLElement {
  document.body.innerHTML = '<section><h2 id="accessTitle">Early Access</h2><form id="inviteForm"><input name="email" type="email" value="friend@example.com"><button type="submit">Request an invite</button></form><p id="requestStatus" role="status"></p><a href="/login">Sign in</a></section>'
  const element = document.querySelector('section')!
  attachInvitationForm(element)
  return element
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0))

afterEach(() => {
  jest.useRealTimers()
})

test('request receipt replaces form only after persistence and keeps sign in', async () => {
  let finish: (response: unknown) => void = () => {}
  global.fetch = jest.fn(() => new Promise(resolve => { finish = resolve })) as jest.Mock
  const element = panel()
  element.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }))
  expect(element.querySelector('button')!.disabled).toBe(true)
  expect(element.querySelector('button')!.textContent).toBe('Sending…')
  finish({ ok: true, status: 202, json: async () => ({ message: 'Request received' }) })
  await flush()
  expect(element.querySelector('form')!.hidden).toBe(true)
  expect(element.querySelector('#accessTitle')!.textContent).toBe('Request received')
  expect(element.querySelector('a')!.textContent).toBe('Sign in')
})

test('failed save keeps address and retry available with an accessible error', async () => {
  global.fetch = jest.fn().mockResolvedValue({ ok: false, status: 503, json: async () => ({ error: "Your request wasn't saved. Please try again." }) })
  const element = panel()
  element.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }))
  await flush()
  expect(element.querySelector('input')!.value).toBe('friend@example.com')
  expect(element.querySelector('button')!.disabled).toBe(false)
  expect(element.querySelector('[role="status"]')!.textContent).toContain("wasn't saved")
})

test.each(['headers', 'body'])('stalled %s preserves the address and allows retry after 15 seconds', async phase => {
  jest.useFakeTimers()
  global.fetch = jest.fn((_url, options) => {
    const stalled = new Promise((_resolve, reject) => {
      options.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    })
    return phase === 'headers' ? stalled : Promise.resolve({ status: 202, json: () => stalled })
  }) as jest.Mock
  const element = panel()
  element.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }))
  await jest.advanceTimersByTimeAsync(15_000)
  expect(element.querySelector('form')!.hidden).toBe(false)
  expect(element.querySelector('input')!.value).toBe('friend@example.com')
  expect(element.querySelector('button')!.disabled).toBe(false)
  expect(element.querySelector('[role="status"]')!.textContent).toBe("We couldn't confirm your request. Please try again.")
  expect(jest.getTimerCount()).toBe(0)

  global.fetch = jest.fn().mockResolvedValue({ status: 202, json: async () => ({ message: 'Request received' }) })
  element.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }))
  await jest.advanceTimersByTimeAsync(0)
  expect(element.querySelector('form')!.hidden).toBe(true)
  expect(jest.getTimerCount()).toBe(0)
})

test.each(['network', 'malformed receipt'])('%s failure does not claim the request was not saved', async failure => {
  global.fetch = failure === 'network'
    ? jest.fn().mockRejectedValue(new TypeError('Connection lost'))
    : jest.fn().mockResolvedValue({ status: 202, json: async () => ({}) })
  const element = panel()
  element.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true }))
  await flush()
  expect(element.querySelector('button')!.disabled).toBe(false)
  expect(element.querySelector('input')!.value).toBe('friend@example.com')
  expect(element.querySelector('[role="status"]')!.textContent).toBe("We couldn't confirm your request. Please try again.")
})
