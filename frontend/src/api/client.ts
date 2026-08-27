/**
 * The one place this application talks to the API.
 *
 * SPEC-v2 D19 decided where the two credentials live, and this file is the
 * consequence:
 *
 * - The **access token** is a module variable. Not localStorage, not a cookie
 *   — it dies with the tab, which is the point. Fifteen minutes of exposure
 *   beats thirty days of it.
 * - The **refresh token** is an httpOnly cookie this code cannot read, set by
 *   the API and attached by the browser. Every request therefore goes out with
 *   `credentials: "include"`.
 * - The **CSRF token** is the third piece, and the only one that has to
 *   survive a reload. A reload wipes the access token; without a CSRF token
 *   the refresh cookie is unusable and the session is lost for no reason. It
 *   lives in localStorage, which is safe here for a reason worth stating: the
 *   thing CSRF defends against is *another origin* acting through the
 *   browser, and another origin cannot read this origin's localStorage. It is
 *   also useless on its own — without the cookie it authorises nothing.
 *
 * On a 401 the wrapper refreshes once, silently, and replays the request.
 * Concurrent 401s share a single refresh: five requests racing after a token
 * expires must not fire five rotations, because rotation invalidates the
 * previous token and four of them would then fail with the session destroyed.
 */

const CONFIGURED_API_URL: string | undefined = import.meta.env.VITE_API_URL;
const BASE_URL: string = (CONFIGURED_API_URL ?? "http://localhost:8000").replace(/\/+$/, "");

if (import.meta.env.PROD && CONFIGURED_API_URL === undefined) {
  // Vite inlines this at build time, so an unset variable in Cloudflare Pages'
  // build environment ships a bundle pointing at the reviewer's own machine.
  // Loud, because the symptom otherwise is a connection error with no clue
  // attached — and not a thrown error, because a blank page says even less.
  console.error(
    "VITE_API_URL was not set at build time; falling back to http://localhost:8000. " +
      "Set it in the Cloudflare Pages build environment and redeploy.",
  );
}

const CSRF_STORAGE_KEY = "aiwa.csrf";

let accessToken: string | null = null;

/** Notified when the session appears or disappears, so React can re-render. */
type SessionListener = (authenticated: boolean) => void;
const listeners = new Set<SessionListener>();

export function onSessionChange(listener: SessionListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function announce(authenticated: boolean): void {
  for (const listener of listeners) listener(authenticated);
}

function readCsrfToken(): string | null {
  try {
    return window.localStorage.getItem(CSRF_STORAGE_KEY);
  } catch {
    // Private browsing modes and blocked site data both throw here. The
    // session simply will not survive a reload, which is a degradation
    // rather than a failure.
    return null;
  }
}

function writeCsrfToken(token: string | null): void {
  try {
    if (token === null) window.localStorage.removeItem(CSRF_STORAGE_KEY);
    else window.localStorage.setItem(CSRF_STORAGE_KEY, token);
  } catch {
    /* see readCsrfToken */
  }
}

export function setSession(session: { access_token: string; csrf_token: string }): void {
  accessToken = session.access_token;
  writeCsrfToken(session.csrf_token);
  announce(true);
}

export function clearSession(): void {
  accessToken = null;
  writeCsrfToken(null);
  announce(false);
}

/**
 * Whether a reload has any chance of restoring the session.
 *
 * The refresh cookie itself is unreadable, so this is the only signal
 * available: a CSRF token means a session existed and was not logged out.
 * Wrong occasionally — the cookie may have expired — which costs one failed
 * refresh, not a wrong screen.
 */
export function mightHaveSession(): boolean {
  return readCsrfToken() !== null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly retryAfter: number | null;

  constructor(status: number, detail: string, retryAfter: number | null = null) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.retryAfter = retryAfter;
  }

  /** Rate limited, or the workspace has spent its daily token budget (D22). */
  get isRateLimited(): boolean {
    return this.status === 429;
  }
}

async function toApiError(response: Response): Promise<ApiError> {
  let detail = response.statusText || `Request failed (${response.status})`;
  try {
    const body: unknown = await response.json();
    if (body !== null && typeof body === "object" && "detail" in body) {
      const value = (body as { detail: unknown }).detail;
      // FastAPI's own 422 puts a list of field errors here, where the app's
      // error envelope puts a sentence.
      if (typeof value === "string") detail = value;
      else if (Array.isArray(value) && value.length > 0) detail = describeValidationError(value);
    }
  } catch {
    /* not JSON; the status line stands */
  }

  const retryAfter = Number(response.headers.get("Retry-After"));
  return new ApiError(response.status, detail, Number.isFinite(retryAfter) ? retryAfter : null);
}

function describeValidationError(errors: unknown[]): string {
  const first = errors[0];
  if (first !== null && typeof first === "object" && "msg" in first) {
    const location =
      "loc" in first && Array.isArray((first as { loc: unknown[] }).loc)
        ? (first as { loc: unknown[] }).loc.slice(-1)[0]
        : null;
    const message = String((first as { msg: unknown }).msg);
    return location ? `${String(location)}: ${message}` : message;
  }
  return "That request was not valid.";
}

/** In flight, or null. Shared so concurrent 401s cause one rotation. */
let refreshInFlight: Promise<boolean> | null = null;

async function performRefresh(): Promise<boolean> {
  const csrf = readCsrfToken();
  if (csrf === null) return false;

  const response = await fetch(`${BASE_URL}/auth/refresh`, {
    method: "POST",
    credentials: "include",
    headers: { "X-CSRF-Token": csrf },
  });

  if (!response.ok) {
    clearSession();
    return false;
  }

  const session = (await response.json()) as { access_token: string; csrf_token: string };
  setSession(session);
  return true;
}

export function refreshSession(): Promise<boolean> {
  refreshInFlight ??= performRefresh().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

export interface RequestOptions {
  method?: string;
  /** Serialised as JSON unless it is already FormData. */
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  signal?: AbortSignal;
  /** Skip the refresh-and-replay dance. Used by the login call itself. */
  anonymous?: boolean;
}

function buildUrl(path: string, query: RequestOptions["query"]): string {
  const url = new URL(BASE_URL + path);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
  }
  return url.toString();
}

function send(path: string, options: RequestOptions): Promise<Response> {
  const headers = new Headers();
  // Read at call time, not at request-construction time: after a refresh the
  // replay has to go out with the *new* token.
  if (accessToken !== null) headers.set("Authorization", `Bearer ${accessToken}`);

  let body: BodyInit | undefined;
  if (options.body instanceof FormData) {
    // Deliberately no Content-Type. The browser has to set it so it can add
    // the multipart boundary, and naming it here would produce a body the
    // server cannot parse.
    body = options.body;
  } else if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(options.body);
  }

  const init: RequestInit = {
    method: options.method ?? "GET",
    headers,
    credentials: "include",
  };
  if (body !== undefined) init.body = body;
  if (options.signal !== undefined) init.signal = options.signal;

  return fetch(buildUrl(path, options.query), init);
}

/**
 * A request, with one silent refresh-and-replay on a 401.
 *
 * Once, and only once — a second 401 after a successful refresh means the
 * token was not the problem, and retrying again would be a loop.
 */
export async function requestRaw(path: string, options: RequestOptions = {}): Promise<Response> {
  let response = await send(path, options);

  if (response.status === 401 && options.anonymous !== true) {
    if (await refreshSession()) {
      response = await send(path, options);
    } else {
      clearSession();
    }
  }

  if (!response.ok) throw await toApiError(response);
  return response;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await requestRaw(path, options);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
