import { getAccessToken, forceRefreshToken } from "@/lib/auth";
import { apiUrl } from "@/lib/base-path";

const BASE = apiUrl("");
const API_TIMEOUT_MS = 60000;
const ACTIVE_TENANT_KEY = "propai_active_tenant";

function apiErrorMessage(body: string, fallback: string): string {
  const raw = body.trim();
  if (!raw) return fallback;
  try {
    const parsed = JSON.parse(raw);
    const detail = parsed?.message ?? parsed?.detail ?? parsed?.error;
    if (typeof detail === "string") return detail.trim() || fallback;
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => (typeof item === "string" ? item : item?.msg))
        .filter((item): item is string => typeof item === "string" && Boolean(item.trim()));
      if (messages.length) return messages.join("; ");
    }
    if (detail != null) return JSON.stringify(detail);
  } catch {
    // Non-JSON API responses are surfaced as plain text below.
  }
  return raw;
}

function readActiveTenantId(): string | null {
  if (typeof window === "undefined") return null;
  const value = window.localStorage.getItem(ACTIVE_TENANT_KEY);
  return value && value.trim() ? value.trim() : null;
}

export function setActiveTenantId(tenantId: string | null | undefined) {
  if (typeof window === "undefined") return;
  if (tenantId && tenantId.trim()) {
    window.localStorage.setItem(ACTIVE_TENANT_KEY, tenantId.trim());
  } else {
    window.localStorage.removeItem(ACTIVE_TENANT_KEY);
  }
}

export async function fetchJSON<T>(url: string, init?: RequestInit, timeoutMs = API_TIMEOUT_MS): Promise<T> {
  return fetchJSONWithRetry<T>(url, init, timeoutMs, false);
}

export interface OnboardingGroup {
  group_jid: string;
  group_name: string;
  participants: number;
  last_message_at: string | null;
  connected?: boolean;
  opted_out: boolean;
  network_owned?: boolean;
  suggestion?: {
    score: number;
    reasons: string[];
  } | null;
  overlap_score?: number;
  overlap_sample_count?: number;
  overlap_shared_count?: number;
  overlap_status?: "high_overlap" | "moderate_overlap" | "new_reach" | "unknown";
  member_count?: number;
  tracked_member_count?: number;
  overlap_percent?: number | null;
  novel_member_count?: number;
  novelty_percent?: number | null;
  covered_by_other_connection?: boolean;
  selection_reason?: string;
  selectable?: boolean;
}

export interface OnboardingGroupCap {
  tier: string;
  cap: number | null;
  opted_out_count: number;
  selected_count: number;
  remaining: number | null;
  overridden: boolean;
  unlimited?: boolean;
  soft_warning_at_cap: boolean;
  hard_block: boolean;
}

export interface OnboardingGroupCheck {
  group: OnboardingGroup;
  sample_count: number;
  shared_count: number;
  overlap_score: number;
  high_overlap: boolean;
  sample_phones: string[];
  threshold: number;
  cap: OnboardingGroupCap;
}

export interface OnboardingGroupState extends OnboardingGroupCap {
  groups: OnboardingGroup[];
  extraction_status: "stopped" | "running" | "paused";
}

export interface OnboardingGroupToggleResult {
  ok: boolean;
  group: OnboardingGroup;
  cap: OnboardingGroupCap;
  opted_out: boolean;
}

export function getOnboardingGroups(whatsappConnectionId: number) {
  return fetchJSON<OnboardingGroupState>(`/onboarding/groups?whatsapp_connection_id=${whatsappConnectionId}`);
}

export type ExtractionStatus = "stopped" | "running" | "paused";

function setExtractionStatus(whatsappConnectionId: number, action: "start" | "pause" | "stop") {
  return fetchJSON<{ ok: boolean; whatsapp_connection_id: number; extraction_status: ExtractionStatus; message: string }>(`/onboarding/extraction/${action}`, {
    method: "POST",
    body: JSON.stringify({ whatsapp_connection_id: whatsappConnectionId }),
  });
}

export function startExtraction(whatsappConnectionId: number) {
  return setExtractionStatus(whatsappConnectionId, "start");
}

export function pauseExtraction(whatsappConnectionId: number) {
  return setExtractionStatus(whatsappConnectionId, "pause");
}

export function stopExtraction(whatsappConnectionId: number) {
  return setExtractionStatus(whatsappConnectionId, "stop");
}

export function checkOnboardingGroup(
  whatsappConnectionId: number,
  groupJid: string,
  confirmCap = false,
) {
  return fetchJSON<OnboardingGroupCheck>("/onboarding/groups/check", {
    method: "POST",
    body: JSON.stringify({
      whatsapp_connection_id: whatsappConnectionId,
      group_jid: groupJid,
      confirm_cap: confirmCap,
    }),
  });
}

export function optOutOnboardingGroup(
  whatsappConnectionId: number,
  groupJid: string,
  groupName?: string,
  confirmCap = false,
) {
  return fetchJSON<OnboardingGroupToggleResult>("/onboarding/groups/opt-out", {
    method: "POST",
    body: JSON.stringify({
      whatsapp_connection_id: whatsappConnectionId,
      group_jid: groupJid,
      group_name: groupName,
      confirm_cap: confirmCap,
    }),
  });
}

export function optInOnboardingGroup(whatsappConnectionId: number, groupJid: string) {
  return fetchJSON<{ ok: boolean; message: string; cap: OnboardingGroupCap }>("/onboarding/groups/opt-in", {
    method: "POST",
    body: JSON.stringify({
      whatsapp_connection_id: whatsappConnectionId,
      group_jid: groupJid,
    }),
  });
}

export function selectOnboardingGroups(whatsappConnectionId: number, groupJids: string[]) {
  return fetchJSON<{ ok: boolean; selected_group_jids: string[]; selected_count: number; cap: OnboardingGroupCap }>("/onboarding/groups/select", {
    method: "POST",
    body: JSON.stringify({
      whatsapp_connection_id: whatsappConnectionId,
      group_jids: groupJids,
      confirm: true,
    }),
  });
}

export async function fetchFormData<T>(url: string, formData: FormData, timeoutMs = API_TIMEOUT_MS): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const token = await getAccessToken();
    const tenantId = readActiveTenantId();
    const res = await fetch(`${BASE}${url}`, {
      method: "POST",
      body: formData,
      cache: "no-store",
      signal: controller.signal,
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(tenantId ? { "X-Tenant-Id": tenantId } : {}),
      },
    });
    if (!res.ok) {
      const body = await res.text();
      const message = apiErrorMessage(body, "Backend API did not return a response.");
      throw new Error(`${res.status} ${res.statusText}: ${message}`);
    }
    return await parseJSONBody<T>(res, url);
  } finally {
    clearTimeout(timeout);
  }
}

async function fetchJSONWithRetry<T>(
  url: string,
  init: RequestInit | undefined,
  timeoutMs: number,
  retried: boolean,
  requestBase = BASE,
): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  
  const handleAbort = () => controller.abort();
  if (init?.signal?.aborted) {
    handleAbort();
  } else {
    init?.signal?.addEventListener("abort", handleAbort, { once: true });
  }
  
  try {
    const token = await getAccessToken();
    const tenantId = readActiveTenantId();
    const res = await fetch(`${requestBase}${url}`, {
      ...init,
      cache: "no-store",
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(tenantId ? { "X-Tenant-Id": tenantId } : {}),
        ...init?.headers,
      },
    });
    if (res.status === 401 && !retried) {
      // Token likely expired between acquisition and send — force a refresh
      // and retry once with the fresh token before surfacing the error.
      const fresh = await forceRefreshToken();
      if (fresh) {
        const retryRes = await fetch(`${requestBase}${url}`, {
          ...init,
          signal: controller.signal,
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${fresh}`,
            ...(tenantId ? { "X-Tenant-Id": tenantId } : {}),
            ...init?.headers,
          },
        });
        if (retryRes.ok) {
          return await parseJSONBody<T>(retryRes, url);
        }
      }
    }
    if (!res.ok) {
      const body = await res.text();
      let message = apiErrorMessage(
        body,
        "Backend API did not return a response. Check that the API server is running.",
      );
      const isHtmlResponse = /<!DOCTYPE\s+html|<html|<\s*div|<\s*body/i.test(body);
      if (isHtmlResponse) {
        const statusLabel = res.statusText || (res.status >= 500 ? "Server error" : "Request failed");
        message = `Backend error ${res.status}: ${statusLabel}. Please try again.`;
      }
      if (!message || message.trim() === "") {
        const statusLabel = res.statusText || (res.status >= 500 ? "Server error" : "Request failed");
        message = `Backend error ${res.status}: ${statusLabel}. Please try again.`;
      }
      throw new Error(`${res.status} ${res.statusText}: ${message}`);
    }
    return await parseJSONBody<T>(res, url);
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error(timedOut ? `Request timed out: ${url}` : `Request cancelled: ${url}`);
    }
    throw error;
  } finally {
    clearTimeout(timeout);
    init?.signal?.removeEventListener("abort", handleAbort);
  }
}

async function parseJSONBody<T>(res: Response, url: string): Promise<T> {
  const body = await res.text();
  if (!body) return undefined as T;
  try {
    return JSON.parse(body) as T;
  } catch {
    throw new Error(`Expected JSON from ${url}, got: ${body.slice(0, 200)}`);
  }
}

export interface RawMessage {
  id: number;
  chat_id?: string;
  chat_type?: "group" | "direct";
  chat_name?: string;
  conversation_type?: "group" | "direct";
  conversation_key?: string;
  conversation_name?: string;
  group_name: string;
  sender: string;
  sender_jid?: string;
  sender_phone?: string;
  broker_name?: string;
  broker_phone?: string;
  building_name?: string;
  micro_market?: string;
  landmark_name?: string;
  parsed_intent?: string;
  message_count?: number;
  latest_message_at?: string;
  duplicate_count?: number;
  duplicate_group_names?: string[];
  message: string;
  message_type: string;
  timestamp: string;
  created_at?: string;
  source: string;
  event_id: string;
  message_uid: string;
  raw_payload: string | Record<string, unknown>;
  attachments?: string | Record<string, boolean>;
  from_me?: boolean | number;
  synced_at: string;
  pipeline_version: string;
  tenant_id?: string;
  market_scope?: "workspace" | "shared";
  delivery_status?: string | null;
  delivery_updated_at?: string | null;
}

export interface InboxThread extends RawMessage {
  conversation_type: "group" | "direct";
  conversation_key: string;
  message_count: number;
  conversation_name: string;
  chat_id?: string;
  chat_type?: "group" | "direct";
  chat_name?: string;
  latest_message_at?: string;
  lag_seconds?: number;
}

export interface ParsedObservation {
  id: number;
  raw_message_id: number;
  raw_group: string;
  raw_timestamp: string;
  broker_name: string;
  broker_phone: string;
  intent: string;
  principal: string;
  forwarded: boolean;
  bhk: string;
  price: number;
  price_unit: string;
  area_sqft: number;
  furnishing: string;
  location_raw: string;
  location: { tokens?: { text: string; kind: string }[] } | null;
  landmark_name: string;
  building_name: string;
  micro_market: string;
  street_name: string;
  developer: string;
  confidence: number;
  created_at: string;
}

export interface DashboardActivity {
  messages_today: number;
  message_types: Record<string, number>;
}

export interface TimeWindowMetrics {
  window: string;
  label: string;
  messages: number;
  total_messages: number;
  supply: number;
  total_supply: number;
  demand: number;
  total_demand: number;
  rentals: number;
  total_rentals: number;
  needs_review: number;
  total_needs_review: number;
  start_date: string | null;
  end_date: string | null;
}

export function getTimeWindowMetrics(window = "today") {
  return fetchJSON<TimeWindowMetrics>(`/dashboard/time-window?window=${window}`);
}

export function getRecentParsedMessages(limit = 10) {
  return fetchJSON<any[]>(`/extraction/recent-parsed?limit=${limit}`, undefined, 8000);
}

export interface DashboardCoverage {
  groups_connected: number;
  messages_stored: number;
  listings_known?: number;
  buildings_known: number;
  landmarks_known: number;
  developers_known: number;
  micro_markets_known: number;
}

export interface ListingRow {
  id: number;
  fingerprint: string;
  intent: string;
  bhk: string;
  price: number;
  price_unit: string;
  area_sqft: number;
  furnishing: string;
  location_label: string;
  building_name: string;
  landmark_name: string;
  micro_market: string;
  broker_name: string;
  broker_phone: string;
  first_seen: string;
  last_seen: string;
  observation_count: number;
  group_count: number;
  latest_raw_message_id: number;
  representative_raw_message_id: number;
  latest_timestamp: string;
  latest_group: string;
}

export interface ConnectionState {
  state: string;
  connected: boolean;
  status_stale?: boolean;
}

export interface WhatsAppStatus {
  connected: boolean;
  phone: string;
  profile: string;
  instance: string;
  state: string;
  connected_since: string;
  status_stale?: boolean;
}

type LiveConnectionLike = {
  connected?: boolean | null;
  state?: string | null;
  connection_state?: string | null;
  connected_since?: string | null;
};

export function isLiveWhatsAppConnection(status?: LiveConnectionLike | null) {
  return Boolean(
    status?.connected ||
    status?.state === "open" ||
    status?.state === "connected" ||
    status?.connection_state === "open" ||
    status?.connection_state === "connected"
  );
}

export interface MarketAccessStatus {
  authenticated: boolean;
  tenant_id?: string;
  whatsapp_connected: boolean;
  waba_configured?: boolean;
  initial_sync_complete?: boolean;
  trial_active: boolean;
  paid_active: boolean;
  market_unlocked: boolean;
  trial_started_at?: string | null;
  trial_ends_at?: string | null;
  reason: "ready" | "connect_whatsapp" | "sync_pending" | string;
  message: string;
}

export function getRaw(
  limit = 50,
  offset = 0,
  group_name?: string,
  sender?: string,
  sender_phone?: string,
  sender_jid?: string,
  timeoutMs = 15000,
) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (group_name) params.set("group_name", group_name);
  if (sender) params.set("sender", sender);
  if (sender_phone) params.set("sender_phone", sender_phone);
  if (sender_jid) params.set("sender_jid", sender_jid);
  return fetchJSON<RawMessage[]>(`/raw?${params.toString()}`, undefined, timeoutMs);
}

export function getRawMessage(id: number, timeoutMs = 10000) {
  return fetchJSON<RawMessage>(`/raw?raw_id=${encodeURIComponent(String(id))}`, undefined, timeoutMs);
}

export function getInboxThreads(limit = 500, offset = 0) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return fetchJSON<InboxThread[]>(`/inbox/threads?${params.toString()}`);
}

export function getChats(limit = 500, offset = 0) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return fetchJSON<InboxThread[]>(`/chats?${params.toString()}`);
}

export function getChatMessages(chatId: string, limit = 300, offset = 0) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return fetchJSON<RawMessage[]>(`/chats/${encodeURIComponent(chatId)}/messages?${params.toString()}`);
}

export interface SendMessageRequest {
  remote_jid: string;
  text: string;
  broker_id?: string;
  quoted_message_id?: string;
  quoted_remote_jid?: string;
  quoted_participant?: string;
  quoted_from_me?: boolean;
}

export function sendMessage(payload: SendMessageRequest) {
  return fetchJSON<any>("/send", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export interface WabaSendRequest {
  to: string;
  text: string;
  remote_jid?: string;
}

export function sendWabaMessage(payload: WabaSendRequest) {
  return fetchJSON<any>("/waba/send", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export interface SendMediaRequest {
  remote_jid: string;
  media_type: "image" | "video" | "audio" | "document";
  caption?: string;
  file_name?: string;
  mime_type?: string;
  broker_id?: string;
  file: File;
}

export function sendMediaMessage(payload: SendMediaRequest) {
  const formData = new FormData();
  formData.set("remote_jid", payload.remote_jid);
  formData.set("media_type", payload.media_type);
  if (payload.caption) formData.set("caption", payload.caption);
  if (payload.file_name) formData.set("file_name", payload.file_name);
  if (payload.mime_type) formData.set("mime_type", payload.mime_type);
  if (payload.broker_id) formData.set("broker_id", payload.broker_id);
  formData.set("file", payload.file, payload.file.name);
  return fetchFormData<any>("/send-media", formData);
}

export interface WabaSessionStatus {
  active: boolean;
  remaining_seconds: number;
  last_user_at: string | null;
  expired: boolean;
}

export function getWabaSessionStatus(chatId: string) {
  return fetchJSON<WabaSessionStatus>(`/waba/session/${encodeURIComponent(chatId)}`);
}

export function getWabaSessionsBulk() {
  return fetchJSON<Array<{ chat_id: string; active: boolean; remaining_seconds: number; last_user_at: string }>>("/waba/sessions");
}

export function getParsed(limit = 50, offset = 0) {
  return fetchJSON<ParsedObservation[]>(`/parsed?limit=${limit}&offset=${offset}`);
}

export function getListings(limit = 50, offset = 0) {
  return fetchJSON<ListingRow[]>(`/listings?limit=${limit}&offset=${offset}`);
}

export function getListing(listingId: number) {
  return fetchJSON<any>(`/listings/${listingId}`);
}

export function getDashboardActivity() {
  return fetchJSON<DashboardActivity>("/dashboard/activity");
}

export function getDashboardCoverage() {
  return fetchJSON<DashboardCoverage>("/dashboard/coverage");
}

export function getDashboardFeed(limit = 20) {
  return fetchJSON<any[]>(`/dashboard/feed?limit=${limit}`);
}

export function getDashboardHeatmap() {
  return fetchJSON<any[]>("/dashboard/heatmap");
}

export function getStats(timeoutMs = 10000) {
  return fetchJSON<any>("/stats", undefined, timeoutMs);
}

export function getSyncActivity(timeoutMs = 10000) {
  return fetchJSON<any>("/dashboard/sync-activity", undefined, timeoutMs);
}

export function getWhatsAppStatus(timeoutMs = 8000) {
  return fetchJSON<WhatsAppStatus>("/dashboard/whatsapp-status", undefined, timeoutMs);
}

export function getMarketAccessStatus() {
  return fetchJSON<MarketAccessStatus>("/market/access", undefined, 8000);
}

export function getSourceStatus() {
  return fetchJSON<any>("/sources/status");
}

export function getConnectionState() {
  return fetchJSON<ConnectionState>("/sync/connection-state");
}

export function getConnectionDetail() {
  return fetchJSON<any>("/sync/connection");
}

export function logout() {
  return fetchJSON<any>("/sync/logout", { method: "POST" });
}

export function startSync() {
  return fetchJSON<any>("/sources/whatsapp/sync", { method: "POST" });
}

export function stopSync() {
  return fetchJSON<any>("/sources/stop", { method: "POST" });
}

export function getInboxEvidence(id: number) {
  return fetchJSON<any>(`/inbox/evidence/${id}`);
}

export function updateParsedObservation(id: number, schema: string | null, updates: Record<string, unknown>) {
  const query = schema ? `?schema=${encodeURIComponent(schema)}` : "";
  return fetchJSON<{ success: boolean; id: number; updated_fields: string[] }>(`/parsed/${id}${query}`, {
    method: "PATCH",
    body: JSON.stringify(updates),
  });
}

export function getGroups() {
  return fetchJSON<any[]>("/groups");
}

export function getGroupsHealth() {
  return fetchJSON<{ groups: any[]; summary: any }>("/groups/health");
}

export function getGroupMembers(jid: string) {
  return fetchJSON<any[]>(`/groups/${encodeURIComponent(jid)}/members`);
}

export function getWhatsAppConversations(types = "group,broadcast", query = "", relevantOnly = false) {
  const params = new URLSearchParams({ types });
  if (query.trim()) params.set("q", query.trim());
  if (relevantOnly) params.set("relevant_only", "true");
  return fetchJSON<any[]>(`/whatsapp/conversations?${params.toString()}`);
}

export function refreshWhatsAppGroupDirectory() {
  return fetchJSON<{ ok: boolean; state: string; requested: string[]; unavailable: string[] }>(
    "/whatsapp/conversations/refresh",
    { method: "POST" },
  );
}

export function getBuildings(limit = 100, offset = 0) {
  return fetchJSON<any>(`/buildings?limit=${limit}&offset=${offset}`);
}

export function discoverBuildingAliases(minConfidence = 0.7) {
  return fetchJSON<{ discovered: number; saved: number; suggestions: any[] }>(
    `/buildings/aliases/discover?min_confidence=${minConfidence}`,
    { method: "POST" }
  );
}

export function getAliasSuggestions(status = "pending", limit = 50) {
  return fetchJSON<{ suggestions: any[]; count: number }>(
    `/buildings/aliases/suggestions?status=${status}&limit=${limit}`
  );
}

export function reviewAliasSuggestion(suggestionId: number, approved: boolean) {
  return fetchJSON<{ success: boolean }>(
    `/buildings/aliases/${suggestionId}/review?approved=${approved}`,
    { method: "POST" }
  );
}

export function getAliasStats() {
  return fetchJSON<{
    total_suggestions: number;
    pending: number;
    approved: number;
    rejected: number;
    aliases_in_kb: number;
  }>("/buildings/aliases/stats");
}

export function getBrokers() {
  return fetchJSON<any[]>("/brokers");
}

export type BlockedBroker = {
  id: number;
  broker_key: string;
  broker_name?: string;
  broker_phone?: string;
  reason?: string;
  created_at?: string;
};

export function getBlockedBrokers() {
  return fetchJSON<{ brokers: BlockedBroker[] }>("/brokers/blocked");
}

export function blockBroker(phone: string, name = "", reason = "") {
  return fetchJSON<any>("/brokers/block", {
    method: "POST",
    body: JSON.stringify({ phone, name, reason }),
  });
}

export function unblockBroker(brokerKey: string) {
  return fetchJSON<any>("/brokers/block", {
    method: "DELETE",
    body: JSON.stringify({ broker_key: brokerKey }),
  });
}

export function getBroker(id: number) {
  return fetchJSON<any>(`/brokers/${id}`);
}

export function findBroker(name: string, phone: string) {
  const params = new URLSearchParams();
  if (name) params.set("name", name);
  if (phone) params.set("phone", phone);
  return fetchJSON<{ broker_id: number }>(`/brokers/find?${params.toString()}`);
}

export function getPriceStats(market = "", bhk = "", intent = "listing") {
  const params = new URLSearchParams();
  if (market) params.set("market", market);
  if (bhk) params.set("bhk", bhk);
  params.set("intent", intent);
  return fetchJSON<any>(`/price-stats?${params.toString()}`);
}

export function searchMessages(q: string, signal?: AbortSignal) {
  return fetchJSON<any>(`/search?q=${encodeURIComponent(q)}`, { signal });
}

export interface RawSearchResult {
  id: number;
  group_name: string;
  sender: string;
  sender_phone: string;
  message: string;
  timestamp: string;
  source: string;
  snippet: string;
}

export function searchRawMessages(q: string, limit = 20, offset = 0) {
  return fetchJSON<{ results: RawSearchResult[]; count: number; query: string }>(
    `/search/raw?q=${encodeURIComponent(q)}&limit=${limit}&offset=${offset}`
  );
}

export function searchRawBySender(sender: string, limit = 50) {
  return fetchJSON<{ results: RawSearchResult[]; count: number; query: string }>(
    `/search/raw/sender?sender=${encodeURIComponent(sender)}&limit=${limit}`
  );
}

export function searchRawByGroup(groupJid: string, limit = 50) {
  return fetchJSON<{ results: RawSearchResult[]; count: number; query: string }>(
    `/search/raw/group?group_jid=${encodeURIComponent(groupJid)}&limit=${limit}`
  );
}

export function getBuildingProfile(buildingId: string) {
  return fetchJSON<any>(`/buildings/${encodeURIComponent(buildingId)}`);
}

export function getBuildingAliases(buildingId: string) {
  return fetchJSON<any[]>(`/buildings/${encodeURIComponent(buildingId)}/aliases`);
}

export function refreshBuilding(buildingId: string, provider?: string) {
  const params = provider ? `?provider=${provider}` : "";
  return fetchJSON<any>(`/buildings/${encodeURIComponent(buildingId)}/refresh${params}`, {
    method: "POST",
  });
}

export function geocodeBuilding(buildingId: string) {
  return fetchJSON<any>(`/buildings/${encodeURIComponent(buildingId)}/geocode`, {
    method: "POST",
  });
}

export function discoverBuildings() {
  return fetchJSON<any>("/buildings/discover", { method: "POST" });
}

export function refreshBuildingCounts() {
  return fetchJSON<any>("/buildings/refresh-counts", { method: "POST" });
}

export function getBuildingEnrichmentDashboard() {
  return fetchJSON<any>("/buildings/enrichment/dashboard");
}

export function getBuildingEnrichmentJobs(status?: string, limit = 50) {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  params.set("limit", String(limit));
  return fetchJSON<any[]>(`/buildings/enrichment/jobs?${params.toString()}`);
}

export function getBuildingEnrichmentHistory(buildingId?: string, limit = 50) {
  const params = new URLSearchParams();
  if (buildingId) params.set("building_id", buildingId);
  params.set("limit", String(limit));
  return fetchJSON<any[]>(`/buildings/enrichment/history?${params.toString()}`);
}

export function getMarketDetail(name: string) {
  return fetchJSON<any>(`/markets/${encodeURIComponent(name)}`);
}

export function getActionDashboard() {
  return fetchJSON<any>("/action/dashboard");
}

export interface ChatSuggestions {
  top_building: string | null;
  top_supply_market: string | null;
  top_demand_market: string | null;
  top_commercial_market: string | null;
  top_rental_market: string | null;
  top_broker_building: string | null;
}

export function getChatSuggestions(): Promise<ChatSuggestions> {
  return fetchJSON<ChatSuggestions>("/chat/suggestions");
}

// ── AI Chat Sessions ──────────────────────────────────────────

export interface ChatSession {
  id: string;
  slug: string;
  broker_phone: string;
  title: string;
  source: string;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  id: string;
  session_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  blocks?: WorkspaceBlock[];
  created_at: string;
}

export function listChatSessions(): Promise<ChatSession[]> {
  return fetchJSON<ChatSession[]>("/ai/chat/sessions");
}

export function createChatSession(title = "New chat", source = "parsed"): Promise<ChatSession> {
  return fetchJSON<ChatSession>(`/ai/chat/sessions?title=${encodeURIComponent(title)}&source=${encodeURIComponent(source)}`, { method: "POST" });
}

export function getChatSessionMessages(sessionId: string): Promise<ChatMessage[]> {
  return fetchJSON<ChatMessage[]>(`/ai/chat/sessions/${sessionId}/messages`);
}

export function deleteChatSession(sessionId: string): Promise<{ ok: boolean }> {
  return fetchJSON<{ ok: boolean }>(`/ai/chat/sessions/${sessionId}`, { method: "DELETE" });
}

export function renameChatSession(sessionId: string, title: string): Promise<ChatSession> {
  return fetchJSON<ChatSession>(`/ai/chat/sessions/${sessionId}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

export interface AgentActionResult {
  status: string;
  tool?: string;
  reason?: string;
  candidate?: Record<string, unknown> | null;
  note?: Record<string, unknown> | null;
}

export interface BrowserActionResult extends ChatResponse {}

export function confirmAgentAction(confirmationToken: string): Promise<AgentActionResult> {
  return fetchJSON<AgentActionResult>("/ai/chat/confirm", {
    method: "POST",
    body: JSON.stringify({ confirmation_token: confirmationToken }),
  });
}

export function confirmBrowserAction(confirmationToken: string): Promise<BrowserActionResult> {
  return fetchJSON<BrowserActionResult>("/ai/chat/browser/confirm", {
    method: "POST",
    body: JSON.stringify({ confirmation_token: confirmationToken }),
  });
}

export function declineBrowserAction(confirmationToken: string): Promise<BrowserActionResult> {
  return fetchJSON<BrowserActionResult>("/ai/chat/browser/decline", {
    method: "POST",
    body: JSON.stringify({ confirmation_token: confirmationToken }),
  });
}

export function resolveBrokerContact(
  listingId: number,
  sourceSchema?: string,
  rawMessageId?: number,
  contactIndex?: number,
): Promise<{ contact_url: string }> {
  return fetchJSON<{ contact_url: string }>(`/contact-broker/${listingId}`, {
    method: "POST",
    body: JSON.stringify({
      source_schema: sourceSchema || null,
      raw_message_id: rawMessageId || null,
      contact_index: contactIndex ?? null,
    }),
  });
}

export function listBrokerContacts(
  listingId: number,
  sourceSchema?: string,
  rawMessageId?: number,
): Promise<{ contacts: Array<{ index: number; label: string }> }> {
  return fetchJSON<{ contacts: Array<{ index: number; label: string }> }>(`/contact-broker/${listingId}`, {
    method: "POST",
    body: JSON.stringify({
      source_schema: sourceSchema || null,
      raw_message_id: rawMessageId || null,
      list_contacts: true,
    }),
  });
}

export function getGraphGrowth() {
  return fetchJSON<any>("/dashboard/graph-growth");
}

export interface ChatResponse {
  content: string;
  blocks: WorkspaceBlock[];
  sources: string[];
  status_steps?: string[];
  trace?: {
    sources?: string[];
    last_updated?: string;
    notes?: string[];
    route?: string;
    browser_session_id?: string;
    browser_provider?: string;
    browser_url?: string;
    browser_title?: string;
    actions?: ChatActivityTraceAction[];
  };
}

export interface ChatActivityTraceAction {
  tool?: string;
  status?: string;
  summary?: string;
  provider?: string;
  url?: string;
  title?: string;
  detail?: string;
  browser_session_id?: string;
}

export interface WorkspaceBlockAction {
  label: string;
  value?: string;
  href?: string;
  kind?: string;
}

export interface WorkspaceBlockMetric {
  label: string;
  value: string;
  tone?: "neutral" | "good" | "warn" | "bad" | "accent";
}

export interface WorkspaceBlock {
  type:
    | "summary"
    | "listing_cards"
    | "buyer_cards"
    | "broker_cards"
    | "building_card"
    | "market_card"
    | "table"
    | "timeline"
    | "map"
    | "comparison"
    | "original_messages"
    | "ai_suggestions"
    | "charts"
    | "export_panel"
    | "promotion_preview"
    | "property_gallery"
    | "related_listings"
    | "matching_buyers"
  | "suggested_questions"
  | "error_state"
  | "empty_state"
  | "loading"
  | "activity"
  | "confirmation"
  | string;
  title?: string;
  subtitle?: string;
  body?: string;
  summary?: string;
  description?: string;
  note?: string;
  items?: any[];
  results?: any[];
  rows?: any[];
  columns?: string[];
  metrics?: WorkspaceBlockMetric[];
  bullets?: string[];
  actions?: WorkspaceBlockAction[];
  cards?: any[];
  events?: any[];
  questions?: string[];
  sources?: string[];
  status_steps?: string[];
  status?: string;
  content?: string;
  prompt?: string;
  channels?: any[];
  steps?: string[];
  highlights?: string[] | string;
  hashtags?: string[] | string;
  cta?: string;
  headline?: string;
  trace?: ChatResponse["trace"];
}

export interface AIConfig {
  has_server_key: boolean;
  base_url: string;
  model: string;
}

export function getAIConfig() {
  return fetchJSON<AIConfig>("/ai/config");
}

export function chatAIChat(
  messages: { role: string; content: string }[],
  apiKey = "",
  model = ""
): Promise<ChatResponse> {
  return fetchJSON<ChatResponse>(
    "/ai/chat/json",
    {
      method: "PUT",
      body: JSON.stringify({ messages, api_key: apiKey, model }),
    },
    120000,
  );
}

export function marketSearchListings(params: {
  q?: string;
  intent?: string;
  bhk?: string;
  building?: string;
  micro_market?: string;
  price_max?: number;
  price_min?: number;
  furnishing?: string;
  broker?: string;
  sort_by?: string;
  limit?: number;
  offset?: number;
}): Promise<any> {
  const searchParams = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "" && v !== null) {
      searchParams.set(k, String(v));
    }
  }
  return fetchJSON<any>(`/search/market?${searchParams}`);
}

export interface ParsedSearchQuery {
  bhk?: number | string | null;
  intent?: string | null;
  locality?: string | null;
  localities?: string[] | null;
  minPrice?: number | null;
  maxPrice?: number | null;
  furnishing?: string | null;
  building?: string | null;
}

export function parseSearchQuery(q: string): Promise<ParsedSearchQuery> {
  return fetchJSON<ParsedSearchQuery>(`/search/parse?q=${encodeURIComponent(q)}`);
}

export function searchMarketItems(
  q: string,
  resultType: "all" | "listings" | "requirements" = "all",
  limit = 50,
  offset = 0,
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({
    q,
    result_type: resultType,
    limit: String(limit),
    offset: String(offset),
  });
  return fetchJSON<{
    items: any[];
    total: number;
    parsed: ParsedSearchQuery;
    corridor?: { endpoints: string[]; localities: string[]; resolved: boolean };
  }>(
    `/search/market-items?${params.toString()}`,
    { signal },
    30000,
  );
}

export function getListingSources(listingId: number) {
  return fetchJSON<any[]>(`/listings/${listingId}/sources`);
}

export function getParsedSources(parsedId: number) {
  return fetchJSON<any[]>(`/parsed/${parsedId}/sources`);
}

export function getDashboardListings(limit = 20) {
  return fetchJSON<any[]>(`/dashboard/listings?limit=${limit}`);
}

export function getMyRequirements(limit = 200) {
  return fetchJSON<any[]>(`/my/requirements?limit=${limit}`);
}

export function getMyDeals(limit = 200) {
  return fetchJSON<any[]>(`/my/deals?limit=${limit}`);
}

export function mergeMyDeal(sourceSchema: string, sourceId: number, targetSchema: string, targetId: number) {
  return fetchJSON<{ ok: boolean }>("/my/deals/merge", {
    method: "POST",
    body: JSON.stringify({ source_schema: sourceSchema, source_id: sourceId, target_schema: targetSchema, target_id: targetId }),
  });
}

export function getDashboardSignals() {
  return fetchJSON<any>("/dashboard/signals");
}

// ── AI Suggestions ──────────────────────────────────────────────

export function getSuggestions(status = "pending", limit = 50, offset = 0) {
  return fetchJSON<any[]>(`/suggestions?status=${status}&limit=${limit}&offset=${offset}`);
}

export function actOnSuggestion(id: number, action: string, rejection_reason = "") {
  return fetchJSON<any>(`/suggestions/${id}/${action}`, {
    method: "POST",
    body: JSON.stringify({ rejection_reason }),
  });
}

export function batchActOnSuggestions(ids: number[], action: string, rejection_reason = "") {
  return fetchJSON<any>(`/suggestions/batch`, {
    method: "POST",
    body: JSON.stringify({ ids, action, rejection_reason }),
  });
}

export interface PromoteRequest {
  observation_id: number;
  channel: string;
  use_ai?: boolean;
  fields?: Record<string, unknown>;
  api_key?: string;
}

export interface PromoteResponse {
  channel: string;
  emoji: string;
  headline: string;
  body: string;
  highlights: string[];
  ai_enhanced: boolean;
}

export function promoteGenerate(req: PromoteRequest) {
  return fetchJSON<PromoteResponse>("/promote/generate", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export interface PromoteConfig {
  enable_ai_promo: boolean;
  enable_meta_publishing: boolean;
  meta_publish_available: boolean;
}

export function getPromoteConfig() {
  return fetchJSON<PromoteConfig>("/promote/config");
}

// ── PropAI Business API ───────────────────────────────────────────

export interface BusinessApiTeamMember {
  id: number;
  name: string;
  mobile_number: string;
  role: string;
  role_label: string;
  assigned_markets: string[];
  active: boolean;
  waba_identity: string;
  created_at: string;
  updated_at: string;
}

export interface BusinessApiTeamMemberInput {
  name: string;
  mobile_number: string;
  role: string;
  assigned_markets: string[];
  active: boolean;
  waba_identity: string;
}

export interface BusinessApiOverview {
  connection_status: string;
  whatsapp_business_number: string;
  shared_waba_number?: string;
  waba_owner?: "propai" | "broker" | "none";
  outbound_allowed?: boolean;
  connected_team_members: number;
  total_team_members: number;
  last_sync: string;
  messages_today: number;
  ai_requests_today: number;
  pending_conversations: number;
  outbound_messages: number;
  inbound_messages: number;
  webhook_health: string;
  token_status: string;
  waba: {
    phone_number_id: string;
    has_verify_token: boolean;
    has_access_token: boolean;
  };
}

export interface BusinessApiConfig {
  is_super_admin?: boolean;
  can_manage_platform?: boolean;
  whatsapp_business_number: string;
  shared_waba_number?: string;
  waba_owner?: "propai" | "broker" | "none";
  outbound_allowed?: boolean;
  phone_number_id: string;
  has_access_token: boolean;
  access_token_preview: string;
  has_verify_token: boolean;
  verify_token_preview: string;
  webhook_callback_url?: string;
}

export interface BusinessApiConfigInput {
  whatsapp_business_number?: string;
  phone_number_id?: string;
  access_token?: string;
  verify_token?: string;
  clear_access_token?: boolean;
  clear_verify_token?: boolean;
}

export function getBusinessApiOverview() {
  return fetchJSON<BusinessApiOverview>("/business-api/overview");
}

export function getBusinessApiConfig(timeoutMs = 8000) {
  return fetchJSON<BusinessApiConfig>("/business-api/config", undefined, timeoutMs);
}

export function saveBusinessApiConfig(config: BusinessApiConfigInput) {
  return fetchJSON<BusinessApiConfig>("/business-api/config", {
    method: "POST",
    body: JSON.stringify(config),
  });
}

export function getBusinessApiTeam() {
  return fetchJSON<BusinessApiTeamMember[]>("/business-api/team");
}

export function addBusinessApiTeamMember(member: BusinessApiTeamMemberInput) {
  return fetchJSON<BusinessApiTeamMember>("/business-api/team", {
    method: "POST",
    body: JSON.stringify(member),
  });
}

export function updateBusinessApiTeamMember(id: number, member: BusinessApiTeamMemberInput) {
  return fetchJSON<BusinessApiTeamMember>(`/business-api/team/${id}`, {
    method: "PATCH",
    body: JSON.stringify(member),
  });
}

export function getBusinessApiRoles() {
  return fetchJSON<Record<string, { label: string; permissions: string[] }>>("/business-api/roles");
}

export function getBusinessApiTools() {
  return fetchJSON<{ tools: string[] }>("/business-api/tools");
}

export function getBusinessApiConversations() {
  return fetchJSON<any[]>("/business-api/conversations");
}

export function getBusinessApiAudit() {
  return fetchJSON<any[]>("/business-api/audit");
}

// ── WhatsApp Audit ──────────────────────────────────────────────

export interface AuditDashboard {
  whatsapp_session: string;
  webhook_status: string;
  groups_discovered: number;
  groups_monitored: number;
  total_groups: number;
  live_groups: number;
  msgs_today: number;
  last_webhook: string;
  webhook_healthy: boolean;
  error_groups: number;
  duplicate_groups: number;
  attention_required: number;
  attention_breakdown: {
    inactive: number;
    duplicate: number;
    unnamed: number;
    error: number;
  };
  inactive_groups: number;
  unnamed_groups: number;
  failed_events: number;
  pending_enrichment: number;
  pending_ai_suggestions: number;
  avg_process_secs: number | null;
  msgs_per_min: number;
  parser_success_rate: number;
  queue_backlog: number;
}

export interface AuditTimelineEvent {
  source: string;
  ts: string;
  subtype: string;
  label: string;
  group_name?: string;
  ref?: number;
}

export interface AuditGroupCard {
  jid: string;
  name: string;
  status: string;
  health: string;
  error: string;
  messages: number;
  last_activity: string;
  observations: number;
  listings: number;
  requirements: number;
  markets_count: number;
  unknown_locations: number;
  coverage: number;
  active_brokers: number;
  senders_count: number;
  duplicate_pct: number;
  parsed: { city?: string; area?: string };
  allowed?: boolean;
  excluded?: boolean;
}

export interface AuditCaptureHealth {
  msgs_per_min: number;
  avg_process_secs: number | null;
  parser_success_rate: number;
  last_webhook: string;
  queue_backlog: number;
  pending_enrichment: number;
  pending_ai_suggestions: number;
  total_msgs_today: number;
  total_parsed_today: number;
  degraded?: boolean;
  stage?: {
    raw_messages: number;
    parsed_output: number;
    observations: number;
    observation_evidence: number;
    brokers: number;
  };
}

export interface AuditTopContributor {
  group_name: string;
  msg_count: number;
  unique_senders: number;
  last_msg: string;
}

export function getAuditDashboard() {
  return fetchJSON<AuditDashboard>("/audit/dashboard");
}

export function getAuditTimeline(limit = 50) {
  return fetchJSON<AuditTimelineEvent[]>(`/audit/timeline?limit=${limit}`);
}

export interface AuditGroupsResponse {
  groups: AuditGroupCard[];
  total_unique_senders: number;
  total_unique_participants: number;
  total_membership_rows: number;
  duplicate_memberships: number;
  connected_groups: number;
  posting_groups_24h: number;
  errors?: string[];
}

export interface AuditGroupOverlapPair {
  group_a: { jid: string; name: string; senders: number };
  group_b: { jid: string; name: string; senders: number };
  shared_senders: number;
  overlap_pct: number;
  keep: { jid: string; name: string; senders: number };
  skip: { jid: string; name: string; senders: number };
  reason: string;
}

export interface AuditGroupOverlapResponse {
  pairs: AuditGroupOverlapPair[];
  groups: { jid: string; name: string; senders: number }[];
  error?: string;
}

export function getAuditGroups(q = "", status = "") {
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (status) params.set("status", status);
  return fetchJSON<AuditGroupsResponse>(`/audit/groups?${params}`);
}

export function getAuditGroupDetail(jid: string) {
  return fetchJSON<any>(`/audit/groups/${encodeURIComponent(jid)}`);
}

export function getAuditGroupTimeline(jid: string) {
  return fetchJSON<any[]>(`/audit/groups/${encodeURIComponent(jid)}/timeline`);
}

export function getAuditDuplicates() {
  return fetchJSON<any[]>("/audit/duplicates");
}

export function getAuditGroupOverlap(limit = 20) {
  return fetchJSON<AuditGroupOverlapResponse>(`/audit/group-overlap?limit=${limit}`);
}

export function getAuditCaptureHealth() {
  return fetchJSON<AuditCaptureHealth>("/audit/capture-health");
}

export function getOptOutList() {
  return fetchJSON<string[]>("/groups/opt-out");
}

export function setOptOutList(entries: string[]) {
  return fetchJSON<any>("/groups/opt-out", {
    method: "POST",
    body: JSON.stringify(entries),
  });
}

export function clearOptOutList() {
  return fetchJSON<any>("/groups/opt-out", { method: "DELETE" });
}

export function getAllowlist() {
  return getOptOutList();
}

export function setAllowlist(entries: string[]) {
  return setOptOutList(entries);
}

export function clearAllowlist() {
  return clearOptOutList();
}

export interface AuditInsights {
  daily_flow: { date: string; posts: number; requirements: number; listings: number }[];
  markets: { name: string; posts: number; requirements: number; listings: number; brokers: number }[];
  brokers: { name: string; posts: number; listings: number; requirements: number; groups: number; markets: number; last_seen: string }[];
  exclusive_members: Record<string, number>;
  total_unique_brokers?: number;
  total_broker_appearances?: number;
}

export function getAuditInsights() {
  return fetchJSON<AuditInsights>("/audit/insights");
}

export interface AuditLatestRecord {
  id: number | string;
  time: string;
  conversation: string;
  sender: string;
  preview: string;
  stored: boolean;
}

export interface AuditIntelligence {
  network?: Record<string, number | string | boolean>;
  capture?: Record<string, number | string | boolean | AuditLatestRecord[]>;
  search_coverage?: Record<string, number | string>;
  learning?: Record<string, number | string | { term: string; learned_as: string }[]>;
}

export interface AuditSearchEvidence {
  count: number;
  first_seen: string;
  last_seen: string;
  groups: number;
  unique_senders: number;
  top_groups: { name: string; count: number }[];
  recent?: AuditLatestRecord[];
}

export function getAuditIntelligence() {
  return fetchJSON<AuditIntelligence>("/audit/intelligence", undefined, 60000);
}

export function getAuditSearchEvidence(q: string) {
  return fetchJSON<AuditSearchEvidence>(`/audit/search-evidence?q=${encodeURIComponent(q)}`);
}

export function getAuditTopContributors(limit = 10) {
  return fetchJSON<AuditTopContributor[]>(`/audit/top-contributors?limit=${limit}`);
}

// ═══════════════════════════════════════════════════════════════════════
// Client Management
// ═══════════════════════════════════════════════════════════════════════

export interface Client {
  id: number;
  name: string;
  phone?: string;
  email?: string;
  notes?: string;
  status?: string;
  created_at?: string;
  requirements?: ClientRequirement[];
  candidates?: ClientCandidate[];
}

export interface ClientRequirement {
  id: number;
  client_id: number;
  intent: string;
  bhk?: string;
  price_min?: number;
  price_max?: number;
  micro_market?: string;
  building_name?: string;
  area_sqft_min?: number;
  area_sqft_max?: number;
  furnishing?: string;
  use_type?: string;
  notes?: string;
  is_primary?: number;
}

export interface ClientCandidate {
  id: number;
  client_id: number;
  listing_id?: number;
  message_id?: number;
  building_name?: string;
  micro_market?: string;
  bhk?: string;
  price?: number;
  area_sqft?: number;
  furnishing?: string;
  confidence?: number;
  match_breakdown?: Record<string, any>;
  source_text?: string;
  notes?: string;
  status?: string;
  created_at?: string;
}

export interface ClientMessage {
  id: number;
  sender: string;
  message: string;
  timestamp: string;
  group_name: string;
  direction?: string;
  broker_name?: string;
  broker_phone?: string;
}

export interface MatchResult {
  requirement: ClientRequirement & { client_name: string; client_phone?: string };
  score: number;
  breakdown: Record<string, { match: boolean | string; score: number }>;
}

export function getClients(q: string = "") {
  return fetchJSON<Client[]>(`/clients?q=${encodeURIComponent(q)}`);
}

export function getClient(id: number) {
  return fetchJSON<Client>(`/clients/${id}`);
}

export function createClient(data: { name: string; phone?: string; email?: string; notes?: string }) {
  return fetchJSON<{ id: number; name: string }>("/clients", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function updateClient(id: number, data: Partial<Client>) {
  return fetchJSON<{ ok: boolean }>(`/clients/${id}`, {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export function addClientRequirement(clientId: number, data: Partial<ClientRequirement>) {
  return fetchJSON<{ id: number }>(`/clients/${clientId}/requirements`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function getClientCandidates(clientId: number, status?: string) {
  const q = status ? `?status=${status}` : "";
  return fetchJSON<ClientCandidate[]>(`/clients/${clientId}/candidates${q}`);
}

export function addClientCandidate(clientId: number, data: Partial<ClientCandidate>) {
  return fetchJSON<{ id: number } | { error: string }>(`/clients/${clientId}/candidates`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function matchClientsToListing(data: {
  price?: number;
  bhk?: string;
  micro_market?: string;
  area_sqft?: number;
  building_name?: string;
  furnishing?: string;
  intent?: string;
}) {
  return fetchJSON<{ matches: MatchResult[] }>("/clients/match", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export interface SavedView {
  id: number;
  slug: string;
  name?: string;
  label?: string;
  view_type?: "brokers" | "groups";
  description?: string;
  filters?: Record<string, any>;
  created_at?: string;
  updated_at?: string;
  is_default?: boolean;
  is_shared?: boolean;
}

export function getInboxSlugs() {
  return fetchJSON<SavedView[]>("/inbox/slugs");
}

export function getBrokerSummary(name: string, phone: string) {
  const params = new URLSearchParams();
  if (name) params.set("name", name);
  if (phone) params.set("phone", phone);
  return fetchJSON<any>(`/brokers/summary?${params.toString()}`);
}

export function getBrokersFeed(limit = 100, offset = 0, includeTotal = false) {
  return fetchJSON<any[] | { items: any[]; total: number }>(
    `/brokers/feed?limit=${limit}&offset=${offset}${includeTotal ? "&include_total=true" : ""}`
  );
}

export interface ClientMessagesResponse {
  messages: ClientMessage[];
  total: number;
}

export function getClientMessages(clientId: number) {
  return fetchJSON<ClientMessagesResponse>(`/clients/${clientId}/messages`);
}

export function getMarketItemsFeed(
  limit = 50,
  offset = 0,
  brokerKey?: string,
  signal?: AbortSignal,
  resultType: "all" | "listings" | "requirements" = "all",
  marketLocalities?: string[],
) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (brokerKey) params.set("broker_key", brokerKey);
  params.set("result_type", resultType);
  if (marketLocalities?.length) params.set("market_localities", marketLocalities.join(","));
  return fetchJSON<any[]>(`/inbox/items?${params.toString()}`, { signal }, 15000);
}

export type MarketPreferences = {
  primary_localities: string[];
  nearby_localities: string[];
  transaction_types: string[];
  asset_types: string[];
  onboarding_completed: boolean;
};

export function getMarketPreferences() {
  return fetchJSON<MarketPreferences | null>("/workspace/market-preferences");
}

export function saveMarketPreferences(preferences: Pick<MarketPreferences, "primary_localities" | "nearby_localities" | "transaction_types" | "asset_types">) {
  return fetchJSON<MarketPreferences>("/workspace/market-preferences", {
    method: "POST",
    body: JSON.stringify(preferences),
  });
}

export function getMarketItemDetails(itemId: number, sourceSchema: string, rawMessageId?: number, signal?: AbortSignal) {
  const params = new URLSearchParams({ source_schema: sourceSchema });
  if (rawMessageId) params.set("raw_message_id", String(rawMessageId));
  return fetchJSON<any>(`/inbox/items/${itemId}/details?${params.toString()}`, { signal }, 15000);
}

export function hideBroker(phone: string) {
  return fetchJSON<any>(`/brokers/${encodeURIComponent(phone)}/hide`, {
    method: "POST",
  });
}

export function unhideBroker(phone: string) {
  return fetchJSON<any>(`/brokers/${encodeURIComponent(phone)}/unhide`, {
    method: "POST",
  });
}

export function teachObservation(obsId: number, payload: any) {
  return fetchJSON<any>(`/observations/${obsId}/teach`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export interface ObservationBatch {
  id: number;
  status: string;
  created_at?: string | null;
  total_requests?: number;
  completed_count?: number;
  failed_count?: number;
  batch_api_id?: string | null;
  error_message?: string | null;
}

export function createObservationBatch(data: any) {
  return fetchJSON<any>("/observations/batch", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function listObservationBatches(limit = 20, offset = 0): Promise<ObservationBatch[]> {
  return fetchJSON<ObservationBatch[]>(`/observations/batches?limit=${limit}&offset=${offset}`);
}

export function checkBatchStatus(batchId: number | string) {
  return fetchJSON<any>(`/observations/batches/${batchId}/status`);
}

export function applyBatchResults(batchId: number | string, data: any = {}) {
  return fetchJSON<any>(`/observations/batches/${batchId}/apply`, {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export interface BrokerShareCardSnapshot {
  broker_name: string | null;
  phone_display?: string | null;
  total_observations?: number;
  supply_count?: number;
  demand_count?: number;
  first_seen?: string | null;
  last_active?: string | null;
  generated_at?: string | null;
  top_markets?: Array<{ micro_market: string; observation_count: number }> | null;
  top_groups?: Array<{ group_name: string; observation_count: number }> | null;
}

export function getBrokerShareCardSnapshot(phone: string): Promise<BrokerShareCardSnapshot> {
  return fetchJSON<BrokerShareCardSnapshot>(`/brokers/${phone}/share-card-snapshot`);
}

export interface TeamMember {
  id: number;
  name: string;
  email?: string;
  phone?: string;
  role: string;
  is_active: boolean;
  permission_keys?: string[];
  linked_broker_phone?: string;
}

export async function getTeamMembers(): Promise<{ members: TeamMember[] }> {
  return fetchJSON<{ members: TeamMember[] }>("/workspace/members");
}

export async function getCurrentTeamMember(): Promise<TeamMember> {
  return fetchJSON<TeamMember>("/workspace/me");
}

// ── Internal Notes ──────────────────────────────────────────────

export interface Note {
  id: number;
  entity_type: string;
  entity_id: string;
  body: string;
  mentioned_member_ids: number[];
  created_at: string;
  updated_at: string;
  author_id: number;
  author_name: string | null;
}

export function getNotes(entityType: string, entityId: string) {
  const params = new URLSearchParams({ entity_type: entityType, entity_id: entityId });
  return fetchJSON<{ notes: Note[] }>(`/notes?${params.toString()}`);
}

export function createNote(data: {
  entity_type: string;
  entity_id: string;
  body: string;
  mentioned_member_ids?: number[];
}) {
  return fetchJSON<{ ok: boolean }>("/notes", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function deleteNote(noteId: number) {
  return fetchJSON<{ ok: boolean }>(`/notes/${noteId}`, { method: "DELETE" });
}

// ── User Profile ─────────────────────────────────────────────────

export interface UserProfile {
  phone: string;
  first_name: string;
  last_name: string;
  email: string;
  city: string;
}

export function getProfile() {
  return fetchJSON<UserProfile>("/profile");
}

export function saveProfile(data: { first_name: string; last_name: string; email: string; city: string }) {
  return fetchJSON<UserProfile>("/profile", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export type SoundPreferences = {
  whatsapp: string;
  groups: string;
  connection: string;
  leads: string;
};

export function getSoundPreferences() {
  return fetchJSON<SoundPreferences>("/profile/sounds");
}

export function saveSoundPreferences(data: SoundPreferences) {
  return fetchJSON<SoundPreferences>("/profile/sounds", {
    method: "PUT",
    body: JSON.stringify(data),
  });
}

export function getCurrentOrg() {
  return fetchJSON<{ id: string; name?: string; slug?: string }>(`/orgs/current`);
}

export function updateOrganization(orgId: string, data: { name?: string }) {
  return fetchJSON<{ ok: boolean }>(`/orgs/${encodeURIComponent(orgId)}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export interface AuthMeResponse {
  authenticated: boolean;
  user?: any;
  organizations?: { id: string; name?: string; slug?: string }[];
  active_tenant?: string | null;
  is_super_admin?: boolean;
  role_check_available?: boolean;
}

export function getAuthMe() {
  return fetchJSON<AuthMeResponse>("/auth/me", undefined, 8000);
}


// ── System Usage ──────────────────────────────────────────────

export interface UsageStats {
  total_messages: number;
  total_parsed: number;
  total_listings: number;
  total_requirements: number;
  total_brokers: number;
  total_buildings: number;
  total_groups: number;
  total_chat_sessions: number;
  total_chat_messages: number;
  ai_requests_today: number;
  messages_today: number;
  last_sync: string | null;
  broker_phone: string | null;
}

export function getUsageStats() {
  return fetchJSON<UsageStats>("/usage");
}

export function logWorkspaceActivity(payload: {
  action: string;
  target_type?: string;
  target_id?: string;
  details?: Record<string, unknown>;
}) {
  return fetchJSON<{ id: number }>("/workspace/activity", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// ── Phone Management ───────────────────────────────────────────────

export interface Phone {
  id: number;
  organization_id: string;
  phone_number: string;
  instance_name: string;
  broker_id: string;
  is_active: boolean;
  self_chat_enabled: boolean;
  extraction_status?: "stopped" | "running" | "paused";
  connected_at: string;
  created_at: string;
  connected: boolean | null;
  live_status_available?: boolean;
  live_status_error?: string;
  status_stale?: boolean;
  connection_state: string;
  phone_number_live: string;
  registered_phone_number?: string;
  registered_display_label?: string;
  display_name: string;
  connected_since: string;
  last_message_at: string;
  qr_available: boolean;
  qr?: string;
  total_messages_received: number;
}

export function getPhones(includeLive = true, timeoutMs = API_TIMEOUT_MS) {
  const params = new URLSearchParams();
  if (!includeLive) params.set("include_live", "false");
  const qs = params.toString();
  return fetchJSON<{ phones: Phone[] }>(`/phones${qs ? `?${qs}` : ""}`, undefined, timeoutMs);
}

export function getPhone(phoneId: number) {
  return fetchJSON<Phone>(`/phones/${phoneId}`);
}

export function createPhone(data: { phone_number?: string; instance_name?: string }) {
  return fetchJSON<Phone>("/phones", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export function deletePhone(phoneId: number) {
  return fetchJSON<{ ok: boolean }>(`/phones/${phoneId}`, { method: "DELETE" });
}

export function resetPhone(phoneId: number) {
  // Keep destructive session resets same-origin so gateway errors remain
  // readable by the dashboard instead of being misreported as browser CORS.
  return fetchJSON<{
    ok: boolean;
    accepted?: boolean;
    message: string;
    reset_at?: string;
    pairing_required?: boolean;
    phone_number_cleared?: boolean;
    remote_unlink_confirmed?: boolean;
    remote_unlink_warning?: string | null;
  }>(
    `/phones/${phoneId}/reset`,
    { method: "POST" },
    API_TIMEOUT_MS,
  );
}

export function disconnectPhone(phoneId: number) {
  return fetchJSON<{ ok: boolean; message: string }>(`/phones/${phoneId}/disconnect`, { method: "POST" });
}

export function connectPhone(phoneId: number) {
  return fetchJSON<Phone>(`/phones/${phoneId}/connect`, { method: "POST" });
}

export function pairCodePhone(phoneId: number, phone: string) {
  // Keep pairing same-origin through Next's server-side API rewrite. Direct
  // browser requests to api.propai.live require CORS preflight because they
  // carry Authorization and X-Tenant-Id; a gateway error can omit CORS headers
  // and surface only as an opaque NetworkError.
  return fetchJSONWithRetry<any>(
    `/phones/${phoneId}/pair-code`,
    { method: "POST", body: JSON.stringify({ phone }) },
    API_TIMEOUT_MS,
    false,
  );
}

export function getPairCodePhoneStatus(phoneId: number) {
  return fetchJSONWithRetry<any>(
    `/phones/${phoneId}/pair-code/status`,
    { method: "GET" },
    8_000,
    false,
  );
}

export function updatePhone(phoneId: number, data: { instance_name?: string; self_chat_enabled?: boolean }) {
  return fetchJSON<Phone>(`/phones/${phoneId}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export interface AdminWhatsAppSession extends Phone {
  organizations?: {
    id: string;
    name: string;
    slug: string;
    is_active: boolean;
  } | null;
}

export function getAdminWhatsAppSessions() {
  return fetchJSON<{ sessions: AdminWhatsAppSession[] }>("/admin/whatsapp/sessions");
}

export function updateAdminWhatsAppSession(
  phoneId: number,
  data: { instance_name?: string; is_active?: boolean; self_chat_enabled?: boolean; extraction_status?: "paused" | "stopped" },
) {
  return fetchJSON<AdminWhatsAppSession>(`/admin/whatsapp/sessions/${phoneId}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}


export interface ProfilePictureResponse {
  ok: boolean;
  url?: string;
  jid?: string;
  id?: string;
  cached?: boolean;
  note?: string;
}

export function getProfilePicture(jid: string, brokerId?: string) {
  const params = new URLSearchParams();
  if (brokerId) params.set("broker_id", brokerId);
  const qs = params.toString();
  return fetchJSON<ProfilePictureResponse>(`/profile-picture/${encodeURIComponent(jid)}${qs ? "?" + qs : ""}`, undefined, 5000);
}

export interface PhoneDirectoryEntry {
  id: string;
  organization_id: string;
  broker_id: string;
  phone_number: string;
  display_label: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface PhoneDirectoryListResult {
  entries: PhoneDirectoryEntry[];
  cap: number;
  used: number;
}

export function getPhoneDirectory(orgId: string) {
  return fetchJSON<PhoneDirectoryListResult>(
    `/orgs/${encodeURIComponent(orgId)}/phone-directory`,
  );
}

export function addPhoneDirectory(
  orgId: string,
  data: { phone_number: string; display_label?: string | null },
) {
  return fetchJSON<PhoneDirectoryEntry>(
    `/orgs/${encodeURIComponent(orgId)}/phone-directory`,
    {
      method: "POST",
      body: JSON.stringify(data),
    },
  );
}

export function patchPhoneDirectory(
  orgId: string,
  entryId: string,
  data: { phone_number?: string; display_label?: string | null; is_active?: boolean },
) {
  return fetchJSON<PhoneDirectoryEntry>(
    `/orgs/${encodeURIComponent(orgId)}/phone-directory/${encodeURIComponent(entryId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(data),
    },
  );
}

export function removePhoneDirectory(orgId: string, entryId: string) {
  return fetchJSON<{ ok: boolean; message: string }>(
    `/orgs/${encodeURIComponent(orgId)}/phone-directory/${encodeURIComponent(entryId)}`,
    { method: "DELETE" },
  );
}
