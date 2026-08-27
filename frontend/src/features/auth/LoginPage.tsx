import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Banner, Field, Spinner } from "../../components/ui";
import { errorMessage } from "../../lib/useAsync";
import { useAuth } from "./AuthContext";
import { ColdStartNotice } from "./ColdStartNotice";

export function LoginPage() {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(email, password);
      navigate("/", { replace: true });
    } catch (caught) {
      setError(errorMessage(caught));
      setBusy(false);
    }
  }

  return (
    <div className="auth">
      <div className="auth__card">
        <h1 className="auth__title">Sign in</h1>
        <p className="auth__lede">
          A multi-tenant workspace assistant whose agent cannot send anything without a
          human saying so.
        </p>

        {error ? <Banner onDismiss={() => setError(null)}>{error}</Banner> : null}

        <form onSubmit={submit} className="stack">
          <Field label="Email">
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="email"
              required
              autoFocus
            />
          </Field>
          <Field label="Password">
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
              required
            />
          </Field>
          <button type="submit" className="button button--primary" disabled={busy}>
            {busy ? <Spinner label="Signing in" /> : "Sign in"}
          </button>
        </form>

        <p className="auth__alt">
          No account yet? <Link to="/register">Create one</Link>
        </p>
        <p className="auth__alt">
          Been invited? <Link to="/join">Redeem an invite</Link>
        </p>

        <ColdStartNotice />
      </div>
    </div>
  );
}
