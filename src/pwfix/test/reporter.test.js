'use strict';
// The reporter turns Playwright Test's results into the report the study runner's pytest
// plugin writes, so that hoh_runner.status gives the run its status by the same rules.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const Reporter = require('../reporter');
const { outcome, callOf } = Reporter;

const TESTS_DIR = '/work/tests';

function fakeTest(title, { file = 'create_book.spec.ts', describe = [], expectedStatus = 'passed', line = 3 } = {}) {
  const fileSuite = { type: 'file', title: file, parent: { type: 'project', title: '', parent: { type: 'root', title: '' } } };
  let parent = fileSuite;
  for (const name of describe) parent = { type: 'describe', title: name, parent };
  return { title, parent, expectedStatus, location: { file: path.join(TESTS_DIR, file), line, column: 1 } };
}

function config() {
  return { projects: [{ testDir: TESTS_DIR }] };
}

function suiteOf(tests) {
  return { allTests: () => tests };
}

function record(overrides = {}) {
  const body = {
    id: 'r', env_error: null, final_url: 'http://app/books',
    settle: { calls: 4, timeouts: 0, rechecks: 1 },
    page_errors: { count: 0, first: [] },
    ...overrides,
  };
  return { name: 'pwfix-record', contentType: 'application/json', body: Buffer.from(JSON.stringify(body)) };
}

test('outcomes map onto pytest\'s', () => {
  const t = (expectedStatus) => ({ expectedStatus });
  assert.equal(outcome(t('passed'), { status: 'passed' }), 'passed');
  assert.equal(outcome(t('passed'), { status: 'failed' }), 'failed');
  assert.equal(outcome(t('passed'), { status: 'timedOut' }), 'error');
  assert.equal(outcome(t('passed'), { status: 'interrupted' }), 'error');
  assert.equal(outcome(t('passed'), { status: 'skipped' }), 'skipped');
  assert.equal(outcome(t('skipped'), { status: 'skipped' }), 'skipped');
  assert.equal(outcome(t('failed'), { status: 'failed' }), 'xfailed');
  assert.equal(outcome(t('failed'), { status: 'passed' }), 'xpassed');
  assert.equal(outcome(t('failed'), { status: 'timedOut' }), 'error');
});

test('the call that failed is read from the message', () => {
  assert.equal(callOf('Error: expect(locator).toHaveText(expected) failed\n\nLocator: x'), 'toHaveText');
  assert.equal(callOf('Error: expect(locator).not.toBeVisible() failed'), 'toBeVisible');
  assert.equal(callOf('TimeoutError: locator.click: Timeout 30000ms exceeded.'), 'click');
  assert.equal(callOf('Error: boom'), null);
});

test('a run is written as the pytest plugin writes it', async () => {
  const out = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pwfix-')), 'report.json');
  const reporter = new Reporter({ out });
  const target = fakeTest('test_create_book');
  const other = fakeTest('other', { describe: ['group'] });
  reporter.onBegin(config(), suiteOf([target, other]));
  reporter.onTestEnd(target, {
    status: 'failed',
    duration: 1234,
    error: {
      message: '\u001b[31mError: expect(locator).toHaveText(expected) failed\u001b[39m\n\nLocator: x',
      location: { file: path.join(TESTS_DIR, 'create_book.spec.ts'), line: 17, column: 5 },
    },
    attachments: [record()],
  });
  reporter.onTestEnd(other, { status: 'skipped', duration: 0, attachments: [] });
  await reporter.onEnd({ status: 'failed' });
  const report = JSON.parse(fs.readFileSync(out, 'utf8'));
  assert.deepEqual(report.items, [
    'tests/create_book.spec.ts::test_create_book',
    'tests/create_book.spec.ts::group > other',
  ]);
  assert.equal(report.collected, 2);
  assert.equal(report.collect_errors, 0);
  assert.equal(report.env_error, null);
  assert.deepEqual(report.tests[0], {
    nodeid: 'tests/create_book.spec.ts::test_create_book',
    outcome: 'failed',
    phase: 'call',
    lineno: 17,
    exc_type: 'Error',
    call: 'toHaveText',
    message: 'Error: expect(locator).toHaveText(expected) failed\n\nLocator: x',
    duration_s: 1.234,
    status: 'failed',
    expected_status: 'passed',
    attempts: 1,
  });
  assert.equal(report.tests[1].outcome, 'skipped');
  assert.deepEqual(report.settle, { calls: 4, timeouts: 0, rechecks: 1 });
  assert.equal(report.final_url, 'http://app/books');
  assert.equal(report.records.length, 1);
});

test('a test run more than once keeps its first attempt, and its attempts are counted', async () => {
  // A T' can raise its retries (test.describe.configure); the config sets none. The study
  // judges a run on a fresh stack, so the first attempt gives the outcome and the record.
  const out = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pwfix-')), 'report.json');
  const reporter = new Reporter({ out });
  const target = fakeTest('test_create_book');
  reporter.onBegin(config(), suiteOf([target]));
  reporter.onTestEnd(target, {
    status: 'failed', duration: 1000, retry: 0,
    error: { message: 'Error: expect(locator).toHaveText(expected) failed', location: { file: path.join(TESTS_DIR, 'create_book.spec.ts'), line: 9, column: 5 } },
    attachments: [record({ id: 'first', settle: { calls: 4, timeouts: 0, rechecks: 1 } })],
  });
  for (const retry of [1, 2]) {
    reporter.onTestEnd(target, {
      status: 'passed', duration: 900, retry,
      attachments: [record({ id: `retry ${retry}`, settle: { calls: 7, timeouts: 1, rechecks: 0 }, final_url: 'http://app/other' })],
    });
  }
  await reporter.onEnd({ status: 'passed' });
  const report = JSON.parse(fs.readFileSync(out, 'utf8'));
  assert.equal(report.tests.length, 1);
  const [first] = report.tests;
  assert.equal(first.outcome, 'failed');
  assert.equal(first.lineno, 9);
  assert.equal(first.attempts, 3);
  assert.deepEqual(report.records.map((r) => r.id), ['first']);
  assert.deepEqual(report.settle, { calls: 4, timeouts: 0, rechecks: 1 });
  assert.equal(report.final_url, 'http://app/books');
});

test('an error in a file that is not the test leaves lineno empty', async () => {
  const out = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pwfix-')), 'report.json');
  const reporter = new Reporter({ out });
  const target = fakeTest('test_x');
  reporter.onBegin(config(), suiteOf([target]));
  reporter.onTestEnd(target, {
    status: 'failed', duration: 1,
    error: { message: 'Error: x', location: { file: '/opt/node/pwfix/index.js', line: 3, column: 1 } },
    attachments: [],
  });
  await reporter.onEnd({ status: 'failed' });
  assert.equal(JSON.parse(fs.readFileSync(out, 'utf8')).tests[0].lineno, null);
});

test('an environment failure the fixture recorded is the report\'s env_error', async () => {
  const out = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pwfix-')), 'report.json');
  const reporter = new Reporter({ out });
  const target = fakeTest('test_x');
  reporter.onBegin(config(), suiteOf([target]));
  reporter.onTestEnd(target, {
    status: 'failed', duration: 1, error: { message: 'Error: environment failure: x' },
    attachments: [record({ env_error: 'connect: no browser at http://browser:9222 after 60 s: x' })],
  });
  await reporter.onEnd({ status: 'failed' });
  const report = JSON.parse(fs.readFileSync(out, 'utf8'));
  assert.equal(report.env_error, 'connect: no browser at http://browser:9222 after 60 s: x');
});

test('a file that cannot be loaded is a collection error', async () => {
  const out = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pwfix-')), 'report.json');
  const reporter = new Reporter({ out });
  reporter.onBegin(config(), suiteOf([]));
  reporter.onError({ message: 'SyntaxError: /work/tests/x.spec.ts: Unexpected token (3:4)' });
  await reporter.onEnd({ status: 'failed' });
  const report = JSON.parse(fs.readFileSync(out, 'utf8'));
  assert.equal(report.collect_errors, 1);
  assert.deepEqual(report.items, []);
  assert.match(report.errors[0], /SyntaxError/);
});

test('without an output file the reporter writes nothing', async () => {
  const reporter = new Reporter({});
  reporter.onBegin(config(), suiteOf([]));
  await reporter.onEnd({ status: 'passed' });
  assert.equal(reporter.printsToStdio(), false);
});
