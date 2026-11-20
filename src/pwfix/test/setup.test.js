'use strict';
// Settings, the control directory and the connection, on fakes.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const settings = require('../lib/settings');
const control = require('../lib/control');
const browser = require('../lib/browser');

const ENV = {
  HOH_CDP_URL: 'http://browser:9222',
  HOH_START_URL: 'http://app:80/',
  HOH_READY_KEY: '__ready__',
  HOH_QUIET_MS: '800',
  HOH_RUN_TIMEOUT_S: '300',
  PWFIX_CONTROL_DIR: '/ctl',
  PWFIX_RESET: '1',
  PWFIX_REPORT: '/work/out/report.json',
  PATH: '/usr/bin',
};

test('settings are read from the HOH_ and PWFIX_ variables, with the runner\'s defaults', () => {
  const { values, problem } = settings.read(ENV);
  assert.equal(problem, null);
  assert.deepEqual(values, {
    cdpUrl: 'http://browser:9222',
    startUrl: 'http://app:80/',
    readyKey: '__ready__',
    quietMs: 800,
    settleTimeoutMs: 5000,
    preflightTimeoutS: 60,
    controlDir: '/ctl',
    reset: true,
    resetTimeoutS: 900,
  });
});

test('a missing address or a bad number is a problem, not a crash', () => {
  assert.match(settings.read({}).problem, /HOH_CDP_URL is not set/);
  assert.match(settings.read({ HOH_CDP_URL: 'x', HOH_QUIET_MS: 'soon' }).problem, /HOH_QUIET_MS is not a number/);
  assert.match(settings.read({ HOH_CDP_URL: 'x', HOH_QUIET_MS: '-1' }).problem, /HOH_QUIET_MS is negative/);
  assert.equal(settings.read({ HOH_CDP_URL: 'x', PWFIX_RESET: '0' }).values.reset, false);
});

test('the quiet window has no default: without one the run is an environment failure', () => {
  // The Python runner falls back to 500 ms and the study always sets 800; without a default
  // here, a healer's run and a judge's run cannot wait differently without a trace.
  for (const env of [{ HOH_CDP_URL: 'x' }, { HOH_CDP_URL: 'x', HOH_QUIET_MS: ' ' }]) {
    const { values, problem } = settings.read(env);
    assert.match(problem, /HOH_QUIET_MS is not set/);
    assert.equal(values.quietMs, null);
  }
  assert.equal(settings.read({ HOH_CDP_URL: 'x', HOH_QUIET_MS: '800' }).problem, null);
});

test('a record names the quiet window and the settle cap it waited with', () => {
  const { values } = settings.read(ENV);
  assert.deepEqual(settings.timing(values), { quiet_ms: 800, settle_timeout_ms: 5000 });
});

test('in a worker the settings leave the environment before any test code runs', () => {
  const env = { ...ENV, TEST_WORKER_INDEX: '0' };
  const { values } = settings.take(env);
  assert.equal(values.cdpUrl, 'http://browser:9222');
  assert.deepEqual(Object.keys(env).sort(), ['PATH', 'TEST_WORKER_INDEX']);
});

test('outside a worker the environment is left alone (workers inherit it)', () => {
  const env = { ...ENV };
  settings.take(env);
  assert.equal(env.HOH_CDP_URL, 'http://browser:9222');
});

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'pwfix-'));
}

test('a reset request is answered through the control directory', async () => {
  const dir = tempDir();
  const answering = (async () => {
    for (;;) {
      const requests = fs.existsSync(path.join(dir, 'requests')) ? fs.readdirSync(path.join(dir, 'requests')) : [];
      const request = requests.find((name) => name.endsWith('.json'));
      if (request) {
        const { id } = JSON.parse(fs.readFileSync(path.join(dir, 'requests', request), 'utf8'));
        fs.mkdirSync(path.join(dir, 'answers'), { recursive: true });
        fs.writeFileSync(path.join(dir, 'answers', `${id}.json`), JSON.stringify({ ok: true }));
        return id;
      }
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
  })();
  const result = await control.requestReset(dir, { timeoutMs: 5000, pollMs: 10 });
  assert.equal(result.id, await answering);
  assert.ok(result.seconds >= 0);
});

test('a reset that fails or gets no answer is an error', async () => {
  const dir = tempDir();
  const failing = control.requestReset(dir, { timeoutMs: 5000, pollMs: 10 });
  await new Promise((resolve) => setTimeout(resolve, 30));
  const [request] = fs.readdirSync(path.join(dir, 'requests'));
  fs.mkdirSync(path.join(dir, 'answers'));
  fs.writeFileSync(path.join(dir, 'answers', request), JSON.stringify({ ok: false, error: 'compose up failed' }));
  await assert.rejects(failing, /compose up failed/);
  await assert.rejects(control.requestReset(tempDir(), { timeoutMs: 50, pollMs: 10 }), /no answer .* within 0.05 s/);
});

test('a record is written whole into the control directory', () => {
  const dir = tempDir();
  const file = control.writeRecord(dir, { id: 'r1', env_error: null });
  assert.equal(path.dirname(file), path.join(dir, 'records'));
  assert.deepEqual(JSON.parse(fs.readFileSync(file, 'utf8')), { id: 'r1', env_error: null });
  assert.deepEqual(fs.readdirSync(path.join(dir, 'records')), ['r1.json']);
});

function fakeBrowser(pages, contexts = 1) {
  const listeners = [];
  const ctxs = Array.from({ length: contexts }, (_, i) => ({
    pages: () => (i === 0 ? pages : []),
    on: (event, handler) => listeners.push([event, handler]),
  }));
  return { contexts: () => ctxs, listeners };
}

test('connect tries until the deadline, without defaults, and listens for dialogs', async () => {
  let attempts = 0;
  const target = fakeBrowser([{}]);
  const chromium = {
    async connectOverCDP(url, options) {
      attempts += 1;
      assert.equal(url, 'http://browser:9222');
      assert.equal(options.noDefaults, true);
      if (attempts < 3) throw new Error('ECONNREFUSED\nmore');
      return target;
    },
  };
  let t = 0;
  const clock = { now: () => t, sleep: async (ms) => { t += ms; } };
  const dialogs = [];
  const result = await browser.connect(chromium, 'http://browser:9222', 60, { onDialog: (d) => dialogs.push(d), ...clock });
  assert.equal(result, target);
  assert.equal(attempts, 3);
  assert.deepEqual(target.listeners.map(([event]) => event), ['dialog']);
});

test('connect gives up with the first line of the last error', async () => {
  const chromium = { connectOverCDP: async () => { throw new Error('ECONNREFUSED 1.2.3.4\nstack'); } };
  let t = 0;
  const clock = { now: () => t, sleep: async (ms) => { t += ms; } };
  await assert.rejects(
    browser.connect(chromium, 'http://browser:9222', 1, clock),
    (error) => error instanceof browser.Unusable && /no browser at http:\/\/browser:9222 after 1 s: ECONNREFUSED 1.2.3.4$/.test(error.message),
  );
});

test('the browser must hold exactly one tab', () => {
  const page = {};
  assert.equal(browser.onlyTab(fakeBrowser([page])), page);
  assert.throws(() => browser.onlyTab(fakeBrowser([])), /expected exactly one tab, found 0 tabs/);
  assert.throws(() => browser.onlyTab(fakeBrowser([{}, {}])), /found 2 tabs/);
});

function fakeTab(url, answer) {
  return {
    url: () => url,
    async waitForFunction(fn, arg) {
      assert.equal(arg, '__ready__');
      if (answer instanceof Error) throw answer;
      return { toString: () => answer, dispose: async () => {} };
    },
  };
}

test('preflight checks the start address and the ready key', async () => {
  const options = { startUrl: 'http://app:80/', readyKey: '__ready__' };
  await browser.preflight(fakeTab('http://app:80/books', '+true'), options);
  await assert.rejects(browser.preflight(fakeTab('about:blank', '+true'), options), /the tab is at about:blank, not under http:\/\/app:80\//);
  await assert.rejects(browser.preflight(fakeTab('http://app:80/', '-'), options), /session key '__ready__' is null, not 'true'/);
  await assert.rejects(browser.preflight(fakeTab('http://app:80/', '+false'), options), /is 'false', not 'true'/);
  await assert.rejects(browser.preflight(fakeTab('http://app:80/', new Error('closed\nx')), options), /cannot read session key '__ready__': closed$/);
  await browser.preflight(fakeTab('anything', '-'), { startUrl: null, readyKey: null });
});
