"use client";
import { withBasePath } from "@/lib/base-path";

import { useEffect } from "react";

export function ServiceWorkerRegister() {
  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;

    void navigator.serviceWorker
      .getRegistrations()
      .then(async (registrations) => {
        const workerUrl = new URL(withBasePath("/sw.js"), window.location.origin).href;
        await Promise.all(
          registrations
            .filter((registration) => {
              const scriptUrl = registration.active?.scriptURL || registration.waiting?.scriptURL;
              return scriptUrl && scriptUrl !== workerUrl;
            })
            .map((registration) => registration.unregister())
        );

        const registration = await navigator.serviceWorker.register(withBasePath("/sw.js"), { scope: withBasePath("/") });
        await registration.update().catch(() => undefined);
      })
      .catch((error) => {
        console.warn("[SW] Registration failed:", error);
      });
  }, []);

  return null;
}
