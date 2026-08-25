"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { withBasePath } from "@/lib/base-path";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import "./globals.css";
import { getPhones, searchMessages, getAuthMe, getBusinessApiConfig, BusinessApiConfig, getProfile, getWhatsAppStatus, fetchJSON, isLiveWhatsAppConnection, getSoundPreferences as getSavedSoundPreferences, saveSoundPreferences, type Phone, type WhatsAppStatus } from "@/lib/api";
import {
  MessageSquare,
  BarChart3,
  Search,
  Briefcase,
  Wifi,
  WifiOff,
  UserCheck,
  UserCog,
  Users,
  BookOpen,
  MapPin,
  GraduationCap,
  Radar,
  TrendingUp,
  Key,
  LogOut,
  Menu,
  ShieldCheck,
  X,
  Zap,
  Sparkles,
  Volume2,
  VolumeX,
  SlidersHorizontal,
  Bell,
  RefreshCw,
  Megaphone,
  BrainCircuit,
  Wrench,
} from "lucide-react";
import { AuthProvider, useAuth } from "@/lib/AuthProvider";
import { LayoutProvider, useLayout } from "@/hooks/useLayout";
import { useEventStream } from "@/lib/useEventStream";
import { BottomNav } from "@/components/layout/BottomNav";
import { MobileDrawer } from "@/components/layout/MobileDrawer";
import { InstallPrompt } from "@/components/layout/InstallPrompt";
import { ServiceWorkerRegister } from "@/components/layout/ServiceWorkerRegister";
import { isMuted, toggleMute, playConnectionChange, playGroupConnected, playNewLead, playNewWhatsApp, getVolume, setVolume, isSoundEnabled, setSoundEnabled, getSoundPreferences, loadSoundPreferences, setSoundPreference, previewSound, SOUND_LIBRARY, type SoundEvent, type SoundId, type SoundPreferences } from "@/lib/sounds";
import { ThemeProvider } from "@/components/ThemeProvider";
import { getBuildHint, getBuildLabel } from "@/lib/buildInfo";

type NavItem = {
  href: string;
  label: string;
  icon?: any;
  external?: boolean;
  children?: { href: string; label: string }[];
};

type NotificationNotice = {
  title: string;
  detail: string;
};

const baseNavSections = [
  {
    title: "Market",
    items: [
      { href: "/chat", label: "Search & Chat", icon: Search },
      { href: "/inbox", label: "Market Inbox", icon: MessageSquare },
      { href: "/whatsapp?tab=numbers", label: "WhatsApp", icon: Wifi },
      { href: "/brokers", label: "Broker Profiles", icon: Users },
    ],
  },
  {
    title: "Workspace",
    items: [
      { href: "/clients", label: "My Clients", icon: UserCheck },
      { href: "/deals", label: "My Deals", icon: TrendingUp },
    ],
  },
  {
    title: "Growth",
    items: [
      { href: "/social-flow", label: "Realtor Ads Studio", icon: Megaphone },
      { href: "https://automations.propai.live", label: "Automations", icon: Zap, external: true },
    ],
  },
  {
    title: "",
    items: [
      { href: "/account?tab=profile", label: "Account", icon: UserCheck },
      { href: "/reports?tab=usage", label: "Reports", icon: BarChart3 },
    ],
  },
];

const adminNavSection = {
  title: "",
  items: [
    { href: "/admin", label: "Super Admin", icon: ShieldCheck },
    { href: "/admin/pipeline-health?tab=providers", label: "Pipeline Health", icon: Sparkles },
  ],
};

function PaletteModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Record<string, any[]> | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState(false);
  const [selectedIdx, setSelectedIdx] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const router = useRouter();

  const flatItems = results ? Object.values(results).flat() : [];

  useEffect(() => {
    if (open) {
      setQuery("");
      setResults(null);
      setSearching(false);
      setSearchError(false);
      setSelectedIdx(0);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [open]);

  useEffect(() => {
    if (!query.trim()) {
      setResults(null);
      setSearching(false);
      setSearchError(false);
      return;
    }
    const controller = new AbortController();
    const t = setTimeout(async () => {
      setSearching(true);
      setSearchError(false);
      try {
        const data = await searchMessages(query, controller.signal);
        setResults(data);
        setSelectedIdx(0);
      } catch (error) {
        if (!controller.signal.aborted) {
          setResults({});
          setSearchError(true);
        }
      } finally {
        if (!controller.signal.aborted) setSearching(false);
      }
    }, 200);
    return () => {
      clearTimeout(t);
      controller.abort();
    };
  }, [query]);

  function navigate(path: string) {
    onClose();
    router.push(path);
  }

  function onKeyDown(e: React.KeyboardEvent) {
    const total = flatItems.length;
    if (e.key === "ArrowDown") { e.preventDefault(); setSelectedIdx(i => Math.min(i + 1, total - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setSelectedIdx(i => Math.max(i - 1, 0)); }
    else if (e.key === "Enter") {
      const r = flatItems[selectedIdx];
      if (!r) return;
      if (r.name && r.occurrence_count !== undefined) navigate("/chat");
      else if (r.name && r.observation_count !== undefined) navigate(`/brokers?q=${encodeURIComponent(r.name)}`);
      else if (r.micro_market && !r.broker_name) navigate(`/market?q=${encodeURIComponent(r.micro_market)}`);
      else if (r.building_name) navigate("/chat");
      else if (r.broker_name) navigate(`/brokers?q=${encodeURIComponent(r.broker_name)}`);
    }
    else if (e.key === "Escape") onClose();
  }

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[1000] flex items-start justify-center pt-[15vh] bg-black/50 backdrop-blur-sm" onClick={onClose}>
      <div className="w-full max-w-lg mx-4 bg-zinc-900 border border-white/10 rounded-2xl shadow-2xl overflow-hidden" onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 px-4 py-3 border-b border-white/5">
          <Search className="w-4 h-4 text-zinc-500 shrink-0" />
          <input
            ref={inputRef}
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Search properties, brokers, buildings..."
            className="flex-1 bg-transparent text-sm text-white placeholder-zinc-500 outline-none min-w-0"
          />
          <kbd className="text-[10px] text-zinc-500 bg-white/5 px-1.5 py-0.5 rounded shrink-0">ESC</kbd>
        </div>
        {searching && <div className="px-4 py-3 text-xs text-zinc-500">Searching live inventory…</div>}
        {searchError && <div className="px-4 py-3 text-xs text-red-400">Search is temporarily unavailable. Try again.</div>}
        {results && (
          <div className="max-h-80 overflow-y-auto py-2">
            {Object.entries(results).map(([group, items]) => (
              <div key={group}>
                <div className="px-4 py-1.5 text-[10px] font-bold text-zinc-500 uppercase tracking-wider">{group}</div>
                {items.map((item: any, i: number) => {
                  const globalIdx = flatItems.indexOf(item);
                  const isSelected = globalIdx === selectedIdx;
                  return (
                    <button
                      key={i}
                      onClick={() => {
                        if (item.name && item.occurrence_count !== undefined) navigate("/chat");
                        else if (item.micro_market) navigate(`/market?q=${encodeURIComponent(item.micro_market)}`);
                        else if (item.building_name) navigate("/chat");
                        else if (item.broker_name) navigate(`/brokers?q=${encodeURIComponent(item.broker_name)}`);
                      }}
                      className={`flex w-full items-center gap-3 px-4 py-2 text-left text-sm ${isSelected ? "bg-white/5 text-white" : "text-zinc-400 hover:bg-white/5"}`}
                    >
                      <span className="text-xs text-zinc-500 w-4 text-right shrink-0">{globalIdx + 1}</span>
                      <span className="truncate">{item.name || item.micro_market || item.building_name || item.broker_name}</span>
                    </button>
                  );
                })}
              </div>
            ))}
            {flatItems.length === 0 && !searching && !searchError && (
              <div className="px-4 py-8 text-center text-sm text-zinc-500">No results found</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isFocusedWorkspace = pathname === "/inbox";
  const router = useRouter();
  const { user, loading: authLoading, error: authError, refresh: refreshAuth } = useAuth();
  const { drawerOpen, setDrawerOpen, toggleDrawer, setLastTab } = useLayout();
  const [phones, setPhones] = useState<Phone[]>([]);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [offline, setOffline] = useState(false);
  const [profile, setProfile] = useState<{ auth_user_id?: string; phone: string; first_name: string; last_name?: string; email?: string; city?: string } | null>(null);
  const [wabaConfig, setWabaConfig] = useState<BusinessApiConfig | null>(null);
  const [liveStatus, setLiveStatus] = useState<WhatsAppStatus | null>(null);
  const [extractionHealth, setExtractionHealth] = useState<{ pending: number; recentlyProcessed1h: number } | null>(null);
  const [disconnectNoticeOpen, setDisconnectNoticeOpen] = useState(false);
  const previousWhatsAppState = useRef<boolean | null>(null);
  const lastIncomingSoundAt = useRef(0);
  const lastGroupSoundAt = useRef(0);
  const [isSuperAdmin, setIsSuperAdmin] = useState(false);
  const [soundsMuted, setSoundsMuted] = useState(true);
  const [soundSettingsOpen, setSoundSettingsOpen] = useState(false);
  const [mobileStatusExpanded, setMobileStatusExpanded] = useState(false);
  const [soundVolume, setSoundVolume] = useState(0.25);
  const [soundEvents, setSoundEvents] = useState<Record<SoundEvent, boolean>>({ whatsapp: true, groups: false, connection: true, leads: true });
  const [soundPreferences, setSoundPreferences] = useState<SoundPreferences>({ whatsapp: "chime", groups: "pop", connection: "bell", leads: "soft-ding" });
  const [notificationNotice, setNotificationNotice] = useState<NotificationNotice | null>(null);
  const notificationTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { signOut: authSignOut } = useAuth();
  const fallbackFullName = String(user?.user_metadata?.full_name || user?.email?.split("@")[0] || "Account").trim();
  const [fallbackFirstName = "Account", ...fallbackLastName] = fallbackFullName.split(/\s+/);
  const profileIdentity = profile || {
    first_name: fallbackFirstName,
    last_name: fallbackLastName.join(" "),
    city: "",
  };

  // Read profile from localStorage; if missing, try to hydrate from server
  useEffect(() => {
    const readProfile = () => {
      const s = localStorage.getItem("propai_profile");
      if (s) {
        try {
          const parsed = JSON.parse(s);
          setProfile(parsed?.auth_user_id === user?.id ? parsed : null);
        } catch {
          setProfile(null);
        }
      } else {
        setProfile(null);
      }
    };
    readProfile();
    window.addEventListener("storage", readProfile);
    window.addEventListener("propai_profile_updated", readProfile);
    return () => {
      window.removeEventListener("storage", readProfile);
      window.removeEventListener("propai_profile_updated", readProfile);
    };
  }, [user?.id]);

  // Read local sound state immediately, then hydrate the selected sounds from
  // the authenticated profile so choices survive a new browser/session.
  useEffect(() => {
    import("@/lib/sounds").then((s) => setSoundsMuted(s.isMuted()));
    import("@/lib/sounds").then((s) => {
      setSoundVolume(s.getVolume());
      setSoundEvents({
        whatsapp: s.isSoundEnabled("whatsapp"),
        groups: s.isSoundEnabled("groups"),
        connection: s.isSoundEnabled("connection"),
        leads: s.isSoundEnabled("leads"),
      });
      setSoundPreferences(s.getSoundPreferences());
    });
    if (user?.id) {
      void getSavedSoundPreferences()
        .then((saved) => {
          const next = loadSoundPreferences(saved as Partial<SoundPreferences>);
          setSoundPreferences(next);
        })
        .catch(() => undefined);
    }
  }, [user?.id]);

  const handleToggleSounds = useCallback(() => {
    import("@/lib/sounds").then((s) => {
      s.toggleMute();
      setSoundsMuted(s.isMuted());
    });
  }, []);

  const handleSoundEvent = useCallback((event: SoundEvent) => {
    const enabled = setSoundEnabled(event, !soundEvents[event]);
    setSoundEvents((current) => ({ ...current, [event]: enabled }));
  }, [soundEvents]);

  const handleSoundSelection = useCallback((event: SoundEvent, sound: SoundId) => {
    const next = setSoundPreference(event, sound);
    setSoundPreferences(next);
    previewSound(sound);
    void saveSoundPreferences(next).catch(() => undefined);
  }, []);

  // Hydrate localStorage profile from server when missing
  useEffect(() => {
    if (!user || profile) return;
    const phone = user.phone || "";
    if (!phone && !user.id) return;
    let cancelled = false;
    getProfile().then((data: any) => {
      if (cancelled) return;
      if (data && data.first_name) {
        const hydrated = {
          auth_user_id: user.id,
          phone: data.phone || phone,
          first_name: data.first_name || "",
          last_name: data.last_name || "",
          email: data.email || user.email || "",
          city: data.city || "",
        };
        localStorage.setItem("propai_profile", JSON.stringify(hydrated));
        window.dispatchEvent(new Event("propai_profile_updated"));
      }
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [user, profile]);

  // Cached phone rows are useful for identity, but must never be treated as
  // proof of a live WhatsApp session when the live probe was unavailable.
  const livePhone = phones.find((phone) => phone.live_status_available === true && isLiveWhatsAppConnection(phone)) || null;
  const displayPhone = livePhone || phones[0] || (liveStatus?.phone ? ({ phone_number_live: liveStatus.phone } as Phone) : null);
  const hasConfiguredWhatsApp = phones.length > 0;
  const hasPerPhoneLiveStatus = phones.some((phone) => phone.live_status_available === true);
  const waConnected = livePhone
    ? true
    : hasPerPhoneLiveStatus
      ? false
      : liveStatus
        ? isLiveWhatsAppConnection(liveStatus)
        : null;
  const waStale = Boolean(liveStatus?.status_stale || livePhone?.status_stale);
  const waPhone = displayPhone?.phone_number_live || displayPhone?.phone_number || "";
  const extractionStalled = Boolean(
    extractionHealth &&
    extractionHealth.pending > 0 &&
    extractionHealth.recentlyProcessed1h === 0,
  );
  const whatsappHealth: "checking" | "healthy" | "warning" | "error" =
    waConnected === null
      ? "checking"
      : !hasConfiguredWhatsApp
        ? "warning"
        : !waConnected
          ? "error"
          : waStale
            ? "warning"
            : "healthy";
  const extractionHealthState: "checking" | "healthy" | "warning" =
    extractionHealth === null
      ? "checking"
      : phones.some((phone) => phone.extraction_status === "paused" || phone.extraction_status === "stopped") || extractionHealth.pending > 0
        ? "warning"
        : "healthy";
  const extractionStatus = phones.find((phone) => phone.extraction_status)?.extraction_status || null;
  const extractionLabel = extractionHealth === null
    ? "Checking messages"
    : extractionStatus === "paused"
      ? "Syncing paused"
      : extractionStatus === "stopped"
        ? extractionHealth.pending > 0 ? "Syncing stopped · backlog" : "Syncing stopped"
        : extractionHealth.pending > 0
          ? extractionHealth.recentlyProcessed1h > 0 ? "Reading messages · backlog" : "Reading messages · waiting"
          : "Reading messages";
  const whatsappLabel = !hasConfiguredWhatsApp
    ? "Add WhatsApp number"
    : waConnected === null
      ? "Checking WhatsApp"
      : waConnected && waPhone
        ? waPhone
        : waConnected
          ? "WhatsApp connected"
          : "WhatsApp disconnected";

  const playGroupSound = useCallback(() => {
    const now = Date.now();
    if (now - lastGroupSoundAt.current < 5000) return;
    lastGroupSoundAt.current = now;
    playGroupConnected();
  }, []);

  const showNotificationNotice = useCallback((title: string, detail: string) => {
    setNotificationNotice({ title, detail });
    if (notificationTimer.current) clearTimeout(notificationTimer.current);
    notificationTimer.current = setTimeout(() => setNotificationNotice(null), 5000);
  }, []);

  useEventStream({
    "message.received": (event) => {
      if (!hasConfiguredWhatsApp) return;
      const sender = event.data?.sender_name || event.data?.sender || "WhatsApp";
      showNotificationNotice("New WhatsApp message", `Received from ${String(sender)}`);
      const now = Date.now();
      if (now - lastIncomingSoundAt.current < 3000) return;
      lastIncomingSoundAt.current = now;
      playNewWhatsApp();
    },
    "connection.changed": () => {
      if (!hasConfiguredWhatsApp) return;
      showNotificationNotice("Connection changed", "WhatsApp connection status was updated");
      playConnectionChange();
    },
    "whatsapp.conversations.updated": () => {
      if (!hasConfiguredWhatsApp) return;
      showNotificationNotice("Group directory updated", "A WhatsApp group or conversation changed");
      playGroupSound();
    },
    "group.updated": () => {
      if (!hasConfiguredWhatsApp) return;
      showNotificationNotice("Group directory updated", "A WhatsApp group was updated");
      playGroupSound();
    },
    "lead.created": () => {
      showNotificationNotice("New lead", "A new lead was added to your workspace");
      playNewLead();
    },
    "requirement.matched": () => {
      showNotificationNotice("Requirement matched", "A requirement match is available");
      playNewLead();
    },
  });

  useEffect(() => {
    if (waConnected === null || !hasConfiguredWhatsApp) return;
    const previous = previousWhatsAppState.current;
    if (waConnected === false) setDisconnectNoticeOpen(true);
    if (previous === false && waConnected === true) setDisconnectNoticeOpen(false);
    previousWhatsAppState.current = waConnected;
  }, [hasConfiguredWhatsApp, waConnected]);

  useEffect(() => {
    if (authLoading) return;
    if (authError) return;
    if (!user) {
      const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || "";
      const project = supabaseUrl.replace(/^https?:\/\//, "").split(".")[0];
      const hasStoredSession = typeof window !== "undefined" &&
        !!localStorage.getItem(`sb-${project}-auth-token`);
      const t = setTimeout(() => {
        router.replace(`/auth/login?next=${encodeURIComponent(pathname || "/dashboard")}`);
      }, hasStoredSession ? 2000 : 0);
      return () => clearTimeout(t);
    }
  }, [authLoading, authError, user, pathname, router]);

  useEffect(() => {
    if (authLoading) return;
    if (!user) {
      setIsSuperAdmin(false);
      localStorage.removeItem("propai_super_admin_user");
      return;
    }
    // Restore only a role verified for this same Supabase user. A role from a
    // previous browser account must never influence this account's navigation.
    if (localStorage.getItem("propai_super_admin_user") === user.id) {
      setIsSuperAdmin(true);
    }
    let cancelled = false;
    void getAuthMe()
      .then((authState) => {
        if (cancelled) return;
        if (authState.is_super_admin === true) {
          setIsSuperAdmin(true);
          localStorage.setItem("propai_super_admin_user", user.id);
        } else if (authState.role_check_available !== false) {
          setIsSuperAdmin(false);
          localStorage.removeItem("propai_super_admin_user");
        }
      })
      .catch(() => {
        // Retain the last verified role during a network/API interruption.
      });
    return () => { cancelled = true; };
  }, [authLoading, user?.id]);

  // PWA back-navigation stack — intercept popstate so the Android
  // hardware back button navigates in-app instead of exiting the PWA.
  const navStackRef = useRef<string[]>([]);
  useEffect(() => {
    const key = `propai_nav_stack`;
    try {
      const saved = sessionStorage.getItem(key);
      if (saved) navStackRef.current = JSON.parse(saved);
    } catch { /* ignore */ }
    const save = () => sessionStorage.setItem(key, JSON.stringify(navStackRef.current));
    // Push current path on navigation
    if (pathname && navStackRef.current[navStackRef.current.length - 1] !== pathname) {
      navStackRef.current.push(pathname);
      save();
    }
    const onPopState = () => {
      navStackRef.current.pop();
      save();
      const prev = navStackRef.current[navStackRef.current.length - 1];
      if (prev && prev !== pathname) {
        router.push(prev);
      } else {
        router.push("/dashboard");
      }
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [pathname, router]);

  const handleSignOut = useCallback(async () => {
    localStorage.removeItem("propai_profile");
    setProfile(null);
    await authSignOut();
    router.replace("/auth/login");
  }, [authSignOut, router]);

  useEffect(() => {
    if (authLoading || !user) return;
    const phoneCacheKey = `propai_phones:${user.id}`;
    const wabaCacheKey = `propai_waba:${user.id}`;
    const hydrateTimer = window.setTimeout(() => {
      try {
        const cachedPhones = JSON.parse(localStorage.getItem(phoneCacheKey) || "[]") as Phone[];
        if (cachedPhones.length > 0) setPhones(cachedPhones);
        // WABA responses may contain admin-only previews. Never retain them in
        // browser storage across users, role changes, or workspace switches.
        localStorage.removeItem(wabaCacheKey);
        setWabaConfig(null);
      } catch {
        // Ignore invalid snapshots and continue with live status checks.
      }
    }, 0);
    const load = async () => {
      const [phonesRes, status, extraction] = await Promise.all([
        getPhones(true, 15000).catch(() => null),
        // Focused workspaces do not use the dashboard status probe. Avoid
        // making inbox/group pages wait on a slow WhatsMeow status request.
        isFocusedWorkspace ? Promise.resolve(null) : getWhatsAppStatus().catch(() => null),
        fetchJSON<{
          pending?: number;
          eligible_pending?: number;
          recently_processed?: number;
          recently_processed_1h?: number;
        }>("/extraction/progress", undefined, 8000).catch(() => null),
      ]);
      if (phonesRes) {
        setPhones(phonesRes.phones || []);
        if (phonesRes.phones?.length) localStorage.setItem(phoneCacheKey, JSON.stringify(phonesRes.phones));
      }
      if (status) setLiveStatus(status);
      if (extraction) {
        setExtractionHealth({
          // Held/suppressed group rows are intentionally not worker backlog.
          pending: Number(extraction.eligible_pending ?? extraction.pending ?? 0),
          // The backend returns `recently_processed`; accept the legacy alias too.
          recentlyProcessed1h: Number(extraction.recently_processed ?? extraction.recently_processed_1h ?? 0),
        });
      }
    };
    void getBusinessApiConfig(15000).then((config) => {
      setWabaConfig(config);
      // /auth/me and /business-api/config are both server-side role checks.
      // Keep the admin navigation visible if either trusted response confirms
      // the role; a slow auth-me request must not hide platform tools.
      if (config.is_super_admin === true) setIsSuperAdmin(true);
      if (config.is_super_admin === true && user?.id) localStorage.setItem("propai_super_admin_user", user.id);
    }).catch(() => {});
    load();
    const t = setInterval(load, 30000);
    const onStatusUpdate = () => {
      void load();
    };
    window.addEventListener("propai_whatsapp_status_updated", onStatusUpdate);
    return () => {
      window.clearTimeout(hydrateTimer);
      clearInterval(t);
      window.removeEventListener("propai_whatsapp_status_updated", onStatusUpdate);
    };
  }, [authLoading, user, isFocusedWorkspace]);

  // PWA manifest link (static in RootLayout, this is for dynamic fallback)
  useEffect(() => {
    if (!document.querySelector('link[rel="manifest"]')) {
      const link = document.createElement("link");
      link.rel = "manifest";
      link.href = "/manifest";
      document.head.appendChild(link);
    }
  }, []);

  // Offline detection
  useEffect(() => {
    const onOnline = () => setOffline(false);
    const onOffline = () => setOffline(true);
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    return () => {
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
    };
  }, []);

  // Keyboard shortcut
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setPaletteOpen(p => !p);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  // Prevent body scroll when drawer is open
  useEffect(() => {
    document.body.classList.toggle("no-scroll", drawerOpen);
  }, [drawerOpen]);

  if (authError) {
    return (
      <div className="flex min-h-[100svh] items-center justify-center bg-background px-4 text-text-primary lg:min-h-screen">
        <div className="max-w-md rounded-xl border border-red-500/30 bg-transparent p-6 text-center">
          <div className="mx-auto mb-3 h-10 w-10 rounded-full border-2 border-red-400/30 border-t-red-400" />
          <div className="text-sm font-semibold">Session check stalled</div>
          <div className="mt-1 text-xs text-zinc-500">{authError}</div>
          <div className="mt-4 flex items-center justify-center gap-3">
            <button
              onClick={() => void refreshAuth()}
              className="min-h-[44px] rounded-md border border-white bg-white px-4 py-2.5 text-xs font-semibold text-black hover:bg-zinc-200"
            >
              Retry
            </button>
            <button
              onClick={() => router.replace(`/auth/login?next=${encodeURIComponent(pathname || "/dashboard")}`)}
              className="rounded-lg border border-white/10 bg-zinc-800 px-4 py-2.5 text-xs font-bold text-zinc-300 min-h-[44px]"
            >
              Go to login
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (authLoading || !user) {
    return (
      <div className="flex min-h-[100svh] items-center justify-center bg-background text-text-primary lg:min-h-screen">
        <div className="text-center">
          <div className="mx-auto mb-3 h-10 w-10 animate-spin rounded-full border-2 border-white/10 border-t-white" />
          <div className="text-sm font-semibold">
            {authLoading ? "Loading session..." : "Signing in..."}
          </div>
          <div className="mt-1 text-xs text-zinc-500">
            {authLoading ? "Verifying your workspace access." : "Redirecting to login."}
          </div>
        </div>
      </div>
    );
  }

  const navSections = isSuperAdmin
    ? [...baseNavSections, adminNavSection]
    : baseNavSections;
  // Market Inbox needs the same mobile navigation as every other workspace
  // route. Its own panel is sized to the remaining page stage below.
  // Conversation workspaces own the mobile viewport. The status rail remains
  // available on the dashboard and operational pages, but it is redundant
  // chrome above a chat composer and steals valuable keyboard-safe height.
  const hideGlobalChromeOnMobile = pathname === "/chat" || pathname === "/social-flow" || pathname === "/inbox";
  const buildLabel = getBuildLabel();
  const buildHint = getBuildHint();

  return (
    <div className="propai-shell flex h-dvh overflow-hidden bg-background">
      <PaletteModal open={paletteOpen} onClose={() => setPaletteOpen(false)} />
      {disconnectNoticeOpen && (
        <div className="fixed right-4 top-4 z-[1100] w-[min(380px,calc(100vw-2rem))] rounded-xl border border-red-400/30 bg-zinc-950 px-4 py-3 shadow-2xl shadow-black/50" role="alert">
          <div className="flex items-start gap-3">
            <WifiOff className="mt-0.5 h-5 w-5 shrink-0 text-red-300" />
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold text-white">WhatsApp disconnected</div>
              <p className="mt-1 text-xs leading-5 text-zinc-400">New group messages are not being received. Reconnect the linked phone to resume ingestion.</p>
              <div className="mt-3 flex items-center gap-2">
                <a href={withBasePath("/connections")} className="rounded-lg bg-red-400 px-3 py-1.5 text-xs font-semibold text-black hover:bg-red-300">Reconnect WhatsApp</a>
                <button type="button" onClick={() => setDisconnectNoticeOpen(false)} className="rounded-lg border border-white/10 px-3 py-1.5 text-xs text-zinc-300 hover:bg-white/5">Dismiss</button>
              </div>
            </div>
            <button type="button" onClick={() => setDisconnectNoticeOpen(false)} className="text-zinc-500 hover:text-white" aria-label="Dismiss WhatsApp disconnect notice"><X className="h-4 w-4" /></button>
          </div>
        </div>
      )}
      {notificationNotice && (
        <div className="fixed right-4 top-4 z-[1050] w-[min(360px,calc(100vw-2rem))] rounded-xl border border-emerald-400/25 bg-zinc-950 px-4 py-3 shadow-2xl shadow-black/50" role="status" aria-live="polite">
          <div className="flex items-start gap-3">
            <Bell className="mt-0.5 h-5 w-5 shrink-0 text-emerald-300" />
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold text-white">{notificationNotice.title}</div>
              <p className="mt-1 text-xs leading-5 text-zinc-400">{notificationNotice.detail}</p>
            </div>
            <button type="button" onClick={() => setNotificationNotice(null)} className="text-zinc-500 hover:text-white" aria-label="Dismiss notification"><X className="h-4 w-4" /></button>
          </div>
        </div>
      )}
      <MobileDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        onOpenPalette={() => setPaletteOpen(true)}
        isSuperAdmin={isSuperAdmin}
        whatsappConnected={waConnected}
        whatsappPhone={waPhone}
        extractionLabel={extractionLabel}
        extractionWarning={extractionHealthState === "warning"}
        buildLabel={buildLabel}
      />

      {/* ═══════ Sidebar (desktop) ═══════ */}
      <aside className="propai-sidebar hidden lg:flex w-60 flex-col border-r border-border shrink-0">
        {/* Logo */}
        <Link href="/" className="px-5 pt-6 pb-5 block">
          <div className="flex items-center gap-2.5">
            <img src={withBasePath("//propai-logo.svg")} alt="PropAI" className="propai-brand-mark w-10 h-10" />
            <div>
              <div className="text-[15px] font-bold text-text-primary tracking-tight leading-none">PropAI</div>
              <div className="text-[9px] text-zinc-400 uppercase tracking-[0.15em] font-medium mt-0.5">Broker OS</div>
            </div>
          </div>
        </Link>

        {/* Navigation */}
        <nav className="flex-1 overflow-y-auto px-3 pb-4" aria-label="Sidebar navigation">
          {navSections.map((section) => (
            <div key={section.title} className="mb-4">
              {section.title && <div className="px-2 mb-1.5 text-[9px] font-bold text-text-muted uppercase tracking-[0.15em]">{section.title}</div>}
              {section.items.map((item: NavItem) => {
                const itemPath = item.href.split("?")[0];
                const active = pathname === itemPath || (itemPath !== "/" && pathname.startsWith(itemPath));
                const Icon = item.icon;
                const isPrimary = item.label === "Search & Chat" || item.label === "My Deals";
                return (
                  <div key={item.href} className="mb-0.5">
                    {item.external ? (
                      <a href={item.href} target="_blank" rel="noreferrer" className={`propai-nav-link w-full flex items-center gap-2.5 px-2.5 py-2 rounded-lg transition-all duration-150 ${isPrimary ? "font-semibold text-[12px]" : "text-[12px] font-medium"}`}>
                        {Icon ? <Icon className="w-3.5 h-3.5 shrink-0" strokeWidth={1.5} /> : <span className="w-3.5 h-3.5 shrink-0" />}
                        <span className="truncate">{item.label}</span>
                      </a>
                    ) : (
                      <Link href={item.href} prefetch={true} data-active={active} data-priority={isPrimary} className={`propai-nav-link w-full flex items-center gap-2.5 px-2.5 py-2 rounded-lg transition-all duration-150 ${isPrimary ? "font-semibold text-[12px]" : "text-[12px] font-medium"}`}>
                        {Icon ? <Icon className={`w-3.5 h-3.5 shrink-0 ${active ? "text-text-primary" : ""}`} strokeWidth={1.5} /> : <span className="w-3.5 h-3.5 shrink-0" />}
                        <span className="truncate">{item.label}</span>
                        {active && <span className="ml-auto text-[9px] font-semibold uppercase tracking-[.14em] text-accent">Live</span>}
                      </Link>
                    )}
                    {item.children && (
                      <div className="ml-5 mt-1 space-y-0.5">
                        {item.children.map((child) => {
                          const childActive = pathname === child.href || pathname.startsWith(`${child.href}/`);
                          return (
                            <Link
                              key={child.href}
                              href={child.href}
                              prefetch={true}
                              className={`flex items-center gap-2 rounded-md px-2 py-1 text-[11px] transition-colors ${
                                childActive ? "text-text-primary" : "text-text-muted hover:text-text-secondary"
                              }`}
                            >
                              <span className={`h-1.5 w-1.5 rounded-full ${childActive ? "bg-accent" : "bg-border"}`} />
                              <span>{child.label}</span>
                            </Link>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ))}
        </nav>

        {/* Profile Section */}
        {user && (
          <div className="px-4 py-3 border-t border-border">
            <div className="flex items-center gap-2">
              <button onClick={() => router.push("/profile")}
                className="flex min-w-0 flex-1 items-center gap-3 px-2.5 py-2 rounded-lg hover:bg-surface-hover transition-colors text-left">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border bg-surface text-xs font-semibold text-text-secondary">
                  {profileIdentity.first_name?.charAt(0)?.toUpperCase() || "?"}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-[12px] font-semibold text-text-primary truncate">
                    {profileIdentity.first_name}{profileIdentity.last_name ? ` ${profileIdentity.last_name}` : ""}
                  </div>
                  {profileIdentity.city && <div className="text-[10px] text-text-muted truncate">{profileIdentity.city}</div>}
                </div>
              </button>
              <button
                onClick={handleSignOut}
                className="flex h-9 w-9 items-center justify-center rounded-lg text-text-muted transition-colors hover:bg-surface-hover hover:text-text-primary"
                aria-label="Log out"
                title="Log out"
              >
                <LogOut className="h-4 w-4" strokeWidth={1.5} />
              </button>
            </div>
          </div>
        )}

        {/* Bottom Status */}
        <div className="px-4 py-3 border-t border-border space-y-2">
          <button
            onClick={() => setPaletteOpen(true)}
            className="w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-[11px] text-text-muted hover:text-text-secondary hover:bg-surface-hover transition-colors"
          >
            <Search className="w-3.5 h-3.5 shrink-0" strokeWidth={1.5} />
            <span>Search</span>
            <kbd className="ml-auto text-[9px] bg-white/5 px-1.5 py-0.5 rounded text-zinc-500">⌘K</kbd>
          </button>
          <div className="relative">
            {soundSettingsOpen && (
              <div className="absolute bottom-full left-0 z-50 mb-2 w-80 rounded-xl border border-white/10 bg-zinc-950 p-3 shadow-2xl">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold text-white">Sound controls</span>
                  <button type="button" onClick={() => setSoundSettingsOpen(false)} className="text-zinc-500 hover:text-white" aria-label="Close sound controls"><X className="h-3.5 w-3.5" /></button>
                </div>
                <div className="mt-3 flex items-center gap-2">
                  <VolumeX className="h-3.5 w-3.5 text-zinc-500" />
                  <input aria-label="Sound volume" type="range" min="0" max="1" step="0.01" value={soundVolume} onChange={(event) => setSoundVolume(setVolume(Number(event.target.value)))} className="min-w-0 flex-1 accent-emerald-400" />
                  <span className="w-8 text-right text-[10px] text-zinc-500">{Math.round(soundVolume * 100)}%</span>
                </div>
                <div className="mt-3 space-y-2 border-t border-white/[0.06] pt-3">
                  {([
                    ["whatsapp", "New WhatsApp messages"],
                    ["groups", "Group directory updates"],
                    ["connection", "Connection changes"],
                    ["leads", "Leads and requirements"],
                  ] as [SoundEvent, string][]).map(([event, label]) => (
                    <div key={event} className="space-y-1.5 text-[11px] text-zinc-400">
                      <div className="flex items-center justify-between gap-3">
                        <span>{label}</span>
                        <input aria-label={`Enable ${label}`} type="checkbox" checked={soundEvents[event]} onChange={() => handleSoundEvent(event)} className="accent-emerald-400" />
                      </div>
                      <div className="flex items-center gap-2">
                        <select
                          aria-label={`${label} sound`}
                          value={soundPreferences[event]}
                          onChange={(change) => handleSoundSelection(event, change.target.value as SoundId)}
                          className="min-w-0 flex-1 rounded-md border border-white/10 bg-white/5 px-2 py-1.5 text-[11px] text-zinc-300 outline-none focus:border-emerald-400"
                        >
                          {SOUND_LIBRARY.map((sound) => <option key={sound.id} value={sound.id}>{sound.label}</option>)}
                        </select>
                        <button
                          type="button"
                          aria-label={`Preview ${label} sound`}
                          title="Preview selected sound"
                          onClick={() => previewSound(soundPreferences[event])}
                          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-zinc-500 hover:bg-white/10 hover:text-emerald-300"
                        >
                          <Volume2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
            <div className="flex items-center gap-1">
              <button onClick={handleToggleSounds} className="min-w-0 flex-1 flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-[11px] text-zinc-500 hover:text-zinc-300 hover:bg-white/5 transition-colors" title={soundsMuted ? "Unmute sounds" : "Mute sounds"}>
                {soundsMuted ? <VolumeX className="w-3.5 h-3.5 shrink-0" strokeWidth={1.5} /> : <Volume2 className="w-3.5 h-3.5 shrink-0" strokeWidth={1.5} />}
                <span>{soundsMuted ? "Sounds off" : `Sounds ${Math.round(soundVolume * 100)}%`}</span>
              </button>
              <button onClick={() => setSoundSettingsOpen((open) => !open)} className="flex h-7 w-7 items-center justify-center rounded-lg text-zinc-500 hover:bg-white/5 hover:text-zinc-300" title="Sound controls" aria-label="Sound controls">
                <SlidersHorizontal className="h-3.5 w-3.5" />
              </button>
            </div>
            <div
              className="flex items-center justify-between rounded-lg border border-white/5 bg-white/[0.03] px-2.5 py-1.5 text-[10px] text-zinc-500"
              title={buildHint}
            >
              <span className="shrink-0">Build</span>
              <div className="flex items-center gap-1.5">
                <span className="font-mono text-zinc-300">{buildLabel}</span>
                <button
                  type="button"
                  onClick={() => window.location.reload()}
                  className="inline-flex h-5 w-5 items-center justify-center rounded-md text-zinc-500 hover:bg-white/10 hover:text-white"
                  aria-label="Reload the page"
                  title="Reload the page"
                >
                  <RefreshCw className="h-3 w-3" />
                </button>
              </div>
            </div>
          </div>
          <a
            href={withBasePath("/connections")}
            className="flex items-center gap-2.5 px-2.5 py-2.5 rounded-lg hover:bg-white/5 transition-colors group"
          >
            {!hasConfiguredWhatsApp ? (
              <Wifi className="w-3.5 h-3.5 text-amber-300 shrink-0" strokeWidth={1.5} />
            ) : waConnected === null ? (
              <div className="relative shrink-0">
                <Wifi className="w-3.5 h-3.5 text-zinc-500" strokeWidth={1.5} />
                <span className="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 bg-zinc-500 rounded-full animate-pulse" />
              </div>
            ) : waConnected ? (
              <div className="relative shrink-0">
                <Wifi className={`w-3.5 h-3.5 ${waStale ? "text-zinc-500" : "text-[#6B8E63]"}`} strokeWidth={1.5} />
                <span className={`absolute -top-0.5 -right-0.5 h-1.5 w-1.5 rounded-full ${waStale ? "bg-zinc-500" : "bg-[#6B8E63]"}`} />
              </div>
            ) : (
              <WifiOff className="w-3.5 h-3.5 text-red-400 shrink-0" strokeWidth={1.5} />
            )}
            <div className="flex-1 min-w-0">
              <div className={`truncate text-[12px] font-semibold ${waConnected && !waStale ? "text-[#6B8E63]" : "text-zinc-300"}`}>
                  {!hasConfiguredWhatsApp
                  ? "Add WhatsApp number"
                  : waConnected === null
                  ? "Checking WhatsApp"
                  : waStale
                    ? "WhatsApp status stale"
                    : waConnected
                    ? "WhatsApp Connected"
                    : "WhatsApp Disconnected"}
              </div>
              {waConnected && livePhone?.connected_since && (
                <div className="text-[10px] text-zinc-500 truncate">
                  Since {new Date(livePhone.connected_since).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                </div>
              )}
            </div>
            <div className={`h-1.5 w-1.5 shrink-0 rounded-full ${!hasConfiguredWhatsApp ? "bg-amber-300" : waConnected === null ? "bg-zinc-500" : waConnected ? (waStale ? "bg-zinc-500" : "bg-[#6B8E63]") : "bg-red-400"}`} />
          </a>
        </div>
      </aside>

      {/* ═══════ Main Content ═══════ */}
        <main className="flex-1 flex flex-col overflow-hidden bg-background min-w-0">
        {/* ═══ Top Bar ═══ */}
        <div className={`${hideGlobalChromeOnMobile ? "max-lg:hidden " : ""}propai-status-rail shrink-0 border-b border-border`}>
          <div className="flex h-11 items-center gap-2 px-2 lg:px-5">
            {/* Hamburger (mobile) */}
            <button
              onClick={toggleDrawer}
              className="lg:hidden -ml-1 flex h-7 w-7 items-center justify-center rounded-lg text-zinc-400 hover:bg-white/5 hover:text-white transition-colors"
              aria-label={drawerOpen ? "Close menu" : "Open menu"}
            >
              {drawerOpen ? <X className="w-5 h-5" /> : <Menu className="w-5 h-5" />}
            </button>

            {/* Compact connection status. Details are available on tap on mobile. */}
            <button
              type="button"
              onClick={() => setMobileStatusExpanded((expanded) => !expanded)}
              className={`${isFocusedWorkspace ? "max-lg:hidden " : ""}flex min-w-0 flex-1 items-center gap-2 text-left lg:hidden`}
              aria-expanded={mobileStatusExpanded}
              aria-label="Show system status details"
            >
              <div className="min-w-0 space-y-0.5">
                <div className={`flex items-center gap-2 truncate text-[10px] font-semibold ${whatsappHealth === "healthy" ? "text-accent" : whatsappHealth === "error" ? "text-red-300" : "text-amber-300"}`}>
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${whatsappHealth === "healthy" ? "bg-accent" : whatsappHealth === "error" ? "bg-red-400" : "bg-amber-300"}`} />
                  <span className="truncate">{whatsappLabel}</span>
                </div>
                <div className={`flex items-center gap-2 truncate text-[10px] font-medium ${extractionHealthState === "healthy" ? "text-zinc-400" : extractionHealthState === "warning" ? "text-amber-300" : "text-zinc-500"}`}>
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${extractionHealthState === "healthy" ? "bg-accent" : extractionHealthState === "warning" ? "bg-amber-300" : "bg-zinc-500"}`} />
                  <span className="truncate">{extractionLabel}</span>
                </div>
              </div>
            </button>
            <div className="hidden min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1 lg:flex">
              {offline && (
                <span className="flex items-center gap-1 text-[10px] text-red-400 font-semibold">
                  <WifiOff className="w-3 h-3" strokeWidth={1.5} />
                  Offline
                </span>
              )}
              <a href={withBasePath("/connections")} className={`propai-status-pill shrink-0 text-[10px] font-semibold transition-colors sm:text-[11px] ${whatsappHealth === "healthy" ? "text-accent hover:text-accent-hover" : whatsappHealth === "error" ? "text-red-300 hover:text-red-200" : "text-amber-300 hover:text-amber-200"}`}>
                <span className={`h-1.5 w-1.5 rounded-full lg:h-2 lg:w-2 ${whatsappHealth === "healthy" ? "bg-accent" : whatsappHealth === "error" ? "bg-red-400" : "bg-amber-300"}`} />
                <span>{whatsappLabel}</span>
              </a>
              <a href={withBasePath("/connections")} className={`propai-status-pill shrink-0 text-[10px] font-semibold transition-colors sm:text-[11px] ${extractionHealthState === "healthy" ? "text-zinc-300 hover:text-white" : extractionHealthState === "warning" ? "text-amber-300 hover:text-amber-200" : "text-zinc-500 hover:text-zinc-300"}`} title={extractionStalled ? "Extraction has pending messages and processed none in the last hour" : undefined}>
                <span className={`h-1.5 w-1.5 rounded-full lg:h-2 lg:w-2 ${extractionHealthState === "healthy" ? "bg-accent" : extractionHealthState === "warning" ? "bg-amber-300" : "bg-zinc-500"}`} />
                <span>{extractionLabel}</span>
              </a>
              {waConnected && waPhone && (
                <a
                  href={withBasePath("/connections")}
                  className="shrink-0 font-mono text-[9px] text-text-muted transition-colors hover:text-text-primary sm:text-[10px] lg:text-[11px]"
                  title="Manage connected WhatsApp number"
                >
                  {waPhone}
                </a>
              )}
              {(wabaConfig?.outbound_allowed || wabaConfig?.shared_waba_number) && (
                <a href={withBasePath("/waba")} className="flex shrink-0 items-center gap-1 text-[10px] font-semibold text-accent transition-colors hover:text-accent-hover lg:text-[11px]" title={wabaConfig?.outbound_allowed ? "Workspace WABA connected" : "Message the PropAI assistant on WhatsApp"}>
                  <span className="h-1.5 w-1.5 rounded-full bg-accent lg:h-2 lg:w-2" />
                  <span>{wabaConfig?.outbound_allowed ? "WABA Connected" : "PropAI WABA"}</span>
                </a>
              )}
            </div>
            {mobileStatusExpanded && !isFocusedWorkspace && (
              <div className="absolute left-2 right-2 top-12 z-40 rounded-lg border border-border bg-zinc-950 px-3 py-2 text-[10px] text-zinc-400 shadow-xl lg:hidden">
                <div className="flex items-center justify-between gap-3">
                  <span>{whatsappLabel}</span>
                  <a href={withBasePath("/connections")} className="font-semibold text-accent">Manage</a>
                </div>
                <div className={`mt-1 ${extractionHealthState === "warning" ? "text-amber-300" : "text-zinc-500"}`}>{extractionLabel}</div>
                {wabaConfig?.outbound_allowed && <div className="mt-1 text-accent">WABA connected</div>}
              </div>
            )}
            <div className="flex-1" />
            <button
              onClick={handleSignOut}
              className={`${isFocusedWorkspace ? "max-lg:hidden " : ""}flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-text-muted transition-colors hover:bg-surface-hover hover:text-text-primary`}
              aria-label="Log out"
              title="Log out"
            >
              <LogOut className="h-3.5 w-3.5" strokeWidth={1.5} />
            </button>
          </div>
          </div>

        {/* Page content */}
        <div className={`propai-page-stage min-w-0 flex-1 min-h-0 overflow-x-hidden text-text-primary relative max-lg:pb-14 ${pathname === "/chat" ? "overflow-y-hidden" : "overflow-y-auto"}`}>
          {children}
        </div>
      </main>

      {/* ═══════ Bottom Navigation (mobile) ═══════ */}
      <div>
        <BottomNav onTabChange={setLastTab} onMenu={toggleDrawer} />
      </div>

      {/* Install Prompt */}
      <InstallPrompt />
    </div>
  );
}

function LandingLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-background text-text-primary min-h-screen">
      {children}
    </div>
  );
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isLanding = pathname === "/" || pathname === "/how-it-works";
  const isAuth = pathname.startsWith("/auth");
  const isMcpAuthorize = pathname === "/mcp-authorize";
  const isPublicShare = pathname.startsWith("/share/");
  const isLegal = pathname === "/privacy-policy" || pathname === "/terms-of-service";
  const isStandalone = isLanding || isAuth || isMcpAuthorize || isPublicShare || isLegal;

  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <meta charSet="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
        <meta name="theme-color" content="#FAF7F0" />
        <meta name="apple-mobile-web-app-capable" content="yes" />
        <meta name="apple-mobile-web-app-status-bar-style" content="default" />
        <meta name="apple-mobile-web-app-title" content="PropAI" />
        <link rel="icon" type="image/svg+xml" href="/propai-logo.svg" />
        <link rel="apple-touch-icon" href="/pwa-192x192.png" />
        <link rel="manifest" href="/manifest.json" />
        <meta name="application-name" content="PropAI" />
        <meta name="mobile-web-app-capable" content="yes" />
      </head>
      <body className={isStandalone ? "" : "lg:overflow-hidden"}>
        <ThemeProvider>
          <ServiceWorkerRegister />
          {isStandalone ? (
            <LandingLayout>{children}</LandingLayout>
          ) : (
            <LayoutProvider>
              <AuthProvider>
                <AppShell>{children}</AppShell>
              </AuthProvider>
            </LayoutProvider>
          )}
        </ThemeProvider>
      </body>
    </html>
  );
}
