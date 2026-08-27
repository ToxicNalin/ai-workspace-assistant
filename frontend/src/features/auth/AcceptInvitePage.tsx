import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import * as api from "../../api/endpoints";
import { Banner, Field, Spinner } from "../../components/ui";
import { errorMessage } from "../../lib/useAsync";
import { useAuth } from "./AuthContext";

/**
 * Redeeming an invite token.
 *
 * The token arrives either in the link (`/join?token=…`) or pasted by hand.
 * It is a bearer credential — the server stores only its SHA-256 (SPEC-v2 D6)
 * — so it is never written anywhere on this side either: it goes from the URL
 * into a form field into one request, and the URL is replaced afterwards so
 * it does not sit in the address bar or the browser's history.
 */
export function AcceptInvitePage() {
  const { status } = useAuth();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const [token, setToken] = useState(() => params.get("token") ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Take the token out of the address bar as soon as it has been read into
  // state. `replace` so the version carrying it does not stay in history
  // either — a credential in a URL survives a shared screen and a shoulder.
  useEffect(() => {
    if (!params.has("token")) return;
    const stripped = new URLSearchParams(params);
    stripped.delete("token");
    setParams(stripped, { replace: true });
  }, [params, setParams]);

  if (status === "anonymous") {
    return (
      <div className="auth">
        <div className="auth__card">
          <h1 className="auth__title">Redeem an invite</h1>
          <p className="auth__lede">
            An invite joins <em>your account</em> to a workspace, so sign in or create an
            account first. Your invite link will still work afterwards.
          </p>
          <Link className="button button--primary" to="/login">
            Sign in
          </Link>
          <p className="auth__alt">
            No account yet? <Link to="/register">Create one</Link>
          </p>
        </div>
      </div>
    );
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const membership = await api.acceptInvite(token.trim());
      navigate("/", { replace: true, state: { joined: membership.id } });
    } catch (caught) {
      setError(errorMessage(caught));
      setBusy(false);
    }
  }

  return (
    <div className="auth">
      <div className="auth__card">
        <h1 className="auth__title">Redeem an invite</h1>

        {error ? <Banner onDismiss={() => setError(null)}>{error}</Banner> : null}

        <form onSubmit={submit} className="stack">
          <Field
            label="Invite token"
            hint="The long string from the invite link. It can only be redeemed once."
          >
            <input
              value={token}
              onChange={(event) => setToken(event.target.value)}
              required
              autoFocus={token === ""}
              spellCheck={false}
            />
          </Field>
          <button
            type="submit"
            className="button button--primary"
            disabled={busy || token.trim() === ""}
          >
            {busy ? <Spinner label="Joining" /> : "Join workspace"}
          </button>
        </form>

        <p className="auth__alt">
          <Link to="/">Back to your workspaces</Link>
        </p>
      </div>
    </div>
  );
}
