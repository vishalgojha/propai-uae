"use client";

import { useEffect, useRef, useCallback } from "react";
import { apiUrl } from "@/lib/base-path";
import { forceRefreshToken, getAccessToken } from "@/lib/auth";

type EventCallback = (event: { type: string; data: any; timestamp: string }) => void;

export function useEventStream(handlers: Record<string, EventCallback>) {
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const handlerMap = new Map<string, EventCallback>();
    for (const [eventType, cb] of Object.entries(handlers)) {
      handlerMap.set(eventType, cb);
    }

    const controller = new AbortController();
    abortRef.current = controller;
    let stopped = false;

    const dispatch = (block: string) => {
      let eventType = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) eventType = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (!data) return;
      const callback = handlerMap.get(eventType);
      if (!callback) return;
      try {
        callback(JSON.parse(data));
      } catch {
        // Ignore malformed event payloads; the stream remains usable.
      }
    };

    const connect = async () => {
      while (!stopped) {
        try {
          let token = await getAccessToken();
          const tenantId = typeof window !== "undefined"
            ? window.localStorage.getItem("propai_active_tenant")
            : null;
          let response = await fetch(apiUrl("/events"), {
            headers: {
              Accept: "text/event-stream",
              ...(token ? { Authorization: `Bearer ${token}` } : {}),
              ...(tenantId ? { "X-Tenant-Id": tenantId } : {}),
            },
            cache: "no-store",
            signal: controller.signal,
          });
          if (response.status === 401) {
            token = await forceRefreshToken();
            if (token) {
              response = await fetch(apiUrl("/events"), {
                headers: {
                  Accept: "text/event-stream",
                  Authorization: `Bearer ${token}`,
                  ...(tenantId ? { "X-Tenant-Id": tenantId } : {}),
                },
                cache: "no-store",
                signal: controller.signal,
              });
            }
          }
          if (!response.ok || !response.body) throw new Error(`events stream ${response.status}`);

          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          while (!stopped) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const blocks = buffer.split(/\r?\n\r?\n/);
            buffer = blocks.pop() || "";
            for (const block of blocks) dispatch(block);
          }
        } catch (error) {
          if (stopped || (error instanceof DOMException && error.name === "AbortError")) break;
        }
        if (!stopped) await new Promise((resolve) => window.setTimeout(resolve, 3000));
      }
    };

    void connect();
    return () => {
      stopped = true;
      controller.abort();
      abortRef.current = null;
    };
  }, []);
}
