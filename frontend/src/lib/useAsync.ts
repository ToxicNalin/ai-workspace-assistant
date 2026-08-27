import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/client";

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.isRateLimited) {
      const wait = error.retryAfter;
      return wait === null ? error.message : `${error.message} Try again in ${wait}s.`;
    }
    return error.message;
  }
  if (error instanceof Error) {
    // A fetch that never reached the server. Worth naming explicitly, because
    // on a free tier the most likely cause is a service still waking up.
    return error.name === "TypeError"
      ? "Could not reach the API. It may still be waking from a cold start — try again in a moment."
      : error.message;
  }
  return "Something went wrong.";
}

export interface Async<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** Re-run the loader. Keeps the current data on screen while it runs. */
  reload: () => void;
  /** Apply a local change without a round trip. */
  set: (next: T) => void;
}

/**
 * Load something, once per dependency change, and survive being unmounted.
 *
 * The generation counter is the whole of the interesting part: switching
 * workspace twice quickly starts two loads, and without it the slower one can
 * land last and paint the workspace you have already left.
 */
export function useAsync<T>(load: () => Promise<T>, deps: readonly unknown[]): Async<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const generation = useRef(0);

  // Deliberately not a dependency below. The caller passes an inline arrow
  // function, so it is a new value on every render; depending on it would
  // reload forever. `deps` is what the caller declares the load depends on.
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    const mine = ++generation.current;
    setLoading(true);

    void loadRef
      .current()
      .then((result) => {
        if (generation.current !== mine) return;
        setData(result);
        setError(null);
      })
      .catch((caught: unknown) => {
        if (generation.current !== mine) return;
        setError(errorMessage(caught));
      })
      .finally(() => {
        if (generation.current === mine) setLoading(false);
      });

    return () => {
      // Abandon this load's results; a later one owns the state now.
      generation.current++;
    };
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);
  const set = useCallback((next: T) => setData(next), []);

  return { data, error, loading, reload, set };
}
