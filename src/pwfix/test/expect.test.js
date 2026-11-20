'use strict';
// The exported expect: web-first assertions on the page wait for a quiet page and are made
// again while the page changes under them, as hooks.py does for Python's assertions.
const test = require('node:test');
const assert = require('node:assert/strict');

const { wrapExpect, ASYNC_MATCHERS } = require('../lib/expect');

/** A stand-in for Playwright's expect: records what each matcher was called on. */
function fakeExpect(log) {
  const matchersFor = (actual, negated) => ({
    async toBeVisible(...args) {
      log.push(`${negated ? 'not.' : ''}toBeVisible ${actual.id} ${args.length}`);
      if (actual.fail) throw new Error('expect(locator).toBeVisible() failed');
    },
    async toHaveText(expected) {
      log.push(`toHaveText ${actual.id} ${expected}`);
    },
    toBe(expected) {
      log.push(`toBe ${actual} ${expected}`);
      if (actual !== expected) throw new Error('toBe failed');
    },
    get not() {
      return matchersFor(actual, true);
    },
  });
  const expect = (actual) => matchersFor(actual, false);
  expect.soft = (actual) => matchersFor(actual, false);
  expect.configure = (options) => {
    log.push(`configure ${options.timeout}`);
    return fakeExpect(log);
  };
  expect.extend = () => fakeExpect(log);
  expect.poll = () => 'poll';
  expect.any = (cls) => `any ${cls.name}`;
  return expect;
}

function makeState({ stamps = [], wrapped = new Set(), unwrap = (x) => x } = {}) {
  const log = [];
  const state = {
    log,
    wrapper: {
      isWrapped: (value) => wrapped.has(value),
      unwrap,
      unwrapArgs: (args) => args.map(unwrap),
      async rechecked(name, run) {
        // The real Wrapper.rechecked, in short: settle, run, stamp, again while they differ.
        for (let attempt = 1; ; attempt++) {
          log.push('settle');
          const before = stamps.length ? stamps.shift() : 's';
          log.push(`call ${name}`);
          const result = await run();
          const after = stamps.length ? stamps.shift() : 's';
          if (attempt === 3 || before === after) return result;
          log.push('recheck');
        }
      },
    },
  };
  return state;
}

test('the list of web-first matchers is Playwright\'s', () => {
  for (const name of ['toBeVisible', 'toHaveText', 'toHaveURL', 'toHaveTitle', 'toHaveCount',
    'toMatchAriaSnapshot', 'toContainText', 'toBeOK']) {
    assert.ok(ASYNC_MATCHERS.has(name), name);
  }
  assert.ok(!ASYNC_MATCHERS.has('toBe'));
});

test('a web-first matcher on a wrapped locator waits and is made again while the page changes', async () => {
  const locator = { id: 'L' };
  const raw = { id: 'raw' };
  const state = makeState({ stamps: ['a', 'b', 'b', 'b'], wrapped: new Set([locator]), unwrap: (x) => (x === locator ? raw : x) });
  const log = [];
  const expect = wrapExpect(fakeExpect(log), () => state);
  await expect(locator).toBeVisible();
  assert.deepEqual(log, ['toBeVisible raw 0', 'toBeVisible raw 0']);
  assert.deepEqual(state.log, ['settle', 'call expect.toBeVisible', 'recheck', 'settle', 'call expect.toBeVisible']);
});

test('not. is wrapped too', async () => {
  const locator = { id: 'L' };
  const state = makeState({ wrapped: new Set([locator]) });
  const log = [];
  const expect = wrapExpect(fakeExpect(log), () => state);
  await expect(locator).not.toBeVisible();
  assert.deepEqual(log, ['not.toBeVisible L 0']);
  assert.deepEqual(state.log, ['settle', 'call expect.not.toBeVisible']);
});

test('a failed assertion fails at once, as it is', async () => {
  const locator = { id: 'L', fail: true };
  const state = makeState({ wrapped: new Set([locator]) });
  const expect = wrapExpect(fakeExpect([]), () => state);
  await assert.rejects(expect(locator).toBeVisible(), /toBeVisible\(\) failed/);
  assert.deepEqual(state.log, ['settle', 'call expect.toBeVisible']);
});

test('a value that is not a page or a locator goes straight to the matcher', () => {
  const state = makeState();
  const log = [];
  const expect = wrapExpect(fakeExpect(log), () => state);
  expect(3).toBe(3);
  assert.throws(() => expect(3).toBe(4), /toBe failed/);
  assert.deepEqual(state.log, []);
});

test('without a page fixture in use nothing waits', async () => {
  const locator = { id: 'L' };
  const log = [];
  const expect = wrapExpect(fakeExpect(log), () => null);
  await expect(locator).toBeVisible();
  assert.deepEqual(log, ['toBeVisible L 0']);
});

test('a generic matcher on a locator is not made asynchronous', () => {
  const locator = { id: 'L' };
  const state = makeState({ wrapped: new Set([locator]) });
  const expect = wrapExpect(fakeExpect([]), () => state);
  assert.throws(() => expect(locator).toBe(1), /toBe failed/);
  assert.deepEqual(state.log, []);
});

test('expect.soft, expect.configure and expect.extend keep the wait', async () => {
  const locator = { id: 'L' };
  const state = makeState({ wrapped: new Set([locator]) });
  const log = [];
  const expect = wrapExpect(fakeExpect(log), () => state);
  await expect.soft(locator).toHaveText('x');
  await expect.configure({ timeout: 10 })(locator).toBeVisible();
  await expect.extend({})(locator).toBeVisible();
  assert.deepEqual(state.log.filter((entry) => entry === 'settle').length, 3);
  assert.ok(log.includes('configure 10'));
});

test('the other members of expect are Playwright\'s own', () => {
  const expect = wrapExpect(fakeExpect([]), () => null);
  assert.equal(expect.poll(), 'poll');
  assert.equal(expect.any(Number), 'any Number');
});
