import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { clearSession, mightHaveSession, onSessionChange, refreshSession } from "../../api/client";
import * as api from "../../api/endpoints";
import type { User } from "../../api/types";

type Status = "restoring" | "authenticated" | "anonymous";

interface AuthValue {
  status: Status;
  user: User | null;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthValue | null>(null);

/**
 * Owns "who is signed in", and rebuilds it after a reload.
 *
 * A reload wipes the access token — it lives in a module variable on purpose
 * (SPEC-v2 D19) — but not the refresh cookie. So the first thing this does is
 * try one silent refresh. Until that settles the app renders neither the
 * signed-in shell nor the login page: showing "log in" to somebody who is
 * still logged in, for the half second the round trip takes, is a worse
 * mistake than a spinner.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>(() =>
    mightHaveSession() ? "restoring" : "anonymous",
  );
  const [user, setUser] = useState<User | null>(null);

  useEffect(() => {
    if (status !== "restoring") return;

    let cancelled = false;
    void (async () => {
      const restored = await refreshSession();
      if (cancelled) return;
      if (!restored) {
        setStatus("anonymous");
        return;
      }
      try {
        setUser(await api.me());
        if (!cancelled) setStatus("authenticated");
      } catch {
        if (!cancelled) {
          clearSession();
          setStatus("anonymous");
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [status]);

  // The client clears the session by itself when a refresh fails mid-session,
  // which can happen while any screen is open. Without this the app would
  // keep rendering a workspace whose every request now 401s.
  useEffect(
    () =>
      onSessionChange((authenticated) => {
        if (!authenticated) {
          setUser(null);
          setStatus("anonymous");
        }
      }),
    [],
  );

  const signIn = useCallback(async (email: string, password: string) => {
    await api.login(email, password);
    setUser(await api.me());
    setStatus("authenticated");
  }, []);

  const signOut = useCallback(async () => {
    await api.logout();
    setUser(null);
    setStatus("anonymous");
  }, []);

  const value = useMemo<AuthValue>(
    () => ({ status, user, signIn, signOut }),
    [status, user, signIn, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (value === null) throw new Error("useAuth must be used inside an AuthProvider");
  return value;
}
