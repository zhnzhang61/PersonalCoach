export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText} on ${path}`);
  }
  return res.json() as Promise<T>;
}

export async function apiPost<T>(
  path: string,
  body?: Record<string, unknown>,
): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    cache: "no-store",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText} on ${path}`);
  }
  return res.json() as Promise<T>;
}

export async function apiPut<T>(
  path: string,
  body?: Record<string, unknown>,
): Promise<T> {
  const res = await fetch(path, {
    method: "PUT",
    cache: "no-store",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText} on ${path}`);
  }
  return res.json() as Promise<T>;
}

export async function apiDelete<T>(path: string): Promise<T> {
  const res = await fetch(path, { method: "DELETE", cache: "no-store" });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText} on ${path}`);
  }
  return res.json() as Promise<T>;
}

// -- Server-Sent Events client --------------------------------------
//
// Consumes a POST endpoint that returns text/event-stream. EventSource
// would be simpler but only supports GET — and our streaming endpoint
// is POST so the body carries thread_id + message + system_context.
//
// Frame parsing follows the spec: each frame is terminated by "\n\n";
// inside a frame, lines beginning with "data: " are concatenated and
// JSON-parsed. We rely on the producer (FastAPI) to emit one JSON-
// encoded payload per frame — multi-line `data:` continuations aren't
// emitted by our backend.

export type SSEEvent =
  | { type: "token"; content: string }
  | { type: "tool_call"; name: string }
  // A record_coach_fact write actually COMPLETED (backend emits on
  // on_tool_end) — drives the "档案已更新 ✓" badge. Actions, not words:
  // the model claiming it recorded something never produces this event.
  | { type: "fact_recorded"; area: string }
  | { type: "done" }
  | { type: "error"; message: string };

export async function streamSSE(
  path: string,
  body: Record<string, unknown>,
  onEvent: (ev: SSEEvent) => void,
): Promise<void> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText} on ${path}`);
  }
  if (!res.body) {
    throw new Error(`response has no body on ${path}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE frames are separated by blank lines (\n\n).
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const ev = parseFrame(frame);
        if (ev) onEvent(ev);
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function parseFrame(frame: string): SSEEvent | null {
  let data: string | null = null;
  for (const line of frame.split("\n")) {
    if (line.startsWith("data: ")) {
      data = line.slice(6);
    }
  }
  if (!data) return null;
  try {
    return JSON.parse(data) as SSEEvent;
  } catch {
    return null;
  }
}
