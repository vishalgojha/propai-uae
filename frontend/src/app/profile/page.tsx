"use client";

export const dynamic = 'force-dynamic';

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ChevronDown, Smartphone, Save, Users, CreditCard, Key, Settings, Mail, User, Plus, Trash2 } from "lucide-react";
import { getProfile, saveProfile, getCurrentOrg, getPhones, getStats, isLiveWhatsAppConnection, updateOrganization, type Phone, getPhoneDirectory, addPhoneDirectory, patchPhoneDirectory, removePhoneDirectory, type PhoneDirectoryEntry } from "@/lib/api";
import { useAuth } from "@/lib/AuthProvider";
import { getSupabase } from "@/lib/auth";

const CITIES = [
  "Mumbai", "Delhi / NCR", "Bangalore", "Pune", "Hyderabad",
  "Chennai", "Ahmedabad", "Kolkata", "Surat", "Jaipur",
  "Lucknow", "Chandigarh", "Kochi", "Indore", "Nagpur", "Goa",
];

export function ProfilePage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user } = useAuth();
  const [profile, setProfile] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [hasStoredProfile, setHasStoredProfile] = useState(false);
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [email, setEmail] = useState("");
  const [city, setCity] = useState("");
  const [cityOpen, setCityOpen] = useState(false);
  const [customCity, setCustomCity] = useState("");
  const [workspaceName, setWorkspaceName] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [emailChangeOpen, setEmailChangeOpen] = useState(false);
  const [emailChangeSaving, setEmailChangeSaving] = useState(false);
  const [emailChangeMessage, setEmailChangeMessage] = useState<string | null>(null);
  const [emailChangeError, setEmailChangeError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [org, setOrg] = useState<{ id: string; name?: string; slug?: string } | null>(null);
  const [phones, setPhones] = useState<Phone[]>([]);
  const [directoryEntries, setDirectoryEntries] = useState<PhoneDirectoryEntry[]>([]);
  const [directoryCap, setDirectoryCap] = useState<3 | number>(3);
  const [directoryUsed, setDirectoryUsed] = useState(0);
  const [directoryLoading, setDirectoryLoading] = useState(false);
  const [directoryError, setDirectoryError] = useState<string | null>(null);
  const [quickStats, setQuickStats] = useState<{
    total_messages?: number;
    total_listings?: number;
    total_requirements?: number;
    total_brokers?: number;
    stats_available?: boolean;
  } | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [addPhone, setAddPhone] = useState("");
  const [addLabel, setAddLabel] = useState("");
  const [addSubmitting, setAddSubmitting] = useState(false);
  const [editEntryId, setEditEntryId] = useState<string | null>(null);
  const [editPhone, setEditPhone] = useState("");
  const [editLabel, setEditLabel] = useState("");
  const [editSubmitting, setEditSubmitting] = useState(false);
  const [removeEntryId, setRemoveEntryId] = useState<string | null>(null);
  const next = searchParams.get("next") || "";

  // Load profile from API on mount
  useEffect(() => {
    let mounted = true;
    const fullName = String(user?.user_metadata?.full_name || "").trim();
    const [defaultFirstName = "", ...defaultLastNameParts] = fullName.split(/\s+/);
    const stored = localStorage.getItem("propai_profile");
    let localProfile: any = null;
    if (stored) {
      try { localProfile = JSON.parse(stored); } catch {}
    }
    const baseProfile = {
      auth_user_id: user?.id || "",
      phone: localProfile?.auth_user_id === user?.id ? localProfile?.phone || user?.phone || "" : user?.phone || "",
      first_name: localProfile?.auth_user_id === user?.id ? localProfile?.first_name || defaultFirstName || "" : defaultFirstName || "",
      last_name: localProfile?.auth_user_id === user?.id ? localProfile?.last_name || defaultLastNameParts.join(" ") : defaultLastNameParts.join(" "),
      email: localProfile?.auth_user_id === user?.id ? localProfile?.email || user?.email || "" : user?.email || "",
      city: localProfile?.auth_user_id === user?.id ? localProfile?.city || "" : "",
    };

    const applyProfile = (data: any) => {
      setProfile(data);
      setFirstName(data.first_name || "");
      setLastName(data.last_name || "");
      // Login identity is owned by Supabase Auth. The profile table is only
      // descriptive and may lag until the user confirms an email change.
      setEmail(user?.email || data.email || "");
      const c = data.city || "";
      if (CITIES.includes(c)) {
        setCity(c);
        setCustomCity("");
      } else if (c) {
        setCity("__other__");
        setCustomCity(c);
      } else {
        setCity("");
        setCustomCity("");
      }
    };

    applyProfile(baseProfile);
    setHasStoredProfile(Boolean(localProfile?.auth_user_id === user?.id && localProfile?.first_name));

    getProfile().then((data: any) => {
      if (!mounted) return;
      if (data && data.first_name) {
        applyProfile({ ...baseProfile, ...data });
        setHasStoredProfile(true);
      }
      setLoading(false);
    }).catch(() => setLoading(false));
    return () => { mounted = false; };
  }, [user]);

  useEffect(() => {
    getCurrentOrg().then((data) => {
      setOrg(data);
      setWorkspaceName(data?.name || "");
    }).catch(() => {});
    getPhones(false, 12000).then((data) => setPhones(data.phones || [])).catch(() => {});
    getStats(12000).then((data) => setQuickStats(data)).catch(() => setQuickStats({ stats_available: false }));
  }, []);

  const reloadDirectory = (orgId: string) => {
    setDirectoryLoading(true);
    setDirectoryError(null);
    getPhoneDirectory(orgId)
      .then((res) => {
        setDirectoryEntries(res.entries || []);
        setDirectoryCap(res.cap ?? 3);
        setDirectoryUsed(res.used ?? (res.entries || []).length);
      })
      .catch((err: any) => {
        setDirectoryError(err?.message || "Failed to load phone directory");
        setDirectoryEntries([]);
        setDirectoryUsed(0);
      })
      .finally(() => setDirectoryLoading(false));
  };

  useEffect(() => {
    if (org?.id) reloadDirectory(org.id);
  }, [org?.id]);

  const markDirty = () => setDirty(true);

  const handleAddPhone = async () => {
    const phoneValue = String(addPhone ?? "").trim();
    const labelValue = String(addLabel ?? "").trim();
    if (!phoneValue || !org?.id || addSubmitting) return;
    setAddSubmitting(true);
    try {
      const created = await addPhoneDirectory(org.id, {
        phone_number: phoneValue,
        display_label: labelValue,
      });
      setDirectoryEntries((prev) => [...prev, created]);
      setDirectoryUsed((u) => u + 1);
      setAddOpen(false);
      setAddPhone("");
      setAddLabel("");
      setDirectoryError(null);
      router.push("/connections");
    } catch (err: any) {
      setDirectoryError(err?.message || "Failed to add phone");
    } finally {
      setAddSubmitting(false);
    }
  };

  const beginEdit = (entry: PhoneDirectoryEntry) => {
    setEditEntryId(entry.id);
    setEditPhone(entry.phone_number);
    setEditLabel(entry.display_label || "");
  };

  const handleEditPhone = async () => {
    if (!editEntryId || !org?.id || editSubmitting) return;
    setEditSubmitting(true);
    try {
      const updated = await patchPhoneDirectory(org.id, editEntryId, {
        phone_number: editPhone.trim() || undefined,
        display_label: editLabel.trim() || null,
      });
      setDirectoryEntries((prev) => prev.map((e) => (e.id === editEntryId ? updated : e)));
      setEditEntryId(null);
      setEditPhone("");
      setEditLabel("");
      setDirectoryError(null);
    } catch (err: any) {
      setDirectoryError(err?.message || "Failed to update phone");
    } finally {
      setEditSubmitting(false);
    }
  };

  const confirmRemove = async () => {
    if (!removeEntryId || !org?.id) return;
    try {
      await removePhoneDirectory(org.id, removeEntryId);
      setDirectoryEntries((prev) => prev.filter((e) => e.id !== removeEntryId));
      setDirectoryUsed((u) => Math.max(0, u - 1));
      setRemoveEntryId(null);
      setDirectoryError(null);
    } catch (err: any) {
      setDirectoryError(err?.message || "Failed to remove phone");
    }
  };

  const finalCity = city === "__other__" ? customCity.trim() : (city || profile?.city || "");
  const cityMissing = !finalCity;

  const handleChangeEmail = async (e: React.FormEvent) => {
    e.preventDefault();
    const nextEmail = newEmail.trim().toLowerCase();
    const currentEmail = (user?.email || email).trim().toLowerCase();
    setEmailChangeMessage(null);
    setEmailChangeError(null);
    if (!nextEmail || nextEmail === currentEmail) {
      setEmailChangeError("Enter a different email address.");
      return;
    }
    setEmailChangeSaving(true);
    try {
      const { error } = await getSupabase().auth.updateUser({ email: nextEmail });
      if (error) throw error;
      setEmailChangeMessage("Confirmation link sent. Your login email will change after you confirm it.");
      setNewEmail("");
    } catch (error) {
      setEmailChangeError(error instanceof Error ? error.message : "Could not start the email change.");
    } finally {
      setEmailChangeSaving(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!firstName.trim() || !email.trim() || cityMissing) return;
    setSaving(true);
    setSaved(false);
    const finalWorkspaceName = workspaceName.trim() || org?.name || "";
    const data = { first_name: firstName.trim(), last_name: lastName.trim(), email: email.trim(), city: finalCity };
    try {
      let savedProfile = null;
      if (profile?.phone || user?.id) {
        savedProfile = await saveProfile(data);
      }
      if (org?.id && finalWorkspaceName && finalWorkspaceName !== org.name) {
        await updateOrganization(org.id, { name: finalWorkspaceName });
        setOrg((prev) => prev ? { ...prev, name: finalWorkspaceName } : prev);
      }
      const localProfile = {
        auth_user_id: user?.id || "",
        phone: savedProfile?.phone || profile?.phone || "",
        ...data,
      };
      localStorage.setItem("propai_profile", JSON.stringify(localProfile));
      window.dispatchEvent(new Event("propai_profile_updated"));
      setProfile(localProfile);
      setHasStoredProfile(true);
      setSaved(true);
      setDirty(false);
      if (next && next !== "/profile") {
        router.push(next);
        return;
      }
      setTimeout(() => setSaved(false), 2000);
    } catch (error) {
      alert(error instanceof Error ? error.message : "Failed to save profile");
    }
    finally { setSaving(false); }
  };

  if (loading) return <div className="h-[calc(100vh-4rem)] flex items-center justify-center text-zinc-500">Loading...</div>;

  return (
    <div className="h-[calc(100vh-4rem)] overflow-y-auto bg-black">
      {/* Sticky Header */}
      <header className="sticky top-0 z-20 bg-black/95 backdrop-blur border-b border-white/10">
        <div className="max-w-[1400px] mx-auto px-4 lg:px-6 py-4 flex items-center justify-between gap-4">
          <div>
            <h1 className="text-lg font-bold text-white">Profile</h1>
            <p className="text-sm text-zinc-500">Personal details and account settings</p>
          </div>
          <div className="flex items-center gap-3 shrink-0">
            {saved && <span className="text-xs text-emerald-400 bg-emerald-400/10 px-2 py-1 rounded">Saved</span>}
            {dirty && !saving && (
              <span className="text-xs text-amber-400 bg-amber-400/10 px-2 py-1 rounded">Unsaved changes</span>
            )}
            <button
              type="submit"
              form="profile-form"
              disabled={saving || !firstName.trim() || !email.trim() || cityMissing || (!dirty && hasStoredProfile && !next)}
              className="flex items-center gap-2 px-4 py-2 bg-emerald-400 text-black rounded-lg text-sm font-bold min-h-[40px] disabled:opacity-50 transition-opacity shrink-0"
            >
              <Save className="w-4 h-4" />
              {saving ? "Saving..." : next ? "Save & Continue" : "Save Changes"}
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-[1400px] mx-auto px-4 lg:px-6 pb-8">
        <form id="profile-form" onSubmit={handleSubmit} className="grid lg:grid-cols-[1fr_380px] gap-6">
          {/* Left Column - Personal Details */}
          <div className="space-y-6">
            <section className="rounded-2xl border border-white/10 p-6">
              <h2 className="flex items-center gap-2 text-sm font-bold text-white mb-5">
                <User className="w-4 h-4 text-emerald-400" />
                Personal Details
              </h2>
              
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider">First Name *</label>
                  <input
                    value={firstName}
                    onChange={(e) => { setFirstName(e.target.value); markDirty(); }}
                    required
                    className="mt-1 w-full rounded-lg border border-white/10 bg-zinc-800/50 px-3 py-2.5 text-sm text-white placeholder-zinc-500 outline-none focus:border-emerald-500/50 transition-colors"
                  />
                </div>
                <div>
                  <label className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider">Last Name</label>
                  <input
                    value={lastName}
                    onChange={(e) => { setLastName(e.target.value); markDirty(); }}
                    className="mt-1 w-full rounded-lg border border-white/10 bg-zinc-800/50 px-3 py-2.5 text-sm text-white placeholder-zinc-500 outline-none focus:border-emerald-500/50 transition-colors"
                  />
                </div>
              </div>

              <div>
                <div className="flex items-center justify-between gap-3">
                  <label htmlFor="profile-email" className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider">Login email</label>
                  <span className="text-[10px] text-zinc-600">Managed by sign-in</span>
                </div>
                <div className="mt-1 flex items-center gap-2">
                  <Mail className="w-4 h-4 text-zinc-500 shrink-0" />
                  <input
                    id="profile-email"
                    type="email"
                    value={email}
                    readOnly
                    aria-readonly="true"
                    className="flex-1 cursor-not-allowed rounded-lg border border-white/10 bg-zinc-900/70 px-3 py-2.5 text-sm text-zinc-400 placeholder-zinc-500 outline-none"
                    placeholder="your@email.com"
                  />
                </div>
                <p className="mt-1.5 text-xs text-zinc-600">
                  To change the email used to sign in, use a verified account-security flow.
                </p>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider">City *</label>
                  <div className="relative mt-1">
                    <button
                      type="button"
                      onClick={() => setCityOpen((open) => !open)}
                      className="flex w-full items-center justify-between rounded-lg border border-white/10 bg-zinc-800/50 px-3 py-2.5 text-left text-sm text-white outline-none transition-colors hover:bg-zinc-800 focus:border-emerald-500/50"
                    >
                      <span className={city ? "text-white" : "text-zinc-500"}>
                        {city === "__other__" ? customCity || "Other" : city || "Select your city"}
                      </span>
                      <ChevronDown className={`h-4 w-4 text-zinc-500 transition-transform ${cityOpen ? "rotate-180" : ""}`} />
                    </button>
                    {cityOpen && (
                      <div className="absolute left-0 right-0 top-full z-50 mt-1 max-h-64 overflow-y-auto rounded-lg border border-white/10 bg-zinc-950 py-1 shadow-2xl">
                        <button
                          type="button"
                          onClick={() => { setCity(""); setCustomCity(""); setCityOpen(false); markDirty(); }}
                          className="block w-full px-3 py-2 text-left text-sm text-zinc-500 transition-colors hover:bg-white/5 hover:text-white"
                        >
                          Select your city
                        </button>
                        {CITIES.map((c) => (
                          <button
                            key={c}
                            type="button"
                            onClick={() => { setCity(c); setCustomCity(""); setCityOpen(false); markDirty(); }}
                            className={`block w-full px-3 py-2 text-left text-sm transition-colors hover:bg-emerald-400/10 hover:text-emerald-300 ${city === c ? "bg-emerald-400/10 text-emerald-300" : "text-zinc-200"}`}
                          >
                            {c}
                          </button>
                        ))}
                        <button
                          type="button"
                          onClick={() => { setCity("__other__"); setCityOpen(false); markDirty(); }}
                          className={`block w-full px-3 py-2 text-left text-sm transition-colors hover:bg-emerald-400/10 hover:text-emerald-300 ${city === "__other__" ? "bg-emerald-400/10 text-emerald-300" : "text-zinc-200"}`}
                        >
                          Other
                        </button>
                      </div>
                    )}
                  </div>
                  {city === "__other__" && (
                    <input
                      value={customCity}
                      onChange={(e) => { setCustomCity(e.target.value); markDirty(); }}
                      autoFocus
                      className="mt-2 w-full rounded-lg border border-white/10 bg-zinc-800/50 px-3 py-2.5 text-sm text-white placeholder-zinc-500 outline-none focus:border-emerald-500/50 transition-colors"
                      placeholder="Type your city"
                    />
                  )}
                </div>
                <div>
                  <label className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider">WhatsApp Number</label>
                  <div className="mt-1 flex items-center gap-2 rounded-lg border border-white/10 bg-zinc-800/50 px-3 py-2.5 text-sm text-zinc-300">
                    <Smartphone className="w-4 h-4 text-zinc-500 shrink-0" />
                    <span className="font-mono text-white">
                      {(() => {
                        const live = phones.find((p) => isLiveWhatsAppConnection(p));
                        const isPlaceholder = (value?: string) => !value || value.startsWith("Unpaired:");
                        const connectionNumber = phones
                          .flatMap((p) => [p.phone_number_live, p.phone_number])
                          .find((value) => !isPlaceholder(value));
                        const registeredNumber = phones.find((p) => p.registered_phone_number)?.registered_phone_number
                          || directoryEntries[0]?.phone_number;
                        const candidate = live?.phone_number_live || connectionNumber || registeredNumber || profile?.phone || "";
                        return candidate || "Not linked yet";
                      })()}
                    </span>
                  </div>
                </div>
              </div>
            </section>

            {false && <>
            {/* WhatsApp Phone Directory */}
            <section className="rounded-2xl border border-white/10 p-6">
              <div className="flex items-center justify-between mb-5">
                <h2 className="flex items-center gap-2 text-sm font-bold text-white">
                  <Smartphone className="w-4 h-4 text-emerald-400" />
                  WhatsApp Phone Directory
                </h2>
                <span className="text-[11px] text-zinc-500 shrink-0">
                  {directoryUsed} / {directoryCap} used
                </span>
              </div>
              <p className="text-xs text-zinc-500 mb-4">
                Add the WhatsApp numbers your brokers use. Connection (pair / reset / repair) is managed from the
                <button type="button" onClick={() => router.push("/connections")} className="mx-1 underline underline-offset-2 hover:text-emerald-400">Connections</button>
                page. Removing here permanently deletes the connection.
              </p>

              {directoryError && (
                <div className="mb-4 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
                  {directoryError}
                </div>
              )}

              <ul className="space-y-3">
                {directoryLoading ? (
                  <li className="text-xs text-zinc-500">Loading…</li>
                ) : directoryEntries.length === 0 ? (
                  <li className="text-xs text-zinc-500">No numbers added yet.</li>
                ) : (
                  directoryEntries.map((entry) => (
                    <li key={entry.id} className="rounded-lg border border-white/10 bg-zinc-800/40 p-3 flex items-center gap-3">
                      <Smartphone className="w-4 h-4 text-zinc-500 shrink-0" />
                      <div className="flex-1 min-w-0">
                        {editEntryId === entry.id ? (
                          <div className="grid grid-cols-1 sm:grid-cols-[1fr_1fr_auto] gap-2">
                            <input
                              value={editPhone}
                              onChange={(e) => { setEditPhone(e.target.value); markDirty(); }}
                              required
                              className="rounded-md border border-white/10 bg-zinc-900 px-2 py-1.5 text-sm text-white outline-none focus:border-emerald-500/50"
                              placeholder="+9715XXXXXXX / +91XXXXXXXXXX"
                            />
                            <input
                              value={editLabel}
                              onChange={(e) => { setEditLabel(e.target.value); markDirty(); }}
                              className="rounded-md border border-white/10 bg-zinc-900 px-2 py-1.5 text-sm text-white outline-none focus:border-emerald-500/50"
                              placeholder="Label (e.g. Sales)"
                            />
                            <div className="flex gap-2 justify-end">
                              <button
                                type="button"
                                onClick={() => { setEditEntryId(null); setEditPhone(""); setEditLabel(""); }}
                                className="px-2 py-1 rounded-md text-[11px] text-zinc-400 hover:bg-white/5"
                              >
                                Cancel
                              </button>
                              <button
                                type="button"
                                onClick={() => void handleEditPhone()}
                                disabled={editSubmitting || !editPhone.trim()}
                                className="px-2 py-1 rounded-md bg-emerald-400 text-black text-[11px] font-bold disabled:opacity-50"
                              >
                                {editSubmitting ? "Saving…" : "Save"}
                              </button>
                            </div>
                          </div>
                        ) : (
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="font-mono text-white text-sm">{entry.phone_number || "—"}</span>
                            {entry.display_label && (
                              <span className="text-[11px] text-zinc-300 bg-white/5 px-2 py-0.5 rounded">{entry.display_label}</span>
                            )}
                            {!entry.is_active && (
                              <span className="text-[11px] text-amber-300 bg-amber-400/10 px-2 py-0.5 rounded">Disabled</span>
                            )}
                          </div>
                        )}
                        <div className="text-[10px] text-zinc-500 font-mono mt-1 break-all">broker: {entry.broker_id}</div>
                      </div>
                      {editEntryId !== entry.id && (
                        <div className="flex gap-1 shrink-0">
                          <button
                            type="button"
                            onClick={() => beginEdit(entry)}
                            className="px-2 py-1 rounded-md text-[11px] text-zinc-300 hover:bg-white/5"
                          >
                            Edit
                          </button>
                          <button
                            type="button"
                            onClick={() => setRemoveEntryId(entry.id)}
                            className="px-2 py-1 rounded-md text-[11px] text-red-300 hover:bg-red-500/10"
                          >
                            <Trash2 className="w-3 h-3 inline" /> Remove
                          </button>
                        </div>
                      )}
                    </li>
                  ))
                )}
              </ul>

              {addOpen ? (
                <div className="mt-4 grid grid-cols-1 sm:grid-cols-[1fr_1fr_auto] gap-2">
                  <input
                    value={addPhone}
                    onChange={(e) => setAddPhone(e.target.value)}
                    placeholder="+9715XXXXXXX / +91XXXXXXXXXX"
                    required
                    className="rounded-md border border-white/10 bg-zinc-900 px-2 py-1.5 text-sm text-white outline-none focus:border-emerald-500/50"
                  />
                  <input
                    value={addLabel}
                    onChange={(e) => setAddLabel(e.target.value)}
                    placeholder="Label (optional)"
                    className="rounded-md border border-white/10 bg-zinc-900 px-2 py-1.5 text-sm text-white outline-none focus:border-emerald-500/50"
                  />
                  <div className="flex gap-2 justify-end">
                    <button
                      type="button"
                      onClick={() => { setAddOpen(false); setAddPhone(""); setAddLabel(""); }}
                      className="px-2 py-1 rounded-md text-[11px] text-zinc-400 hover:bg-white/5"
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleAddPhone()}
                      disabled={addSubmitting || !addPhone.trim()}
                      className="px-2 py-1 rounded-md bg-emerald-400 text-black text-[11px] font-bold disabled:opacity-50"
                    >
                      {addSubmitting ? "Adding…" : "Add"}
                    </button>
                  </div>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => setAddOpen(true)}
                  disabled={directoryUsed >= directoryCap}
                  className="mt-4 w-full flex items-center justify-center gap-1 rounded-lg border border-dashed border-white/15 px-3 py-2 text-xs font-medium text-zinc-300 hover:bg-white/5 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <Plus className="w-3 h-3" />
                  {directoryUsed >= directoryCap
                    ? `Cap reached (${directoryCap} numbers)`
                    : `Add WhatsApp number (${directoryCap - directoryUsed} slot${directoryCap - directoryUsed === 1 ? "" : "s"} left)`}
                </button>
              )}

              {removeEntryId && (
                <div className="fixed inset-0 z-30 flex items-center justify-center bg-black/70 p-4">
                  <div className="w-full max-w-sm rounded-2xl border border-white/10 bg-zinc-950 p-5">
                    <h3 className="text-sm font-bold text-white">Remove this number?</h3>
                    <p className="mt-2 text-xs text-zinc-400">
                      The WhatsApp connection will be disconnected and the broker record deleted from this workspace.
                      Use the Connections page to reset or reconnect instead — only remove when you're done with this number.
                    </p>
                    <div className="mt-4 flex justify-end gap-2">
                      <button
                        type="button"
                        onClick={() => setRemoveEntryId(null)}
                        className="px-3 py-1.5 rounded-md text-[11px] text-zinc-300 hover:bg-white/5"
                      >
                        Cancel
                      </button>
                      <button
                        type="button"
                        onClick={confirmRemove}
                        className="px-3 py-1.5 rounded-md bg-red-500 text-white text-[11px] font-bold"
                      >
                        Remove permanently
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </section></>}

            {/* Workspace Info (read-only) */}
            <section className="rounded-2xl border border-white/10 p-6">
              <h2 className="flex items-center gap-2 text-sm font-bold text-white mb-5">
                <Settings className="w-4 h-4 text-emerald-400" />
                Agency / Workspace
              </h2>
              <div className="mb-4">
                <label className="text-[11px] font-semibold text-zinc-500 uppercase tracking-wider">Agency / Workspace Name</label>
                <input
                  value={workspaceName}
                  onChange={(e) => { setWorkspaceName(e.target.value); markDirty(); }}
                  className="mt-1 w-full rounded-lg border border-white/10 bg-zinc-800/50 px-3 py-2.5 text-sm text-white placeholder-zinc-500 outline-none focus:border-emerald-500/50 transition-colors"
                  placeholder="e.g. Ananta Realty"
                />
                <p className="mt-1 text-[11px] text-zinc-500">This is the workspace name shown across the app.</p>
              </div>
              <dl className="space-y-3 text-sm">
                <div className="flex justify-between">
                  <dt className="text-zinc-500">Role</dt>
                  <dd className="text-white font-medium">Owner</dd>
                </div>
                {org && (
                  <div className="flex justify-between items-start gap-4">
                    <dt className="text-zinc-500 shrink-0">Workspace ID</dt>
                    <dd className="text-white font-mono text-[11px] text-right break-all">{org.id}</dd>
                  </div>
                )}
                {org?.name && (
                  <div className="flex justify-between">
                    <dt className="text-zinc-500">Workspace</dt>
                    <dd className="text-white font-medium">{org.name}</dd>
                  </div>
                )}
                <div className="flex justify-between">
                  <dt className="text-zinc-500">Timezone</dt>
                  <dd className="text-white font-medium">Asia/Kolkata (IST)</dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-zinc-500">Language</dt>
                  <dd className="text-white font-medium">English</dd>
                </div>
              </dl>
            </section>
          </div>

          {/* Right Column - Account Actions */}
          <div className="space-y-6">
            <section className="rounded-2xl border border-white/10 p-6 h-fit sticky top-24">
              <h2 className="flex items-center gap-2 text-sm font-bold text-white mb-5">
                <Key className="w-4 h-4 text-emerald-400" />
                Account
              </h2>
              <nav className="space-y-2">
                <button
                  type="button"
                  onClick={() => router.push("/profile/team")}
                  className="w-full flex items-center gap-3 px-3 py-3 rounded-lg text-left hover:bg-white/5 transition-colors group"
                >
                  <div className="w-8 h-8 flex items-center justify-center">
                    <Users className="w-4 h-4 text-zinc-400 group-hover:text-emerald-400 transition-colors" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium text-white">Team Management</div>
                    <div className="text-xs text-zinc-500">Members, roles, permissions</div>
                  </div>
                </button>

                <button
                  type="button"
                  onClick={() => router.push("/profile/billing")}
                  className="w-full flex items-center gap-3 px-3 py-3 rounded-lg text-left hover:bg-white/5 transition-colors group"
                >
                  <div className="w-8 h-8 flex items-center justify-center">
                    <CreditCard className="w-4 h-4 text-zinc-400 group-hover:text-emerald-400 transition-colors" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium text-white">Billing & Plan</div>
                    <div className="text-xs text-zinc-500">Subscription, usage, invoices</div>
                  </div>
                </button>

                <button
                  type="button"
                  onClick={() => router.push("/waba")}
                  className="w-full flex items-center gap-3 px-3 py-3 rounded-lg text-left hover:bg-white/5 transition-colors group"
                >
                  <div className="w-8 h-8 flex items-center justify-center">
                    <Key className="w-4 h-4 text-zinc-400 group-hover:text-emerald-400 transition-colors" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium text-white">API Keys</div>
                    <div className="text-xs text-zinc-500">WhatsApp Business API, webhooks</div>
                  </div>
                </button>
              </nav>
            </section>

            <section className="rounded-2xl border border-white/10 p-6">
              <div className="flex items-start gap-3">
                <Mail className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
                <div className="min-w-0 flex-1">
                  <h2 className="text-sm font-bold text-white">Login email</h2>
                  <p className="mt-1 break-all text-xs text-zinc-500">{user?.email || email || "No email configured"}</p>
                </div>
              </div>

              {!emailChangeOpen ? (
                <button
                  type="button"
                  onClick={() => { setEmailChangeOpen(true); setEmailChangeMessage(null); setEmailChangeError(null); }}
                  className="mt-4 w-full rounded-lg border border-white/10 px-3 py-2 text-xs font-medium text-zinc-200 transition-colors hover:border-emerald-400/40 hover:bg-emerald-400/5"
                >
                  Change login email
                </button>
              ) : (
                <form onSubmit={handleChangeEmail} className="mt-4 space-y-3">
                  <label htmlFor="new-login-email" className="text-[11px] font-semibold uppercase tracking-wider text-zinc-500">New email address</label>
                  <input
                    id="new-login-email"
                    type="email"
                    value={newEmail}
                    onChange={(event) => { setNewEmail(event.target.value); setEmailChangeError(null); }}
                    autoComplete="email"
                    required
                    className="w-full rounded-lg border border-white/10 bg-zinc-800/50 px-3 py-2.5 text-sm text-white outline-none transition-colors placeholder:text-zinc-600 focus:border-emerald-500/50"
                    placeholder="you@company.com"
                  />
                  <p className="text-xs leading-5 text-zinc-500">We’ll send a confirmation link. The current login stays active until the new address is verified.</p>
                  {emailChangeMessage && <p role="status" className="text-xs leading-5 text-emerald-300">{emailChangeMessage}</p>}
                  {emailChangeError && <p role="alert" className="text-xs leading-5 text-red-300">{emailChangeError}</p>}
                  <div className="flex gap-2">
                    <button
                      type="submit"
                      disabled={emailChangeSaving || !newEmail.trim()}
                      className="rounded-lg bg-emerald-400 px-3 py-2 text-xs font-bold text-black transition-opacity disabled:opacity-50"
                    >
                      {emailChangeSaving ? "Sending…" : "Send confirmation"}
                    </button>
                    <button
                      type="button"
                      onClick={() => { setEmailChangeOpen(false); setNewEmail(""); setEmailChangeError(null); }}
                      className="rounded-lg px-3 py-2 text-xs text-zinc-400 hover:bg-white/5 hover:text-white"
                    >
                      Cancel
                    </button>
                  </div>
                </form>
              )}
            </section>

            {/* Quick Stats */}
            <section className="rounded-2xl border border-white/10 p-6">
              <h2 className="text-sm font-bold text-white mb-4">Quick Stats</h2>
              <dl className="grid grid-cols-2 gap-4 text-sm">
                <div>
                  <dt className="text-zinc-500">Messages</dt>
                  <dd className="text-white font-bold">
                    {quickStats?.stats_available ? Number(quickStats.total_messages || 0).toLocaleString() : quickStats ? "Unavailable" : "Loading…"}
                  </dd>
                </div>
                <div>
                  <dt className="text-zinc-500">Listings</dt>
                  <dd className="text-white font-bold">
                    {quickStats?.stats_available ? Number(quickStats.total_listings || 0).toLocaleString() : quickStats ? "Unavailable" : "Loading…"}
                  </dd>
                </div>
                <div>
                  <dt className="text-zinc-500">Requirements</dt>
                  <dd className="text-white font-bold">
                    {quickStats?.stats_available ? Number(quickStats.total_requirements || 0).toLocaleString() : quickStats ? "Unavailable" : "Loading…"}
                  </dd>
                </div>
                <div>
                  <dt className="text-zinc-500">Brokers</dt>
                  <dd className="text-white font-bold">
                    {quickStats?.stats_available ? Number(quickStats.total_brokers || 0).toLocaleString() : quickStats ? "Unavailable" : "Loading…"}
                  </dd>
                </div>
              </dl>
              {quickStats && !quickStats.stats_available && (
                <p className="mt-4 text-[11px] leading-4 text-zinc-600">Live counts are temporarily unavailable.</p>
              )}
            </section>
          </div>
        </form>
      </main>
    </div>
  );
}

export default function LegacyProfilePage() {
  const router = useRouter();
  useEffect(() => { router.replace("/account?tab=profile"); }, [router]);
  return null;
}
