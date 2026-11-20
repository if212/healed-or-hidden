'use strict';
// Wait until a page has gone quiet: the study runner's quiet.py, for Playwright Test.
//
// A page is quiet when its document has finished loading, no request it made is still open,
// and, for a given time, nothing has happened in it: no DOM change, no finished request, no
// load event. quiet.js (the study runner's file, byte for byte) keeps the record inside the
// page; this module asks it. The open requests are counted on this side, from Playwright's
// events, because a page sees a request only when it ends. A request open longer than
// STALE_MS no longer counts (a stream or a long poll), nor does a WebSocket or an event stream.
//
// Every question is put with page.waitForFunction, which polls inside the page, carries on in
// the new document when the page navigates, and is given up by Playwright itself when the time
// is over, also when the page cannot run script at all (an open dialog).

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const QUIET_MS = 500;
const TIMEOUT_MS = 5000;
const STALE_MS = 10_000;
const IGNORED_REQUESTS = new Set(['websocket', 'eventsource']);
const KEY = '__pageActivity__';
const POLL_MS = 25;
const READ_TIMEOUT_MS = 1000;
const RETRY_PAUSE_MS = 50;

// In the image the tracker sits next to the package (copied there by the Dockerfile); in the
// repository it is the study runner's own file.
const CANDIDATES = [
  path.join(__dirname, '..', 'quiet.js'),
  path.join(__dirname, '..', '..', 'hoh_runner', 'quiet.js'),
];

let cached = null;

/** The tracker's source and its SHA-256. */
function source() {
  if (cached === null) {
    const file = CANDIDATES.find((candidate) => fs.existsSync(candidate));
    if (file === undefined) throw new Error(`quiet.js is not at ${CANDIDATES.join(' or ')}`);
    const bytes = fs.readFileSync(file);
    cached = {
      text: bytes.toString('utf8'),
      sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
      file,
    };
  }
  return cached;
}

let callFunction = null;

/**
 * The function the page runs: it makes sure the document has a tracker (one that got none
 * when it was created starts being tracked here) and calls one of its methods. Built as a
 * real function so that Playwright passes it as one; its text is quiet.py's _CALL.
 */
function call() {
  if (callFunction === null) {
    const text = `([name, arg]) => {\n${source().text}\nreturn window.${KEY}[name](arg);\n}`;
    // eslint-disable-next-line no-new-func
    callFunction = new Function(`return (${text});`)();
  }
  return callFunction;
}

const sleepFor = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const monotonic = () => performance.now();

function isTimeout(error) {
  return Boolean(error) && error.name === 'TimeoutError';
}

/**
 * Wait until the tracker's method `name` returns a stamp ("<document id>:<count>"), and
 * return it. The answer travels as a string: Playwright hands a string over together with
 * the handle, so reading it needs no second trip to the page.
 */
async function ask(page, name, arg, timeoutMs) {
  const handle = await page.waitForFunction(call(), [name, arg], {
    timeout: Math.max(1, timeoutMs),
    polling: POLL_MS,
  });
  const text = String(handle);
  handle.dispose().catch(() => {});
  const match = /^(.+):(\d+)$/s.exec(text);
  if (match === null) throw new Error(`unexpected answer from the page: ${JSON.stringify(text.slice(0, 80))}`);
  return text;
}

/** The requests of one page that have started and not ended, from Playwright's events. */
class Requests {
  constructor(page, now = monotonic) {
    this.now = now;
    this.open = new Map();
    this.lastEnd = -Infinity;
    page.on('request', (request) => this.started(request));
    page.on('requestfinished', (request) => this.ended(request));
    page.on('requestfailed', (request) => this.ended(request));
  }

  started(request) {
    let kind = null;
    try {
      kind = request.resourceType();
    } catch {
      // A request that cannot say what it is still counts.
    }
    if (!IGNORED_REQUESTS.has(kind)) this.open.set(request, this.now());
  }

  ended(request) {
    if (this.open.delete(request)) this.lastEnd = this.now();
  }

  /** 0 while a request is open; else how long ago the last one ended (Infinity if none did). */
  idleMs() {
    const now = this.now();
    for (const started of this.open.values()) {
      if (now - started < STALE_MS) return 0;
    }
    return now - this.lastEnd;
  }
}

/**
 * Track activity in the page's current document and in every later one, and the requests the
 * page makes from now on. Returns the request tracker.
 */
async function install(page, now = monotonic) {
  const requests = new Requests(page, now);
  await page.context().addInitScript(source().text);
  await ask(page, 'stamp', null, TIMEOUT_MS);
  return requests;
}

/** The page's stamp right now; null if it cannot be read. */
async function stamp(page) {
  if (page.isClosed()) return null;
  try {
    return await ask(page, 'stamp', null, READ_TIMEOUT_MS);
  } catch {
    return null;
  }
}

/**
 * Wait until the page is quiet; returns {ok, stamp}: ok false when the cap was hit (the stamp
 * is then read afterwards and may be null). A navigation during the wait starts the wait again
 * in the new document. Never throws for anything the page does.
 */
async function settleStamped(page, options = {}) {
  const {
    quietMs = QUIET_MS,
    timeoutMs = TIMEOUT_MS,
    requests = null,
    now = monotonic,
    sleep = sleepFor,
  } = options;
  const deadline = now() + timeoutMs;
  while (!page.isClosed()) {
    let leftMs = deadline - now();
    if (leftMs <= 0) break;
    let found;
    try {
      found = await ask(page, 'quiet', quietMs, leftMs);
    } catch (error) {
      if (isTimeout(error)) break;
      // The page could not be asked just now (it is being replaced, say).
      await sleep(RETRY_PAUSE_MS);
      continue;
    }
    const idleMs = requests === null ? Infinity : requests.idleMs();
    if (idleMs >= quietMs) return { ok: true, stamp: found };
    // A request is open, or ended a moment ago: give it time, then ask the page again.
    leftMs = deadline - now();
    if (leftMs <= 0) break;
    await sleep(Math.min(Math.max(quietMs - idleMs, POLL_MS), leftMs));
  }
  return { ok: false, stamp: await stamp(page) };
}

module.exports = {
  QUIET_MS,
  TIMEOUT_MS,
  STALE_MS,
  KEY,
  Requests,
  ask,
  install,
  isTimeout,
  settleStamped,
  source,
  stamp,
};
