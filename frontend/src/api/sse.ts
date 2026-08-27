/**
 * A minimal server-sent-events reader over `fetch`.
 *
 * Written by hand rather than using `EventSource`, for one reason: the access
 * token travels in an Authorization header and `EventSource` cannot set
 * headers. The alternative — the token in the query string — puts a
 * credential into server logs and browser history, which is not a trade worth
 * making to save this file.
 *
 * Only the parts of the protocol the API actually emits are implemented:
 * `event:` and `data:` fields, comment lines (sse_starlette sends `: ping`
 * keepalives), and CRLF or LF line endings. No `id:`, no `retry:`, and no
 * reconnection — a dropped chat stream is reported, not silently resumed
 * against a server that would charge for the answer twice.
 */

export interface ServerSentEvent {
  event: string;
  data: string;
}

/**
 * A blank line, which is what ends an event.
 *
 * Matched against the accumulated buffer rather than against each chunk,
 * because a chunk boundary can fall between the CR and the LF of a single
 * line ending — and it does: the API sends CRLF, and normalising per chunk
 * silently stops finding any boundary at all when that happens.
 *
 * The three alternatives are whole terminator pairs. Writing it as
 * `(?:\r\n|\r|\n){2}` would be shorter and wrong: on a lone `\r\n` the engine
 * backtracks into `\r` + `\n` and declares every line ending a frame
 * boundary. It does mean a stream mixing terminators within one frame is not
 * recognised, which no single server does.
 */
const FRAME_BOUNDARY = /\r\n\r\n|\n\n|\r\r/;
const LINE_BREAK = /\r\n|\n|\r/;

function parseFrame(frame: string): ServerSentEvent | null {
  let event = "message";
  const dataLines: string[] = [];

  for (const line of frame.split(LINE_BREAK)) {
    // A comment. sse_starlette's keepalive arrives as one of these, and it
    // exists precisely so proxies do not close an idle stream.
    if (line.startsWith(":")) continue;

    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    // "Remove a single leading space" is the spec's rule, not "trim".
    const rest = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");

    if (field === "event") event = rest;
    else if (field === "data") dataLines.push(rest);
  }

  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join("\n") };
}

export async function* readEvents(response: Response): AsyncGenerator<ServerSentEvent> {
  const body = response.body;
  if (body === null) return;

  // TextDecoderStream, not a decoder per chunk: it carries the state needed to
  // reassemble a multi-byte character split across a chunk boundary.
  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += value;

      for (;;) {
        const boundary = FRAME_BOUNDARY.exec(buffer);
        if (boundary === null) break;

        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        const parsed = parseFrame(frame);
        if (parsed !== null) yield parsed;
      }
    }

    // A final frame with no trailing blank line. The API always sends one,
    // but a stream that ends politely should not lose its last event.
    const trailing = parseFrame(buffer);
    if (trailing !== null) yield trailing;
  } finally {
    reader.releaseLock();
  }
}
