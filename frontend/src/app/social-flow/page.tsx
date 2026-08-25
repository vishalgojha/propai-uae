"use client";

import { ChangeEvent, FormEvent, ReactNode, useEffect, useRef, useState } from "react";
import { ExternalLink, Paperclip, Send, Sparkles, X } from "lucide-react";
import { getAccessToken } from "@/lib/auth";
import { fetchFormData, fetchJSON } from "@/lib/api";

type Message = { role: "assistant" | "user"; text: string };
type Asset = {
  id: number;
  filename: string;
  mime_type: string;
  size_bytes: number;
  asset_kind: string;
  url: string;
};
type ListingContext = {
  id: number;
  source_schema: string;
  title: string;
  details: Record<string, unknown>;
  brief: string;
};
type PendingApproval = { token: string; action: string; params: Record<string, unknown>; summary: string };

const starterPrompts = [
  "Build a target audience and campaign plan for my latest listing",
  "Suggest 3 ad angles and a test budget for this property",
  "How are my Meta ads doing this week, and what should I change?",
];

function sizeLabel(bytes: number): string {
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function resultText(value: unknown): string {
  if (!value) return "";
  if (typeof value === "string") return value;
  const item = value as Record<string, unknown>;
  if (typeof item.narrative === "string") return item.narrative;
  if (typeof item.message === "string") return item.message;
  if (item.report && typeof item.report === "object") return resultText(item.report);
  return `\n\n${JSON.stringify(value, null, 2)}`;
}

function inlineMarkdown(value: string): ReactNode[] {
  const parts = value.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^)]+\))/g);
  return parts.map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={index} className="font-semibold text-white">{part.slice(2, -2)}</strong>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={index} className="rounded bg-white/10 px-1 py-0.5 text-emerald-200">{part.slice(1, -1)}</code>;
    const link = part.match(/^\[([^\]]+)\]\((https?:\/\/[^)]+)\)$/);
    if (link) return <a key={index} href={link[2]} target="_blank" rel="noreferrer" className="text-emerald-300 underline underline-offset-2">{link[1]}</a>;
    return <span key={index}>{part}</span>;
  });
}

function RichText({ value }: { value: string }) {
  return <div className="space-y-1.5">{value.split("\n").map((line, index) => {
    const heading = line.match(/^#{1,3}\s+(.+)$/);
    const bullet = line.match(/^\s*[-*]\s+(.+)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (!line.trim()) return <div key={index} className="h-1" />;
    if (heading) return <p key={index} className="pt-1 font-semibold text-white">{inlineMarkdown(heading[1])}</p>;
    if (bullet) return <p key={index} className="pl-4 before:mr-2 before:text-emerald-300 before:content-['•']">{inlineMarkdown(bullet[1])}</p>;
    if (numbered) return <p key={index} className="pl-1">{inlineMarkdown(line.trim())}</p>;
    return <p key={index}>{inlineMarkdown(line)}</p>;
  })}</div>;
}

export default function SocialFlowPage() {
  const [tokenReady, setTokenReady] = useState(false);
  const [messages, setMessages] = useState<Message[]>([
    {
      role: "assistant",
      text: "I’m your PropAI Ads Agent. Send me a property brief, upload the creative, or ask about an existing campaign. I’ll keep you in one approval-safe conversation.",
    },
  ]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [connectionStatus, setConnectionStatus] = useState<"connecting" | "connected" | "needs_setup" | "not_connected">("connecting");
  const [connectionMessage, setConnectionMessage] = useState("");
  const [activeTab, setActiveTab] = useState<"chat" | "ads">("chat");
  const [currentAds, setCurrentAds] = useState("");
  const [discoveringIds, setDiscoveringIds] = useState(false);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null);
  const [listingContext, setListingContext] = useState<ListingContext | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    getAccessToken().then((token) => {
      if (token) {
        window.sessionStorage.setItem("propai_social_flow_token", token);
        fetchJSON<{ assets: Asset[] }>("/social-flow/assets")
          .then((result) => setAssets(result.assets || []))
          .catch(() => undefined);
        fetchJSON<{ status: "connected" | "needs_setup" | "not_connected"; message?: string }>("/social-flow/connection")
          .then((result) => { setConnectionStatus(result.status); setConnectionMessage(result.message || ""); })
          .catch(() => setConnectionStatus("not_connected"));
      } else {
        setConnectionStatus("not_connected");
      }
      setTokenReady(true);
    });
  }, []);

  useEffect(() => {
    if (!tokenReady) return;
    const params = new URLSearchParams(window.location.search);
    const sourceSchema = params.get("listing_schema") || "";
    const sourceId = params.get("listing_id") || "";
    if (!sourceSchema || !sourceId) return;
    fetchJSON<ListingContext>(`/social-flow/listing-context?source_schema=${encodeURIComponent(sourceSchema)}&source_id=${encodeURIComponent(sourceId)}`)
      .then((context) => {
        setListingContext(context);
        setMessages((current) => [...current, { role: "assistant", text: `I loaded “${context.title}” from My Deals. I’ll use its verified details for the next campaign draft.` }]);
        setInput("Create a campaign for this listing");
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "I couldn’t load that My Deals listing."));
  }, [tokenReady]);

  async function uploadFiles(files: FileList | null) {
    if (!files?.length || busy) return;
    setBusy(true);
    setError("");
    try {
      for (const file of Array.from(files).slice(0, 8)) {
        const form = new FormData();
        form.set("file", file, file.name);
        const result = await fetchFormData<{ asset: Asset }>("/social-flow/assets", form);
        setAssets((current) => [result.asset, ...current.filter((item) => item.id !== result.asset.id)].slice(0, 8));
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "I couldn’t upload that file.");
    } finally {
      setBusy(false);
    }
  }

  async function askPropAI(prompt: string) {
    const text = prompt.trim();
    if (!text || busy) return;
    const effectivePrompt = listingContext
      ? `${text}\n\nSelected listing from My Deals (verified structured fields):\n${listingContext.brief}\nUse this listing as the campaign source. Do not invent missing fields; ask for anything required.`
      : text;
    setInput("");
    setError("");
    const nextMessages = [...messages, { role: "user" as const, text }];
    setMessages(nextMessages);
    setBusy(true);
    try {
      const result = await fetchJSON<{ content: string; approval?: PendingApproval | null; sdk_result?: unknown }>("/social-flow/agent", {
        method: "POST",
        body: JSON.stringify({
          prompt: effectivePrompt,
          asset_ids: assets.map((asset) => asset.id),
          messages: messages.slice(-12),
        }),
      });
      setMessages([...nextMessages, { role: "assistant", text: `${result.content}${resultText(result.sdk_result)}` }]);
      setPendingApproval(result.approval || null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "PropAI couldn’t complete that request.");
    } finally {
      setBusy(false);
    }
  }

  async function connectMeta() {
    if (busy) return;
    const authWindow = window.open("about:blank", "_blank");
    if (authWindow) authWindow.opener = null;
    setBusy(true);
    setError("");
    try {
      const result = await fetchJSON<{ authorization_url: string }>("/social-flow/meta-mcp/connect", { method: "POST" });
      if (!authWindow) throw new Error("Please allow pop-ups for this site to connect Meta in a new tab.");
      authWindow.location.href = result.authorization_url;
    } catch (reason) {
      authWindow?.close();
      setError(reason instanceof Error ? reason.message : "PropAI couldn’t start Meta connection.");
      setBusy(false);
    }
  }

  async function loadCurrentAds() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await fetchJSON<{ content: string; sdk_result?: unknown }>("/social-flow/agent", {
        method: "POST",
        body: JSON.stringify({
          prompt: "Show my current Meta campaigns with status, spend, and leads. Keep it concise.",
          asset_ids: [],
          messages: [],
        }),
      });
      setCurrentAds(`${result.content}${resultText(result.sdk_result)}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "I couldn’t load your current ads.");
    } finally {
      setBusy(false);
    }
  }

  async function discoverMetaIds() {
    if (discoveringIds || busy) return;
    setDiscoveringIds(true);
    setError("");
    try {
      const result = await fetchJSON<{ message: string; status: string; ids?: Record<string, string> }>("/social-flow/meta-discovery", { method: "POST" }, 180000);
      setMessages((current) => [...current, { role: "assistant", text: result.message }]);
      if (result.status === "found") {
        setConnectionStatus("connecting");
        const connection = await fetchJSON<{ status: "connected" | "needs_setup" | "not_connected" }>("/social-flow/connection");
        setConnectionStatus(connection.status);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "I couldn’t open the Meta lookup browser.");
    } finally {
      setDiscoveringIds(false);
    }
  }

  async function approveAction() {
    if (!pendingApproval || busy) return;
    setBusy(true);
    setError("");
    try {
      await fetchJSON("/social-flow/actions/execute", {
        method: "POST",
        body: JSON.stringify({ action: pendingApproval.action, params: pendingApproval.params, approval_token: pendingApproval.token }),
      });
      setMessages((current) => [...current, { role: "assistant", text: "Approved and sent to Social Flow. The requested Meta action completed successfully." }]);
      setPendingApproval(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The approved Meta action failed.");
    } finally {
      setBusy(false);
    }
  }

  function submit(event?: FormEvent) {
    event?.preventDefault();
    void askPropAI(input);
  }

  function handleFiles(event: ChangeEvent<HTMLInputElement>) {
    void uploadFiles(event.target.files);
    event.target.value = "";
  }

  return (
    <div className="propai-social-screen flex h-[calc(100dvh-44px)] flex-col overflow-hidden bg-[#090b0f] text-white">
      <nav className="flex shrink-0 gap-1 border-b border-white/10 bg-[#0d1117] px-4 py-2 sm:px-8" aria-label="Ads workspace">
        <button type="button" onClick={() => setActiveTab("chat")} className={`rounded-lg px-3 py-2 text-xs font-semibold ${activeTab === "chat" ? "bg-emerald-400 text-black" : "text-zinc-400 hover:bg-white/5 hover:text-white"}`}>Ads assistant</button>
        <button type="button" onClick={() => { setActiveTab("ads"); if (!currentAds) void loadCurrentAds(); }} className={`rounded-lg px-3 py-2 text-xs font-semibold ${activeTab === "ads" ? "bg-emerald-400 text-black" : "text-zinc-400 hover:bg-white/5 hover:text-white"}`}>Current ads</button>
      </nav>

      <main className="mx-auto flex min-h-0 w-full max-w-none flex-1 flex-col overflow-hidden px-4 py-4 sm:px-8 sm:py-5 lg:px-10">
        {activeTab === "ads" ? (
          <section className="min-h-0 flex-1 rounded-3xl border border-white/10 bg-white/[0.02] p-5 sm:p-7">
            <div className="flex items-center justify-between gap-3"><div><p className="text-sm font-semibold">Current ads</p><p className="mt-1 text-xs text-zinc-500">Live campaign status, spend, and leads from your connected Meta account.</p></div><button type="button" onClick={() => void loadCurrentAds()} disabled={busy} className="rounded-lg border border-white/15 px-3 py-2 text-xs text-zinc-300 hover:border-emerald-400/40 disabled:opacity-40">Refresh</button></div>
            {currentAds ? <pre className="mt-5 h-[calc(100%-72px)] overflow-auto whitespace-pre-wrap rounded-2xl border border-white/10 bg-[#11151c] p-4 text-sm leading-6 text-zinc-200">{currentAds}</pre> : <div className="mt-16 text-center text-sm text-zinc-500">{connectionStatus === "connected" ? "Loading your live campaigns…" : "Connect Meta setup in the assistant first, then your campaigns will appear here."}</div>}
          </section>
        ) : <>
        <section className={`propai-social-messages ${messages.length === 1 ? "propai-social-empty" : ""} min-h-0 flex-1 space-y-3 overflow-y-auto rounded-3xl border border-white/10 bg-white/[0.02] p-3 sm:p-5`}>
            {listingContext && <div className="rounded-xl border border-emerald-300/20 bg-emerald-300/[0.06] px-3 py-2 text-xs text-emerald-200">Listing attached from My Deals · {listingContext.title}</div>}
        {connectionStatus !== "connected" && <div className="propai-meta-setup"><div className="min-w-0"><p className="text-sm font-semibold text-amber-100">{connectionStatus === "needs_setup" ? "Connect Meta to manage ads" : "Meta connection needs attention"}</p><p className="mt-1 text-xs text-zinc-400">{connectionMessage || "Connect once to manage campaigns from this workspace."}</p></div><div className="propai-meta-actions"><button type="button" onClick={() => void connectMeta()} disabled={busy} className="rounded-lg bg-amber-200 px-3 py-2 text-xs font-semibold text-black disabled:opacity-50">Connect Meta</button><details><summary>More setup options</summary><div className="mt-2 flex flex-wrap gap-2"><button type="button" onClick={() => void discoverMetaIds()} disabled={discoveringIds} className="rounded-lg border border-white/15 px-3 py-2 text-xs text-zinc-300 hover:border-emerald-400/40 disabled:opacity-50">{discoveringIds ? "Looking up IDs…" : "Find IDs automatically"}</button><button type="button" onClick={() => setInput("Guide me to find my Meta Page ID and Ad Account ID")} className="rounded-lg border border-white/15 px-3 py-2 text-xs text-zinc-300 hover:border-emerald-400/40">Guide me</button><a href="https://business.facebook.com/settings/accounts" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 rounded-lg border border-white/15 px-3 py-2 text-xs text-zinc-300 hover:border-emerald-400/40">Ad accounts <ExternalLink className="h-3 w-3" /></a><a href="https://www.facebook.com/pages/?category=your_pages" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 rounded-lg border border-white/15 px-3 py-2 text-xs text-zinc-300 hover:border-emerald-400/40">Facebook Pages <ExternalLink className="h-3 w-3" /></a></div></details></div></div>}
          {messages.map((message, index) => <div key={`${message.role}-${index}`} className={`propai-social-message flex ${message.role === "user" ? "justify-end" : "justify-start"}`}><div className={`max-w-[92%] rounded-2xl px-4 py-3 text-sm leading-6 ${message.role === "user" ? "bg-emerald-400 text-black" : "border border-white/10 bg-[#11151c] text-zinc-200"}`}><RichText value={message.text} /></div></div>)}
          {busy && <div className="flex items-center gap-2 px-2 text-sm text-zinc-500"><Sparkles className="h-4 w-4 animate-pulse text-emerald-400" /> PropAI is working…</div>}
          {pendingApproval && <div className="rounded-2xl border border-amber-300/30 bg-amber-300/[0.08] p-4"><p className="font-semibold text-amber-200">Approval required</p><p className="mt-1 text-sm text-zinc-300">{pendingApproval.summary}</p><div className="mt-3 flex gap-2"><button type="button" onClick={() => void approveAction()} disabled={busy} className="rounded-lg bg-emerald-400 px-3 py-2 text-xs font-semibold text-black disabled:opacity-50">Approve action</button><button type="button" onClick={() => setPendingApproval(null)} disabled={busy} className="rounded-lg border border-white/15 px-3 py-2 text-xs text-zinc-300">Cancel</button></div></div>}
        </section>

        {error && <div className="mt-3 rounded-xl border border-red-400/20 bg-red-400/10 px-3 py-2 text-sm text-red-200">{error}</div>}

        {assets.length > 0 && <div className="mt-3 flex flex-wrap gap-2">{assets.map((asset) => <div key={asset.id} className="group flex items-center gap-2 rounded-xl border border-emerald-400/20 bg-emerald-400/[0.06] px-2 py-1.5 text-xs text-zinc-300">{asset.asset_kind === "image" && asset.url ? <img src={asset.url} alt={asset.filename} className="h-8 w-8 rounded-lg object-cover" /> : <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-white/10 text-[9px] uppercase">{asset.asset_kind}</span>}<span className="max-w-36 truncate" title={asset.filename}>{asset.filename}</span><span className="text-zinc-600">{sizeLabel(asset.size_bytes)}</span><button type="button" aria-label={`Remove ${asset.filename}`} onClick={() => setAssets((current) => current.filter((item) => item.id !== asset.id))} className="text-zinc-600 hover:text-white"><X className="h-3.5 w-3.5" /></button></div>)}</div>}

        <div className="propai-social-starters mt-3 flex flex-wrap gap-2">{starterPrompts.map((prompt) => <button key={prompt} type="button" onClick={() => setInput(prompt)} className="rounded-full border border-white/10 px-3 py-1.5 text-xs text-zinc-400 hover:border-emerald-400/40 hover:text-zinc-200">{prompt}</button>)}</div>

        <form onSubmit={submit} className="propai-social-composer mt-3 flex items-end gap-2 rounded-2xl border border-white/15 bg-[#11151c] p-2 shadow-2xl shadow-black/20">
          <input ref={fileInputRef} type="file" multiple accept="image/jpeg,image/png,image/webp,image/gif,video/mp4,video/quicktime,application/pdf" className="hidden" onChange={handleFiles} />
          <button type="button" aria-label="Attach creative media" onClick={() => fileInputRef.current?.click()} disabled={busy} className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-zinc-500 hover:bg-white/5 hover:text-emerald-300 disabled:opacity-40"><Paperclip className="h-4 w-4" /></button>
          <textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submit(); } }} rows={1} placeholder={assets.length ? "Ask PropAI about the attached creative…" : "Tell PropAI what you want to do with your Meta Ads…"} className="max-h-36 min-h-10 flex-1 resize-none bg-transparent py-2 text-sm text-white outline-none placeholder:text-zinc-500" />
          <button type="submit" disabled={!input.trim() || busy || !tokenReady} aria-label="Send" className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-emerald-400 text-black hover:bg-emerald-300 disabled:opacity-40"><Send className="h-4 w-4" /></button>
        </form>
        <p className="mt-2 text-center text-[11px] text-zinc-600">PropAI prepares actions for approval. Meta credentials stay server-side.</p>
        </>}
      </main>
    </div>
  );
}
