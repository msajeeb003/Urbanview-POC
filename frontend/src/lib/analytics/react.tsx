"use client";

/**
 * React access to the one tracker of the page. The tracker is a module singleton so React's
 * development double-mounting never starts two visits or double-counts `map_loaded`.
 */
import { useCallback, useEffect } from "react";

import { setSessionIdProvider } from "@/lib/api/client";
import { api } from "@/lib/api/endpoints";
import type { AnalyticsEventName } from "@/lib/api/types";
import { safeLocalStorage } from "@/lib/storage";

import { Identity } from "./identity";
import { Tracker, type EventProperties } from "./tracker";

let tracker: Tracker | null = null;

// every API call from the browser carries the anonymous session id (`X-Session-ID`)
if (typeof window !== "undefined") setSessionIdProvider(() => getTracker().currentSessionId(window));

export function getTracker(): Tracker {
  if (!tracker) {
    tracker = new Tracker({
      identity: new Identity(safeLocalStorage),
      send: (batch, { keepalive }) => api.events(batch, { keepalive, timeoutMs: 10_000 }),
      enabled: typeof window !== "undefined" && process.env.NEXT_PUBLIC_ANALYTICS_ENABLED !== "false",
      onDrop: (events, error) => {
        if (process.env.NODE_ENV !== "production") {
          console.warn("[analytics] batch rejected by the API and dropped", error, events);
        }
      },
    });
  }
  return tracker;
}

/** Mount once near the root: starts the visit and flushes on page hide / unload. */
export function AnalyticsBoot() {
  useEffect(() => {
    const t = getTracker();
    t.start(window);
    // No stop() on unmount: the tracker lives for the page, including strict-mode remounts.
  }, []);
  return null;
}

export function useTrack() {
  return useCallback((name: AnalyticsEventName, properties?: EventProperties) => {
    getTracker().track(name, properties);
  }, []);
}

/** Emit an event once per page load after the first render of the calling component. */
export function useTrackOnce(key: string, name: AnalyticsEventName, properties?: EventProperties) {
  useEffect(() => {
    getTracker().trackOnce(key, name, properties);
    // properties are read once by design
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, name]);
}
