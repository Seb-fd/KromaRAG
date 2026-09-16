// k6 helpers: W3C traceparent minting + SSE frame parsing (specs/09 §3.4).
export function newTraceparent(sampled = true) {
  const hex = (n) => Array.from({ length: n }, () => Math.floor(Math.random() * 16).toString(16)).join('');
  return `00-${hex(32)}-${hex(16)}-${sampled ? '01' : '00'}`;
}

export function traceIdFromTraceparent(tp) {
  try {
    const parts = String(tp || '').split('-');
    const h = parts[1] || '';
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20, 32)}`;
  } catch (_) {
    return '';
  }
}

// Parse SSE text into [{event, data}] ignoring `: ping` comments.
export function parseSse(text) {
  const out = [];
  const body = String(text || '').replace(/\r\n/g, '\n');
  for (const blk of body.split('\n\n')) {
    let ev = null;
    let data = null;
    for (const line of blk.split('\n')) {
      if (line.startsWith('event:')) ev = line.slice(6).trim();
      else if (line.startsWith('data:')) data = line.slice(5).trim();
    }
    if (ev) {
      let parsed = null;
      try { parsed = data ? JSON.parse(data) : null; } catch (_) { parsed = data; }
      out.push({ event: ev, data: parsed });
    }
  }
  return out;
}

export function sseSummary(frames) {
  const kinds = frames.map((f) => f.event);
  return {
    kinds,
    sourceCount: kinds.filter((k) => k === 'source').length,
    tokenCount: kinds.filter((k) => k === 'token').length,
    doneCount: kinds.filter((k) => k === 'done').length,
    errorCount: kinds.filter((k) => k === 'error').length,
    done: frames.find((f) => f.event === 'done'),
  };
}
