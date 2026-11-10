/** One persistent inline form, shared by loading, preview and failure states. */
import { InvitationRequestError, requestInvitation } from '../service/invitations'

export function attachInvitationForm(panel: HTMLElement): void {
  const form = panel.querySelector<HTMLFormElement>('#inviteForm')!
  const input = form.querySelector<HTMLInputElement>('input')!
  const button = form.querySelector<HTMLButtonElement>('button')!
  const status = panel.querySelector<HTMLElement>('#requestStatus')!
  const title = panel.querySelector<HTMLElement>('#accessTitle')!
  let sending = false
  button.disabled = false
  form.addEventListener('submit', async event => {
    event.preventDefault()
    if (sending || !form.reportValidity()) return
    sending = true
    button.disabled = true
    button.textContent = 'Sending…'
    status.textContent = 'Sending…'
    input.removeAttribute('aria-invalid')
    try {
      await requestInvitation(input.value)
      form.hidden = true
      title.textContent = 'Request received'
      status.textContent = "We'll email you if you're invited."
      title.setAttribute('tabindex', '-1')
      title.focus({ preventScroll: true })
    } catch (error) {
      if (error instanceof InvitationRequestError && error.invalidEmail) input.setAttribute('aria-invalid', 'true')
      status.textContent = error instanceof Error ? error.message : "Unable to save your request. Please try again."
    } finally {
      sending = false
      button.disabled = false
      button.textContent = 'Request an invite'
    }
  })
  input.setAttribute('aria-describedby', 'requestStatus')
  const app = document.getElementById('app')
  const viewport = window.visualViewport
  if (app && viewport) {
    const fitKeyboard = () => {
      const keyboard = document.activeElement === input && viewport.height < window.innerHeight - 100
      app.toggleAttribute('data-email-keyboard', keyboard)
      if (keyboard) {
        app.style.setProperty('--access-viewport-height', `${viewport.height}px`)
        input.scrollIntoView({ block: 'center', behavior: 'instant' })
      } else app.style.removeProperty('--access-viewport-height')
    }
    viewport.addEventListener('resize', fitKeyboard)
    input.addEventListener('focus', fitKeyboard)
    input.addEventListener('blur', () => setTimeout(fitKeyboard, 0))
  }
}
