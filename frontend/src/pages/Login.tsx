import { useState, type FormEvent } from 'react'
import { post } from '../api'
import { Button, Card, ErrorNote, Field } from '../components/ui'

// Codes the Google callback puts in ?login_error= (see GoogleSignInError in backend/src/ledger/auth.py).
const GOOGLE_ERRORS: Record<string, string> = {
  not_allowed: 'That Google account isn’t allowed to sign in. Add its email to a household member and tick Can sign in.',
  ambiguous: 'More than one household member signs in with that email.',
  unverified: 'Google hasn’t verified that account’s email address.',
  cancelled: 'Google sign-in was cancelled.',
  expired: 'The Google sign-in expired or was started in another tab. Try again.',
  google_failed: 'Google sign-in didn’t complete. Try again.',
  not_configured: 'Google sign-in isn’t set up on this server.',
}

function takeLoginError(): string | null {
  const params = new URLSearchParams(window.location.search)
  const code = params.get('login_error')
  if (!code) return null
  params.delete('login_error')
  const qs = params.toString()
  window.history.replaceState(null, '', window.location.pathname + (qs ? `?${qs}` : ''))
  return GOOGLE_ERRORS[code] ?? 'Google sign-in failed.'
}

export function Login({ onLogin, google }: { onLogin: () => void; google: boolean }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState<unknown>(takeLoginError)
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await post('/auth/login', { password })
      onLogin()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <form onSubmit={submit}>
        <Card className="login-card">
          <div className="brand" style={{ padding: 0 }}>
            <span className="brand-name">Ledger</span>
            <span className="brand-sub">Household</span>
          </div>
          {google && (
            <>
              <a className="btn btn-secondary" href="/api/auth/google/start">Sign in with Google</a>
              <div className="login-or text-muted">or</div>
            </>
          )}
          <Field label="Password">
            <input className="input" type="password" autoFocus={!google} autoComplete="current-password" value={password}
              onChange={(e) => setPassword(e.target.value)} />
          </Field>
          <ErrorNote error={error} />
          <Button variant="primary" type="submit" disabled={busy || !password}>Sign in</Button>
        </Card>
      </form>
    </div>
  )
}
