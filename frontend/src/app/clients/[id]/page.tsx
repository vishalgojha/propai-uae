"use client";

import { useEffect, useState } from "react";
import { withBasePath } from "@/lib/base-path";
import { useParams } from "next/navigation";
import * as api from "@/lib/api";
import EntityProfileShell from "@/components/EntityProfileShell";
import {
  Mail,
  Phone,
  UserCheck,
  MessageSquare,
  Inbox,
  ChevronDown,
  ChevronUp,
} from "lucide-react";

export default function ClientDetailPage() {
  const params = useParams();
  const clientId = Number(params.id);

  const [client, setClient] = useState<api.Client | null>(null);
  const [messages, setMessages] = useState<api.ClientMessage[]>([]);
  const [totalMessages, setTotalMessages] = useState(0);
  const [loading, setLoading] = useState(true);
  const [msgLoading, setMsgLoading] = useState(true);
  const [showMessages, setShowMessages] = useState(true);
  const [showRequirements, setShowRequirements] = useState(true);
  const [showCandidates, setShowCandidates] = useState(true);

  useEffect(() => {
    if (!clientId) return;
    setLoading(true);
    api.getClient(clientId).then(setClient).catch(() => setClient(null)).finally(() => setLoading(false));
  }, [clientId]);

  useEffect(() => {
    if (!clientId) return;
    setMsgLoading(true);
    api.getClientMessages(clientId)
      .then((data) => {
        setMessages(data.messages || []);
        setTotalMessages(data.total || 0);
      })
      .catch(() => {})
      .finally(() => setMsgLoading(false));
  }, [clientId]);

  if (loading) {
    return <div className="min-h-screen bg-[#070b0e] text-white p-6 text-center text-xs text-zinc-500 py-12">Loading client...</div>;
  }

  if (!client) {
    return (
      <div className="min-h-screen bg-[#070b0e] text-white p-6">
        <div className="max-w-3xl mx-auto text-center py-12">
          <div className="text-sm text-zinc-500">Client not found.</div>
          <a href={withBasePath("/clients")} className="text-xs text-[#3EE88A] hover:underline mt-2 inline-block">Back to clients</a>
        </div>
      </div>
    );
  }

  return (
    <EntityProfileShell
      title={client.name}
      subtitle={client.notes || "Client profile with linked requirements, candidates, and message history."}
      backHref="/clients"
      backLabel="Back to Clients"
      metrics={[
        { label: "Status", value: client.status || "active", tone: "good" },
        { label: "Requirements", value: client.requirements?.length || 0, tone: "accent" },
        { label: "Candidates", value: client.candidates?.length || 0 },
        { label: "Messages", value: totalMessages, tone: "accent" },
      ]}
    >
      <div className="rounded-2xl border border-white/10 bg-zinc-900 p-5">
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-500">
          {client.phone && (
            <span className="flex items-center gap-1">
              <Phone className="h-3 w-3" strokeWidth={1.5} />
              <span className="font-mono">{client.phone}</span>
            </span>
          )}
          {client.email && (
            <span className="flex items-center gap-1">
              <Mail className="h-3 w-3" strokeWidth={1.5} />
              <span>{client.email}</span>
            </span>
          )}
        </div>
      </div>

      {client.requirements && client.requirements.length > 0 && (
        <div className="rounded-2xl border border-white/10 bg-zinc-900 overflow-hidden">
          <button
            onClick={() => setShowRequirements(!showRequirements)}
            className="w-full flex items-center justify-between px-5 py-3 text-sm font-bold text-white hover:bg-white/5 transition-colors"
          >
            <span>Requirements ({client.requirements.length})</span>
            {showRequirements ? <ChevronUp className="w-4 h-4" strokeWidth={1.5} /> : <ChevronDown className="w-4 h-4" strokeWidth={1.5} />}
          </button>
          {showRequirements && (
            <div className="px-5 pb-4 space-y-2">
              {client.requirements.map((req) => (
                <div key={req.id} className="bg-zinc-800 rounded-lg p-3 text-xs">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="font-bold text-white">{req.intent}</span>
                    {req.bhk && <span className="text-[#3EE88A]">{req.bhk}</span>}
                  </div>
                  <div className="text-zinc-500 space-y-0.5">
                    {req.micro_market && <div>Location: {req.micro_market}</div>}
                    {req.building_name && <div>Building: {req.building_name}</div>}
                    {(req.price_min || req.price_max) && (
                      <div>Budget: {req.price_min ? `₹${req.price_min} Lakh` : ""} - {req.price_max ? `₹${req.price_max} Lakh` : ""}</div>
                    )}
                    {req.notes && <div className="text-zinc-400 mt-1">{req.notes}</div>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {client.candidates && client.candidates.length > 0 && (
        <div className="rounded-2xl border border-white/10 bg-zinc-900 overflow-hidden">
          <button
            onClick={() => setShowCandidates(!showCandidates)}
            className="w-full flex items-center justify-between px-5 py-3 text-sm font-bold text-white hover:bg-white/5 transition-colors"
          >
            <span>Candidates ({client.candidates.length})</span>
            {showCandidates ? <ChevronUp className="w-4 h-4" strokeWidth={1.5} /> : <ChevronDown className="w-4 h-4" strokeWidth={1.5} />}
          </button>
          {showCandidates && (
            <div className="px-5 pb-4 space-y-2">
              {client.candidates.map((c) => (
                <div key={c.id} className="bg-zinc-800 rounded-lg p-3 text-xs">
                  <div className="flex items-center gap-2 mb-1">
                    {c.building_name && <span className="font-bold text-white">{c.building_name}</span>}
                    {c.bhk && <span className="text-[#3EE88A]">{c.bhk}</span>}
                    {c.price != null && <span className="text-[#f59e0b]">₹{c.price}L</span>}
                  </div>
                  <div className="text-zinc-500">
                    {c.micro_market && <span>{c.micro_market}</span>}
                    {c.status && <span className="ml-2 capitalize">Status: {c.status}</span>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="rounded-2xl border border-white/10 bg-zinc-900 overflow-hidden">
        <button
          onClick={() => setShowMessages(!showMessages)}
          className="w-full flex items-center justify-between px-5 py-3 text-sm font-bold text-white hover:bg-white/5 transition-colors"
        >
          <span className="flex items-center gap-2">
            <Inbox className="w-4 h-4" strokeWidth={1.5} />
            Messages ({totalMessages})
          </span>
          {showMessages ? <ChevronUp className="w-4 h-4" strokeWidth={1.5} /> : <ChevronDown className="w-4 h-4" strokeWidth={1.5} />}
        </button>

        {showMessages && (
          <div className="px-5 pb-4">
            {msgLoading ? (
              <div className="text-center text-xs text-zinc-500 py-8">Loading messages...</div>
            ) : messages.length === 0 ? (
              <div className="text-center py-8">
                <MessageSquare className="w-8 h-8 mx-auto text-zinc-500 mb-2" strokeWidth={1.5} />
                <div className="text-xs text-zinc-500">No messages found for this client.</div>
                <div className="text-[10px] text-zinc-500 mt-1">
                  Messages will appear when the client's phone number matches incoming WhatsApp messages.
                </div>
              </div>
            ) : (
              <div className="space-y-2 max-h-[600px] overflow-y-auto">
                {totalMessages === 1 && (
                  <div className="text-center py-3">
                    <span className="text-[10px] text-[#475569] italic">Conversation started here</span>
                  </div>
                )}
                {messages.map((msg) => (
                  <div
                    key={msg.id}
                    className={`flex ${msg.direction === "outbound" ? "justify-end" : "justify-start"}`}
                  >
                    <div
                      className={`max-w-[80%] rounded-xl px-3.5 py-2 text-xs leading-relaxed ${
                        msg.direction === "outbound"
                          ? "bg-[#166534] text-green-50 rounded-br-sm"
                          : "bg-[#1e293b] text-white rounded-bl-sm"
                      }`}
                    >
                      <div className="whitespace-pre-wrap break-words">{msg.message}</div>
                      <div className={`flex items-center gap-1.5 mt-1.5 ${msg.direction === "outbound" ? "justify-end" : "justify-start"}`}>
                        <span className={`text-[9px] ${msg.direction === "outbound" ? "text-green-300" : "text-zinc-500"}`}>
                          {msg.direction === "outbound" ? "You" : client.name}
                        </span>
                        <span className="text-[8px] text-[#475569]">
                          {msg.timestamp ? new Date(msg.timestamp).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : ""}
                        </span>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </EntityProfileShell>
  );
}
