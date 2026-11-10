export class InvitationRequestError extends Error {
  constructor(message: string, readonly invalidEmail = false) { super(message) }
}

const UNCERTAIN_REQUEST = "We couldn't confirm your request. Please try again."

export async function requestInvitation(email: string): Promise<void> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 15_000)
  try {
    const response = await fetch('/api/invitation-requests', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email }),
      signal: controller.signal,
    })
    const data = await response.json()
    if (response.status !== 202 || typeof data?.message !== 'string') {
      const message = response.status !== 202 && typeof data?.error === 'string' ? data.error : UNCERTAIN_REQUEST
      throw new InvitationRequestError(message, response.status === 422)
    }
  } catch (error) {
    if (error instanceof InvitationRequestError) throw error
    throw new InvitationRequestError(UNCERTAIN_REQUEST)
  } finally {
    clearTimeout(timeout)
  }
}
