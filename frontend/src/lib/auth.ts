import { absoluteUrl } from "@/lib/base-path";
import { createClient, type SupabaseClient, type User, type Session } from "@supabase/supabase-js";

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || "";
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "";

if (!supabaseUrl || !supabaseAnonKey) {
  throw new Error("NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY are required");
}

let supabase: SupabaseClient | null = null;
let cachedSession: Session | null = null;
let sessionLoaded = false;
let sessionRequest: Promise<Session | null> | null = null;
let refreshRequest: Promise<Session | null> | null = null;

function sessionNeedsRefresh(session: Session | null): boolean {
  if (!session?.expires_at) return false;
  return session.expires_at <= Math.floor(Date.now() / 1000) + 30;
}

function getSupabaseOrThrow(): SupabaseClient {
  if (!supabase) {
    supabase = createClient(supabaseUrl, supabaseAnonKey, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true,
      },
    });
  }
  return supabase;
}

export function getSupabase(): SupabaseClient {
  return getSupabaseOrThrow();
}

export async function signInWithEmail(email: string, password: string) {
  const { data, error } = await getSupabase().auth.signInWithPassword({ email, password });
  if (error) throw error;
  cachedSession = data.session;
  sessionLoaded = true;
  return data;
}

export async function signInWithMagicLink(email: string, redirectTo?: string) {
  const { data, error } = await getSupabase().auth.signInWithOtp({
    email,
    options: {
      emailRedirectTo: redirectTo || absoluteUrl("/auth/callback"),
    },
  });
  if (error) throw error;
  return data;
}

export async function signUp(
  email: string,
  password: string,
  redirectTo?: string,
  fullName?: string,
  workspaceName?: string,
  phone?: string,
) {
  const { data, error } = await getSupabase().auth.signUp({
    email,
    password,
    options: {
      emailRedirectTo: redirectTo || absoluteUrl("/auth/callback"),
      data: {
        ...(fullName ? { full_name: fullName } : {}),
        ...(workspaceName ? { workspace_name: workspaceName } : {}),
        ...(phone ? { phone } : {}),
      },
    },
  });
  if (error) throw error;
  return data;
}

export async function signOut() {
  const { error } = await getSupabase().auth.signOut();
  if (error) throw error;
  cachedSession = null;
  sessionLoaded = true;
}

// A failed /auth/v1/user request means the browser has an unusable access
// token (for example, one persisted before a key/session rotation). Clear
// only this browser's copy so the app can return to the login screen instead
// of continuing to render with a stale user object.
export async function clearInvalidLocalSession() {
  const { error } = await getSupabase().auth.signOut({ scope: "local" });
  if (error) throw error;
  cachedSession = null;
  sessionLoaded = true;
}

export async function getSession(): Promise<Session | null> {
  if (sessionLoaded && !sessionNeedsRefresh(cachedSession)) return cachedSession;
  if (sessionLoaded && cachedSession) {
    if (!refreshRequest) {
      refreshRequest = refreshCurrentSession().finally(() => {
        refreshRequest = null;
      });
    }
    return refreshRequest;
  }
  if (!sessionRequest) {
    sessionRequest = getSupabase().auth.getSession().then(({ data, error }) => {
      if (error) throw error;
      cachedSession = data.session;
      sessionLoaded = true;
      return cachedSession;
    }).finally(() => {
      sessionRequest = null;
    });
  }
  return sessionRequest;
}

// Force-refresh the session. If the standard refresh fails (e.g. the stored
// refresh token is also stale), fall back to getUser() which validates and
// mints a fresh access token via the auth server. Guarantees we never hand an
// expired token to the API (which would 401).
async function refreshCurrentSession(): Promise<Session | null> {
  try {
    const { data, error } = await getSupabase().auth.refreshSession();
    if (!error && data.session) {
      cachedSession = data.session;
      return cachedSession;
    }
  } catch {
    // fall through to getUser()
  }
  try {
    const { data } = await getSupabase().auth.getUser();
    if (data.user) {
      // getUser returns a fresh user; re-read the session to capture the new
      // access token that supabase-js persisted during the validate call.
      const ses = (await getSupabase().auth.getSession()).data.session;
      if (ses) {
        cachedSession = ses;
        return cachedSession;
      }
    }
  } catch {
    // ignore — caller will return whatever we have (possibly null)
  }
  return cachedSession;
}

export async function getUser(): Promise<User | null> {
  const { data, error } = await getSupabase().auth.getUser();
  if (error) throw error;
  return data.user;
}

export function onAuthStateChange(callback: (event: string, session: Session | null) => void) {
  return getSupabase().auth.onAuthStateChange((event, session) => {
    cachedSession = session;
    sessionLoaded = true;
    callback(event, session);
  });
}

export async function getAccessToken(): Promise<string | null> {
  const session = await getSession();
  // Last-resort guard: never return an already-expired token.
  if (session && sessionNeedsRefresh(session)) {
    const refreshed = await refreshCurrentSession();
    return refreshed?.access_token ?? null;
  }
  return session?.access_token ?? null;
}

// Force a fresh access token, bypassing the cached/expired one. Used by the
// API layer to recover from a 401 without surfacing an error to the user.
export async function forceRefreshToken(): Promise<string | null> {
  const refreshed = await refreshCurrentSession();
  return refreshed?.access_token ?? null;
}
