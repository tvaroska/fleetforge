import type { UserSummary } from '../api'

export type PendingConfirm =
  | { kind: 'remove-member'; username: string }
  | { kind: 'revoke-token'; id: string; name: string }
  | { kind: 'disable-user'; username: string }
  | { kind: 'delete-user'; username: string }
  | { kind: 'delete-secret'; key: string }
  | { kind: 'delete-group'; id: string; name: string }

export function emailDeliveryError(
  delivery: UserSummary['email_delivery'],
  failedMessage: string,
): string | null {
  if (delivery === 'failed') {
    return failedMessage
  }
  if (delivery === 'not_configured') {
    return 'Firebase is not configured on the server; no email was sent.'
  }
  return null
}
