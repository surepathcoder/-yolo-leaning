/**
 * The realtime WebSocket connection.
 *
 * One socket per browser tab, shared through a Zustand store rather than
 * opened per component: a dashboard with six live widgets would otherwise hold
 * six sockets, six authentications and six copies of every message.
 *
 * Reconnection is the part that matters in this market. Connections on mobile
 * data and rural links drop constantly, and a dashboard that silently stops
 * updating is worse than one that says it is offline -- an operator watching a
 * frozen screen believes nothing is happening.
 */

import { useEffect } from "react";
import { create } from "zustand";

import { realtimeUrl, tokenStore } from "@/services/client";
import type { RealtimeMessage } from "@/types/api";

export type ConnectionState = "idle" | "connecting" | "open" | "reconnecting" | "closed";

type Listener = (message: RealtimeMessage) => void;

interface RealtimeState {
  status: ConnectionState;
  lastMessageAt: number | null;
  reconnectAttempts: number;
  /** Messages received this session. Surfaced in the connection tooltip. */
  messageCount: number;

  connect: () => void;
  disconnect: () => void;
  subscribe: (listener: Listener) => () => void;
  setCameraFilter: (cameraIds: string[]) => void;
}

let socket: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let intentionalClose = false;
const listeners = new Set<Listener>();

/** Capped exponential backoff. A long outage must not become a reconnect storm. */
function backoffDelay(attempt: number): number {
  return Math.min(1000 * 2 ** Math.max(attempt - 1, 0), 30_000);
}

export const useRealtimeStore = create<RealtimeState>((set, get) => ({
  status: "idle",
  lastMessageAt: null,
  reconnectAttempts: 0,
  messageCount: 0,

  connect() {
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
      return;
    }
    const token = tokenStore.access;
    if (!token) return;

    intentionalClose = false;
    set({ status: get().reconnectAttempts > 0 ? "reconnecting" : "connecting" });

    let ws: WebSocket;
    try {
      ws = new WebSocket(realtimeUrl());
    } catch {
      scheduleReconnect(set, get);
      return;
    }
    socket = ws;

    ws.onopen = () => {
      // The token goes in the first frame, not the URL: a query string ends up
      // in access logs and proxy logs, which is where credentials leak.
      ws.send(JSON.stringify({ type: "auth", token }));
    };

    ws.onmessage = (raw) => {
      let message: RealtimeMessage;
      try {
        message = JSON.parse(raw.data as string) as RealtimeMessage;
      } catch {
        return;
      }

      if (message.type === "connected") {
        set({ status: "open", reconnectAttempts: 0 });
        return;
      }
      if (message.type === "ping") {
        ws.send(JSON.stringify({ type: "pong" }));
        return;
      }
      if (message.type === "pong") return;

      set((state) => ({
        lastMessageAt: Date.now(),
        messageCount: state.messageCount + 1,
      }));
      for (const listener of listeners) {
        try {
          listener(message);
        } catch {
          // One broken subscriber must not stop the others receiving.
        }
      }
    };

    ws.onclose = () => {
      socket = null;
      if (intentionalClose) {
        set({ status: "closed" });
        return;
      }
      scheduleReconnect(set, get);
    };

    ws.onerror = () => {
      // `onclose` always follows, and handles the reconnect.
    };
  },

  disconnect() {
    intentionalClose = true;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    socket?.close();
    socket = null;
    set({ status: "closed", reconnectAttempts: 0 });
  },

  subscribe(listener) {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  },

  setCameraFilter(cameraIds) {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "set_camera_filter", camera_ids: cameraIds }));
    }
  },
}));

function scheduleReconnect(
  set: (partial: Partial<RealtimeState>) => void,
  get: () => RealtimeState,
): void {
  const attempts = get().reconnectAttempts + 1;
  set({ status: "reconnecting", reconnectAttempts: attempts });
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    useRealtimeStore.getState().connect();
  }, backoffDelay(attempts));
}

/**
 * Subscribe a component to realtime messages.
 *
 * The handler is kept in a ref-free closure deliberately: callers pass a
 * stable callback (or accept re-subscription), which keeps this hook simple
 * and avoids the stale-closure bug that a ref would paper over.
 */
export function useRealtimeEvents(
  handler: Listener,
  deps: React.DependencyList = [],
): void {
  const subscribe = useRealtimeStore((state) => state.subscribe);
  useEffect(() => {
    return subscribe(handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

/** Opens the shared connection for as long as the app is signed in. */
export function useRealtimeConnection(enabled: boolean): ConnectionState {
  const status = useRealtimeStore((state) => state.status);
  const connect = useRealtimeStore((state) => state.connect);
  const disconnect = useRealtimeStore((state) => state.disconnect);

  useEffect(() => {
    if (!enabled) {
      disconnect();
      return;
    }
    connect();

    // A tab restored from the background may hold a socket the OS quietly
    // killed. Reconnecting on visibility change is what stops a dashboard
    // looking alive but frozen after a laptop wakes.
    const onVisible = () => {
      if (document.visibilityState === "visible") connect();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", connect);

    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", connect);
    };
  }, [enabled, connect, disconnect]);

  return status;
}
