import '../discovery/styles.css'
import './styles.css'

document.querySelector('form[action="/logout"]')?.addEventListener('submit', () => {
  try { sessionStorage.removeItem('eventHuntDiscovery') } catch { /* Storage may be disabled. */ }
})

const copyButton = document.querySelector<HTMLButtonElement>('#copy-prompt')
const prompt = document.querySelector<HTMLElement>('#setup-prompt')
const copyStatus = document.querySelector<HTMLElement>('#copy-status')
let copyReset: ReturnType<typeof setTimeout> | undefined
copyButton?.addEventListener('click', async () => {
  if (!prompt || !copyStatus) return
  clearTimeout(copyReset)
  try {
    await navigator.clipboard.writeText(prompt.textContent?.trim() || '')
    copyStatus.textContent = 'Copied'
    copyReset = setTimeout(() => { copyStatus.textContent = 'Paste into your AI agent' }, 2000)
  } catch {
    copyStatus.textContent = 'Could not copy. Select the prompt and copy it.'
    const selection = window.getSelection()
    const range = document.createRange()
    range.selectNodeContents(prompt)
    selection?.removeAllRanges()
    selection?.addRange(range)
  }
})

const removeDialog = document.querySelector<HTMLDialogElement>('#remove-dialog')
const removeTitle = document.querySelector<HTMLElement>('#remove-title')
let pendingForm: HTMLFormElement | undefined
let removeTrigger: HTMLElement | undefined
document.querySelectorAll<HTMLFormElement>('form[data-remove-client]').forEach(form => {
  form.addEventListener('submit', event => {
    if (!removeDialog?.showModal || !removeTitle) return
    event.preventDefault()
    pendingForm = form
    removeTrigger = form.querySelector('button') || undefined
    removeTitle.textContent = `Remove access for ${form.dataset.removeClient}?`
    removeDialog.showModal()
  })
})
document.querySelector('#cancel-remove')?.addEventListener('click', () => removeDialog?.close())
removeDialog?.addEventListener('close', () => {
  pendingForm = undefined
  removeTrigger?.focus()
})
document.querySelector('#confirm-remove')?.addEventListener('click', () => {
  const form = pendingForm
  if (!form) return
  const confirmed = form.querySelector<HTMLInputElement>('input[name="confirmed"]')
  if (confirmed) confirmed.value = 'yes'
  removeDialog?.close()
  form.submit()
})
