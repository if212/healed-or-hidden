'use strict';
// Is the environment usable? The study runner's preflight.py, for the fixture.
//
// A run needs a browser that answers on its DevTools address and holds exactly one tab: the
// one prepared for the test (signed in, start page open). When that is not so the run is not a
// result of the test: it ends as an environment failure and is made again on a fresh stack.

const POLL_MS = 25;
const RETRY_PAUSE_MS = 250;
const CONNECT_TIMEOUT_S = 10;
const READ_TIMEOUT_MS = 5000;

class Unusable extends Error {
  constructor(message) {
    super(message);
    this.name = 'Unusable';
  }
}

const sleepFor = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function firstLine(error) {
  const text = String((error && error.message) || error || '').trim();
  return text ? text.split('\n')[0] : (error && error.name) || 'Error';
}

/**
 * Connect to the browser as one more DevTools client, trying until `timeoutS` has passed.
 *
 * `noDefaults` leaves the prepared context as it is: no focus or media emulation, the
 * browser's own download behavior. The process that prepared the tab stays connected and
 * dismisses every JavaScript dialog the moment it opens; a second client that did the same
 * would find the dialog gone and stop working (preflight.py, connect). So this side listens
 * for dialogs, which stops it from answering them, and leaves them alone.
 */
async function connect(chromium, cdpUrl, timeoutS, options = {}) {
  const { onDialog = () => {}, now = Date.now, sleep = sleepFor } = options;
  const deadline = now() + timeoutS * 1000;
  let last = null;
  for (;;) {
    const attemptS = Math.min(CONNECT_TIMEOUT_S, Math.max(1, (deadline - now()) / 1000));
    try {
      const browser = await chromium.connectOverCDP(cdpUrl, { timeout: attemptS * 1000, noDefaults: true });
      for (const context of browser.contexts()) context.on('dialog', onDialog);
      return browser;
    } catch (error) {
      last = error;
    }
    const leftMs = deadline - now();
    if (leftMs <= 0) {
      throw new Unusable(`no browser at ${cdpUrl} after ${timeoutS} s: ${firstLine(last)}`);
    }
    await sleep(Math.min(RETRY_PAUSE_MS, leftMs));
  }
}

/** The browser's one tab; Unusable when there is none or more than one. */
function onlyTab(browser) {
  const pages = browser.contexts().flatMap((context) => context.pages());
  if (pages.length !== 1) throw new Unusable(`expected exactly one tab, found ${pages.length} tabs`);
  return pages[0];
}

/** The value of one sessionStorage key in the tab, or null when it is absent. */
async function sessionValue(page, key) {
  const handle = await page.waitForFunction(
    (k) => { const v = sessionStorage.getItem(k); return v === null ? '-' : `+${v}`; },
    key,
    { timeout: READ_TIMEOUT_MS, polling: POLL_MS },
  );
  const answer = String(handle);
  handle.dispose().catch(() => {});
  return answer.startsWith('+') ? answer.slice(1) : null;
}

/** Unusable unless the tab is in its starting state: under the start address, ready key set. */
async function preflight(page, { startUrl, readyKey }) {
  if (startUrl && !page.url().startsWith(startUrl)) {
    throw new Unusable(`the tab is at ${page.url()}, not under ${startUrl}`);
  }
  if (readyKey) {
    let value;
    try {
      value = await sessionValue(page, readyKey);
    } catch (error) {
      throw new Unusable(`cannot read session key '${readyKey}': ${firstLine(error)}`);
    }
    if (value !== 'true') {
      throw new Unusable(`session key '${readyKey}' is ${value === null ? 'null' : `'${value}'`}, not 'true'`);
    }
  }
}

module.exports = { Unusable, connect, firstLine, onlyTab, preflight, sessionValue };
