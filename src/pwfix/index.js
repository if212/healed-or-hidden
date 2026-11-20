'use strict';
// pwfix: the fixture of the fidelity check's ported tests (design doc 5.6). A ported test
// imports `test` and `expect` from here, as a Python test gets `page` from the study runner's
// pytest plugin.
//
// `page` is the one tab already open in the browser the run was given: signed in, at the start
// page. The fixture connects to it over CDP (noDefaults: the prepared context stays as it is),
// checks it as the runner's preflight does, puts the wait for a quiet page in front of every
// browser call (lib/wrap.js) and every web-first assertion (lib/expect.js), and at the end lets
// go of the connection without closing anything. Before it connects, in a session (PWFIX_RESET)
// it asks for a fresh stack through the control directory, because the official healer runs the
// test inside its own server and the study gives every run a fresh stack.
//
// Anything that keeps the run from being a run of the test (a reset, connection or preflight
// that fails, a browser that goes away, a page that crashes) is recorded as an environment
// failure and fails the test with a message that says so. Each test's record goes to the
// reporter (an attachment) and, in a session, to the control directory.

const path = require('node:path');

const base = require('@playwright/test');
const installed = require('playwright-core/package.json').version;
const { setBoxedStackPrefixes } = require('playwright-core/lib/coreBundle').utils;

// Playwright Test leaves its own files out of an error's stack, and so out of the line an error
// points at and the code it shows. The fixture's files are left out the same way, so an error
// reads as it would without the fixture: it points at the test's own line. (Playwright Test
// sets the list to its own package when it loads; this keeps that entry.)
setBoxedStackPrefixes([
  path.dirname(require.resolve('playwright/package.json')),
  __dirname + path.sep,
]);

const api = require('./lib/api.json');
const browserLib = require('./lib/browser');
const control = require('./lib/control');
const { reEscape } = require('./lib/escape');
const quiet = require('./lib/quiet');
const settingsLib = require('./lib/settings');
const { wrapExpect } = require('./lib/expect');
const { Wrapper } = require('./lib/wrap');

const { values: SETTINGS, problem: SETTINGS_PROBLEM } = settingsLib.take(process.env);
const PAGE_ERRORS_KEPT = 3;
const PAGE_ERROR_LIMIT = 500;
const FIXTURE_TIMEOUT_MS = (SETTINGS.resetTimeoutS + SETTINGS.preflightTimeoutS + 120) * 1000;

/** The state of the page fixture in use, for the exported expect; null between tests. */
let current = null;

function newRecord(testInfo) {
  return {
    schema: 1,
    id: control.newId(),
    test: testInfo.title,
    file: testInfo.file,
    playwright_core: installed,
    quiet_sha256: quiet.source().sha256,
    ...settingsLib.timing(SETTINGS),
    reset: null,
    env_error: null,
    settle: { calls: 0, timeouts: 0, rechecks: 0 },
    tab_calls: 0,
    unsettled: [],
    dialogs: 0,
    page_errors: { count: 0, first: [] },
    final_url: null,
  };
}

function makeState(page, requests, record) {
  const settle = async () => {
    record.settle.calls += 1;
    let result;
    try {
      result = await quiet.settleStamped(page, {
        quietMs: SETTINGS.quietMs, timeoutMs: SETTINGS.settleTimeoutMs, requests,
      });
    } catch {
      result = { ok: false, stamp: null };
    }
    if (!result.ok) record.settle.timeouts += 1;
    return result;
  };
  const wrapper = new Wrapper(api, {
    settle,
    stamp: () => quiet.stamp(page),
    onCall: () => {
      record.tab_calls += 1;
    },
    onRecheck: () => {
      record.settle.rechecks += 1;
    },
    onUnsettled: (name) => {
      if (!record.unsettled.includes(name)) record.unsettled.push(name);
    },
  });
  return { wrapper, settle, wrapped: wrapper.wrap(page) };
}

const expect = wrapExpect(base.expect, () => current);

/** One harmless call and one harmless assertion must pass through the wait (hooks._check). */
async function check(state, record) {
  if (!state.wrapper.isWrapped(state.wrapped)) {
    throw new Error('the wait is not in place: the page is not one of the classes in api.json');
  }
  const seen = record.settle.calls;
  await state.wrapped.title();
  if (record.settle.calls <= seen) throw new Error('the wait is not in place: a browser call went past it');
  const before = record.settle.calls;
  await expect(state.wrapped).toHaveTitle(/.*/);
  if (record.settle.calls <= before) throw new Error('the wait is not in place: an assertion went past it');
  record.settle = { calls: 0, timeouts: 0, rechecks: 0 };
  record.tab_calls = 0;
}

const test = base.test.extend({
  page: [async ({ playwright }, use, testInfo) => {
    const record = newRecord(testInfo);
    const environment = (what, error) => {
      record.env_error ??= `${what}: ${browserLib.firstLine(error)}`;
      return new Error(`environment failure: ${record.env_error}`);
    };
    let browser = null;
    let page = null;
    let state = null;
    let releasing = false;
    try {
      if (SETTINGS_PROBLEM) throw environment('settings', new Error(SETTINGS_PROBLEM));
      if (api['playwright-core'] !== installed) {
        throw environment('settings', new Error(
          `api.json is for playwright-core ${api['playwright-core']}, ${installed} is installed`));
      }
      if (SETTINGS.reset) {
        try {
          if (!SETTINGS.controlDir) throw new Error('PWFIX_RESET is set without PWFIX_CONTROL_DIR');
          record.reset = await control.requestReset(SETTINGS.controlDir, {
            timeoutMs: SETTINGS.resetTimeoutS * 1000,
          });
        } catch (error) {
          throw environment('reset', error);
        }
      }
      try {
        browser = await browserLib.connect(playwright.chromium, SETTINGS.cdpUrl, SETTINGS.preflightTimeoutS, {
          onDialog: () => {
            record.dialogs += 1;
          },
        });
        page = browserLib.onlyTab(browser);
        await browserLib.preflight(page, SETTINGS);
      } catch (error) {
        throw environment('connect', error);
      }
      browser.on('disconnected', () => {
        if (!releasing) record.env_error ??= 'the browser went away during the run';
      });
      page.on('crash', () => {
        record.env_error ??= 'the page crashed during the run';
      });
      page.on('pageerror', (error) => {
        record.page_errors.count += 1;
        if (record.page_errors.first.length < PAGE_ERRORS_KEPT) {
          record.page_errors.first.push(String((error && error.message) || error).slice(0, PAGE_ERROR_LIMIT));
        }
      });
      try {
        const requests = await quiet.install(page);
        state = makeState(page, requests, record);
        current = state;
        await check(state, record);
      } catch (error) {
        throw environment('wait', error);
      }
      await use(state.wrapped);
    } finally {
      current = null;
      if (state !== null && page !== null && !page.isClosed()) {
        try {
          await state.settle();
          record.final_url = page.url();
        } catch {
          record.final_url = null;
        }
      }
      releasing = true;
      if (browser !== null) {
        try {
          await browser.close();
        } catch {
          // Gone already: nothing to let go of.
        }
      }
      if (SETTINGS.controlDir) {
        try {
          control.writeRecord(SETTINGS.controlDir, record);
        } catch (error) {
          record.env_error ??= `record: ${browserLib.firstLine(error)}`;
        }
      }
      await testInfo.attach('pwfix-record', { body: JSON.stringify(record), contentType: 'application/json' });
    }
    if (record.env_error) throw new Error(`environment failure: ${record.env_error}`);
  }, { timeout: FIXTURE_TIMEOUT_MS }],
});

// The expect a test reaches without importing it (test.expect) is the one that waits too.
test.expect = expect;

module.exports = { test, expect, reEscape };
