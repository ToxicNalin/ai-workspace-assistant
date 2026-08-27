import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import * as api from "../../api/endpoints";
import { Banner, Field, Spinner } from "../../components/ui";
import { errorMessage } from "../../lib/useAsync";
import { useAuth } from "./AuthContext";
import { ColdStartNotice } from "./ColdStartNotice";

export function RegisterPage() {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.register(email, password, name);
      // Straight in. Registering and then being shown a login form is a
      // pointless second act, and the credentials are already to hand.
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
        <h1 className="auth__title">Create an account</h1>

        {error ? <Banner onDismiss={() => setError(null)}>{error}</Banner> : null}

        <form onSubmit={submit} className="stack">
          <Field label="Name">
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              autoComplete="name"
              required
              autoFocus
            />
          </Field>
          <Field label="Email">
            <input
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="email"
              required
            />
          </Field>
          <Field
            label="Password"
            hint="At least 8 characters. Capped at 72 — bcrypt ignores anything beyond that, and a password that silently half-works is worse than one that is refused."
          >
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="new-password"
              minLength={8}
              maxLength={72}
              required
            />
          </Field>
          <button type="submit" className="button button--primary" disabled={busy}>
            {busy ? <Spinner label="Creating account" /> : "Create account"}
          </button>
        </form>

        <p className="auth__alt">
          Already registered? <Link to="/login">Sign in</Link>
        </p>

        <ColdStartNotice />
      </div>
    </div>
  );
}
