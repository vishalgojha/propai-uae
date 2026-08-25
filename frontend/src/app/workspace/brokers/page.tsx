"use client";

import React, { useState, useEffect } from "react";
import { withBasePath } from "@/lib/base-path";
import { Eye, EyeOff, ArrowLeft, ChevronRight, User } from "lucide-react";
import { fetchJSON } from "@/lib/api";

async function fetchHiddenBrokers() {
  const data = await fetchJSON<any>("/brokers/hidden");
  return data.brokers;
}

async function unhideBroker(phone: string) {
  return fetchJSON<any>(`/brokers/${encodeURIComponent(phone)}/unhide`, {
    method: "POST",
  });
}

export default function HiddenBrokersPage() {
  const [brokers, setBrokers] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    fetchHiddenBrokers()
      .then(setBrokers)
      .catch(() => setMessage("Failed to load hidden brokers"))
      .finally(() => setLoading(false));
  }, []);

  const handleUnhide = async (phone: string, name: string) => {
    try {
      await unhideBroker(phone);
      setBrokers((prev) => prev.filter((b) => b.primary_phone === phone || b.phone === phone));
      setMessage(`Unhidden: ${name}`);
    } catch {
      setMessage("Failed to unhide broker");
    }
    setTimeout(() => setMessage(null), 3000);
  };

  return (
    <div className="min-h-screen bg-[#070b0e] text-white p-6">
      {/* Header */}
      <div className="max-w-3xl mx-auto mb-6">
        <a
          href={withBasePath("/workspace")}
          className="inline-flex items-center gap-1.5 text-[11px] text-zinc-500 hover:text-white transition-colors mb-4"
        >
          <ArrowLeft className="w-3.5 h-3.5" strokeWidth={1.5} />
          Back to Workspace
        </a>
        <h1 className="text-xl font-bold text-white">Hidden Brokers</h1>
        <p className="text-[11px] text-zinc-500 mt-1">
          Brokers you've hidden from the inbox. Unhide them to see their posts again.
        </p>
      </div>

      {message && (
        <div className="max-w-3xl mx-auto mb-4 bg-[#1e293b] border border-[#3EE88A]/30 text-[#3EE88A] px-4 py-2 rounded-lg text-xs font-semibold text-center">
          {message}
        </div>
      )}

      <div className="max-w-3xl mx-auto space-y-3">
        {loading ? (
          <div className="text-center text-xs text-zinc-500 py-12">Loading...</div>
        ) : brokers.length === 0 ? (
          <div className="text-center py-12">
            <EyeOff className="w-10 h-10 mx-auto text-zinc-500 mb-3" strokeWidth={1.5} />
            <div className="text-sm text-zinc-500">No hidden brokers</div>
            <div className="text-[10px] text-zinc-500 mt-1">
              When you hide a broker from the inbox, they'll appear here.
            </div>
          </div>
        ) : (
          brokers.map((b) => (
            <div
              key={b.id || b.primary_phone}
              className="bg-zinc-900 border border-white/10 rounded-xl p-4 flex items-center justify-between"
            >
              <div className="flex items-center gap-3 min-w-0">
                <div className="w-9 h-9 rounded-full bg-zinc-800 flex items-center justify-center shrink-0">
                  <User className="w-4 h-4 text-zinc-500" strokeWidth={1.5} />
                </div>
                <div className="min-w-0">
                  <div className="text-sm font-bold text-white truncate">{b.canonical_name || "Unknown"}</div>
                  <div className="text-[10px] text-zinc-500 font-mono">
                    {b.primary_phone
                      ? `+91 ${b.primary_phone.slice(-10, -5)} ${b.primary_phone.slice(-5)}`
                      : "—"}
                  </div>
                </div>
              </div>
              <button
                onClick={() => handleUnhide(b.primary_phone, b.canonical_name)}
                className="px-3 py-1.5 bg-[#166534] hover:bg-[#15803d] text-green-100 rounded-lg text-[10px] font-bold uppercase tracking-wider transition-colors flex items-center gap-1.5"
              >
                <Eye className="w-3 h-3" strokeWidth={1.5} />
                Unhide
              </button>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
