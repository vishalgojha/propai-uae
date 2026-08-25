"use client";

import { type FormEvent, useMemo, useState } from "react";
import { Check, MessageSquare, Send } from "lucide-react";

type RequirementCaptureProps = {
  query: string;
};

type SubmitState = "idle" | "submitting" | "success" | "error";

function normalizePhone(value: string): string {
  return value.replace(/[^\d+]/g, "").trim();
}

export default function RequirementCapture({ query }: RequirementCaptureProps) {
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [email, setEmail] = useState("");
  const [timeline, setTimeline] = useState("");
  const [details, setDetails] = useState(query);
  const [contactMe, setContactMe] = useState(false);
  const [shareWithBroker, setShareWithBroker] = useState(false);
  const [message, setMessage] = useState("");
  const [submitState, setSubmitState] = useState<SubmitState>("idle");

  const canSubmit = useMemo(() => {
    return Boolean(details.trim()) && Boolean(timeline.trim()) && (contactMe || shareWithBroker);
  }, [contactMe, details, shareWithBroker, timeline]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage("");

    if (!details.trim()) {
      setSubmitState("error");
      setMessage("Add the requirement you want us to track.");
      return;
    }

    if (!timeline.trim()) {
      setSubmitState("error");
      setMessage("Tell us the timeline you want to stay within.");
      return;
    }

    if (!contactMe && !shareWithBroker) {
      setSubmitState("error");
      setMessage("Choose at least one follow-up option.");
      return;
    }

    if (contactMe && !normalizePhone(phone) && !email.trim()) {
      setSubmitState("error");
      setMessage("Add a phone number or email if you want us to contact you.");
      return;
    }

    setSubmitState("submitting");

    try {
      const response = await fetch("/api/public/requirements", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          name,
          phone: normalizePhone(phone),
          email: email.trim(),
          query: details.trim(),
          timeline: timeline.trim(),
          contact_me: contactMe,
          share_with_broker: shareWithBroker,
        }),
      });

      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(payload?.error || "Failed to save requirement");
      }

      setSubmitState("success");
      setMessage("Requirement saved. We’ll keep an eye out and route matches to the right broker.");
    } catch (error) {
      setSubmitState("error");
      setMessage(error instanceof Error ? error.message : "Failed to save requirement");
    }
  }

  return (
    <form onSubmit={handleSubmit} className="rounded-3xl border border-white/10 bg-zinc-950/90 p-6 lg:p-8 shadow-[0_30px_90px_rgba(0,0,0,0.35)]">
      <div className="flex items-start gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-full bg-green-400/10 text-green-300">
          <MessageSquare className="h-5 w-5" aria-hidden="true" />
        </div>
        <div>
          <h2 className="text-xl font-semibold text-white">No exact match yet</h2>
          <p className="mt-2 text-sm leading-6 text-zinc-400">
            Tell us the exact requirement, the timeline you care about, and whether you want us to contact you or just share it with a broker.
          </p>
        </div>
      </div>

      <div className="mt-6 grid grid-cols-1 gap-4">
        <label className="grid gap-2">
          <span className="text-sm text-zinc-300">What are you looking for?</span>
          <textarea
            value={details}
            onChange={(event) => setDetails(event.target.value)}
            rows={4}
            placeholder="2 BHK in Dubai Marina budget 120k"
            className="w-full rounded-2xl border border-white/10 bg-black/80 px-4 py-3 text-[15px] text-white placeholder:text-zinc-500 focus:border-green-400 focus:outline-none focus:ring-1 focus:ring-green-400"
          />
        </label>

        <label className="grid gap-2 sm:max-w-sm">
          <span className="text-sm text-zinc-300">Timeline</span>
          <input
            value={timeline}
            onChange={(event) => setTimeline(event.target.value)}
            placeholder="Within 2 weeks, before month-end, flexible..."
            className="w-full rounded-2xl border border-white/10 bg-black/80 px-4 py-3 text-[15px] text-white placeholder:text-zinc-500 focus:border-green-400 focus:outline-none focus:ring-1 focus:ring-green-400"
          />
        </label>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="grid gap-2">
            <span className="text-sm text-zinc-300">Name</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Your name"
              className="w-full rounded-2xl border border-white/10 bg-black/80 px-4 py-3 text-[15px] text-white placeholder:text-zinc-500 focus:border-green-400 focus:outline-none focus:ring-1 focus:ring-green-400"
            />
          </label>
          <label className="grid gap-2">
            <span className="text-sm text-zinc-300">Phone or email</span>
            <input
              value={phone}
              onChange={(event) => setPhone(event.target.value)}
              placeholder="WhatsApp / mobile number"
              className="w-full rounded-2xl border border-white/10 bg-black/80 px-4 py-3 text-[15px] text-white placeholder:text-zinc-500 focus:border-green-400 focus:outline-none focus:ring-1 focus:ring-green-400"
            />
            <input
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="Email, if preferred"
              className="w-full rounded-2xl border border-white/10 bg-black/80 px-4 py-3 text-[15px] text-white placeholder:text-zinc-500 focus:border-green-400 focus:outline-none focus:ring-1 focus:ring-green-400"
            />
          </label>
        </div>

        <div className="grid grid-cols-1 gap-3 rounded-2xl border border-white/10 bg-black/60 p-4 sm:grid-cols-2">
          <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-white/10 bg-zinc-950/80 p-4">
            <input
              type="checkbox"
              checked={contactMe}
              onChange={(event) => setContactMe(event.target.checked)}
              className="mt-1 h-4 w-4 rounded border-white/20 bg-black text-green-400 focus:ring-green-400"
            />
            <span>
              <span className="block text-sm font-medium text-white">Contact me if a match appears</span>
              <span className="mt-1 block text-xs leading-5 text-zinc-500">
                We’ll use your timeline to decide when to follow up.
              </span>
            </span>
          </label>
          <label className="flex cursor-pointer items-start gap-3 rounded-xl border border-white/10 bg-zinc-950/80 p-4">
            <input
              type="checkbox"
              checked={shareWithBroker}
              onChange={(event) => setShareWithBroker(event.target.checked)}
              className="mt-1 h-4 w-4 rounded border-white/20 bg-black text-green-400 focus:ring-green-400"
            />
            <span>
              <span className="block text-sm font-medium text-white">Share this with a broker</span>
              <span className="mt-1 block text-xs leading-5 text-zinc-500">
                We’ll keep it in the broker network so matching inventory can surface faster.
              </span>
            </span>
          </label>
        </div>
      </div>

      <div className="mt-6 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-xs leading-5 text-zinc-500">
          If we find a match within your timeline, we’ll use the preference above to decide how to follow up.
        </p>
        <button
          type="submit"
          disabled={!canSubmit || submitState === "submitting"}
          className="inline-flex items-center justify-center gap-2 rounded-2xl bg-green-400 px-5 py-3 text-sm font-semibold text-black transition-colors hover:bg-green-300 disabled:cursor-not-allowed disabled:bg-green-400/40"
        >
          {submitState === "success" ? <Check className="h-4 w-4" aria-hidden="true" /> : <Send className="h-4 w-4" aria-hidden="true" />}
          {submitState === "submitting" ? "Saving..." : submitState === "success" ? "Saved" : "Send requirement"}
        </button>
      </div>

      {message && (
        <div
          className={`mt-4 rounded-2xl border px-4 py-3 text-sm ${
            submitState === "success"
              ? "border-green-400/20 bg-green-400/10 text-green-200"
              : "border-rose-400/20 bg-rose-400/10 text-rose-200"
          }`}
        >
          {message}
        </div>
      )}
    </form>
  );
}
