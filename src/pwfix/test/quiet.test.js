'use strict';
// The wait for a quiet page, on fake pages: what quiet.py does, in the fixture's terms.
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const quiet = require('../lib/quiet');

class FakeTimeout extends Error {
  constructor(message) {
    super(message);
    this.name = 'TimeoutError';
  }
}

/** A page whose tracker answers from a script of answers: a string, a TimeoutError or an Error. */
function fakePage(answers, { closed = false } = {}) {
  const calls = [];
  const handlers = {};
  return {
    calls,
    handlers,
    isClosed: () => closed,
    on(event, handler) {
      (handlers[event] ??= []).push(handler);
      return this;
    },
    emit(event, value) {
      for (const handler of handlers[event] ?? []) handler(value);
    },
    async waitForFunction(fn, arg, options) {
      calls.push({ fn, arg, options });
      const answer = answers.length ? answers.shift() : new FakeTimeout('out of answers');
      if (answer instanceof Error) throw answer;
      return { toString: () => answer, dispose: async () => {} };
    },
  };
}

/** A clock that moves only when the code under test sleeps. */
function fakeClock() {
  let t = 1000;
  return {
    now: () => t,
    sleep: async (ms) => {
      t += ms;
    },
    advance: (ms) => {
      t += ms;
    },
  };
}

test('the tracker is the study runner\'s quiet.js, byte for byte', () => {
  const study = fs.readFileSync(path.join(__dirname, '..', '..', 'hoh_runner', 'quiet.js'));
  const { text, sha256 } = quiet.source();
  assert.equal(text, study.toString('utf8'));
  assert.equal(sha256, crypto.createHash('sha256').update(study).digest('hex'));
});

test('ask passes a function that installs the tracker and calls one of its methods', async () => {
  const page = fakePage(['abc:3']);
  const stamp = await quiet.ask(page, 'quiet', 800, 5000);
  assert.equal(stamp, 'abc:3');
  const [{ fn, arg, options }] = page.calls;
  assert.equal(typeof fn, 'function');
  assert.match(String(fn), /^\(\[name, arg\]\) => \{\n/);
  assert.ok(String(fn).includes(quiet.source().text));
  assert.ok(String(fn).endsWith('\nreturn window.__pageActivity__[name](arg);\n}'));
  assert.deepEqual(arg, ['quiet', 800]);
  assert.deepEqual(options, { timeout: 5000, polling: 25 });
});

test('ask refuses an answer that is not a stamp', async () => {
  await assert.rejects(quiet.ask(fakePage(['true']), 'stamp', null, 1000), /unexpected answer/);
  await assert.rejects(quiet.ask(fakePage([':4']), 'stamp', null, 1000), /unexpected answer/);
});

test('ask gives the page at least one millisecond', async () => {
  const page = fakePage(['a:1']);
  await quiet.ask(page, 'stamp', null, 0);
  assert.equal(page.calls[0].options.timeout, 1);
});

test('settle returns the stamp of the quiet moment when no request is open', async () => {
  const clock = fakeClock();
  const page = fakePage(['doc:7']);
  const requests = new quiet.Requests(page, clock.now);
  const result = await quiet.settleStamped(page, { quietMs: 800, timeoutMs: 5000, requests, ...clock });
  assert.deepEqual(result, { ok: true, stamp: 'doc:7' });
  assert.deepEqual(page.calls[0].arg, ['quiet', 800]);
});

test('settle waits while a request is open and asks the page again', async () => {
  const clock = fakeClock();
  const page = fakePage(['doc:1', 'doc:2', 'doc:3']);
  const requests = new quiet.Requests(page, clock.now);
  const request = { resourceType: () => 'fetch' };
  page.emit('request', request);
  const sleeps = [];
  const sleep = async (ms) => {
    sleeps.push(ms);
    await clock.sleep(ms);
    if (requests.open.size) page.emit('requestfinished', request);
  };
  const result = await quiet.settleStamped(page, {
    quietMs: 800, timeoutMs: 5000, requests, now: clock.now, sleep,
  });
  // The first answer came while the request was open; the request ends during the pause, so
  // the second answer comes with no time since it ended, and the third after the quiet time.
  assert.deepEqual(result, { ok: true, stamp: 'doc:3' });
  assert.equal(page.calls.length, 3);
  assert.deepEqual(sleeps, [800, 800]);
});

test('websockets, event streams and requests open longer than 10 s do not count', () => {
  const clock = fakeClock();
  const page = fakePage([]);
  const requests = new quiet.Requests(page, clock.now);
  page.emit('request', { resourceType: () => 'websocket' });
  page.emit('request', { resourceType: () => 'eventsource' });
  assert.equal(requests.idleMs(), Infinity);
  page.emit('request', { resourceType: () => 'xhr' });
  assert.equal(requests.idleMs(), 0);
  clock.advance(10_000);
  assert.equal(requests.idleMs(), Infinity);
});

test('a request that cannot say its type still counts', () => {
  const page = fakePage([]);
  const requests = new quiet.Requests(page, () => 5);
  page.emit('request', { resourceType: () => { throw new Error('gone'); } });
  assert.equal(requests.idleMs(), 0);
});

test('idle time counts from the end of the last request', () => {
  const clock = fakeClock();
  const page = fakePage([]);
  const requests = new quiet.Requests(page, clock.now);
  const request = { resourceType: () => 'document' };
  page.emit('request', request);
  clock.advance(100);
  page.emit('requestfailed', request);
  clock.advance(300);
  assert.equal(requests.idleMs(), 300);
});

test('settle gives up at the cap and reads the stamp afterwards', async () => {
  const clock = fakeClock();
  const page = fakePage([new FakeTimeout('5000ms exceeded'), 'late:9']);
  const result = await quiet.settleStamped(page, { quietMs: 800, timeoutMs: 5000, ...clock });
  assert.deepEqual(result, { ok: false, stamp: 'late:9' });
  assert.deepEqual(page.calls[1].arg, ['stamp', null]);
  assert.equal(page.calls[1].options.timeout, 1000);
});

test('settle tries again after an error that is not a timeout', async () => {
  const clock = fakeClock();
  const page = fakePage([new Error('Execution context was destroyed'), 'next:1']);
  const result = await quiet.settleStamped(page, { quietMs: 800, timeoutMs: 5000, ...clock });
  assert.deepEqual(result, { ok: true, stamp: 'next:1' });
});

test('a closed page is never asked', async () => {
  const page = fakePage(['x:1'], { closed: true });
  const result = await quiet.settleStamped(page, { quietMs: 800, timeoutMs: 5000, ...fakeClock() });
  assert.deepEqual(result, { ok: false, stamp: null });
  assert.equal(page.calls.length, 0);
});

test('stamp is null when the page cannot be read', async () => {
  assert.equal(await quiet.stamp(fakePage([new Error('closed')])), null);
  assert.equal(await quiet.stamp(fakePage(['d:2'])), 'd:2');
});

test('install registers the tracker for later documents and asks the current one', async () => {
  const scripts = [];
  const page = fakePage(['d:0']);
  page.context = () => ({ addInitScript: async (script) => scripts.push(script) });
  const requests = await quiet.install(page);
  assert.ok(requests instanceof quiet.Requests);
  assert.deepEqual(scripts, [quiet.source().text]);
  assert.deepEqual(page.calls[0].arg, ['stamp', null]);
});
