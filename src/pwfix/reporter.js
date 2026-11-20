'use strict';
// A Playwright Test reporter that writes the report the study runner's pytest plugin writes
// (hoh_runner.plugin), so that pwrun.entry gives the run its status with hoh_runner.status,
// by the same rules as a Python run.
//
// Outcomes, in pytest's words: passed with expected status passed is "passed"; failed with
// expected passed is "failed"; skipped (test.skip, test.fixme) is "skipped"; a test marked
// test.fail(), the TypeScript xfail, is "xfailed" when it fails and "xpassed" when it passes;
// a timeout (over the 300 s test timeout), an interrupted run, or anything else is "error",
// which hoh_runner.status counts as broken. A test that ran more than once (its retries
// raised) keeps its first attempt's outcome and record, with the number of attempts. A file
// that cannot be loaded is a collection error. The fixture's records (one per test, attached as "pwfix-record") give the settle
// counts, the final address, the page errors and any environment failure.
//
// The file is named by PWFIX_REPORT, read when the reporter is loaded (in the runner's main
// process, before any test file). Without it the reporter writes nothing, as in a session.

const fs = require('node:fs');
const path = require('node:path');

const OUT = process.env.PWFIX_REPORT || null;
const MESSAGE_LIMIT = 4000;
const PAGE_ERRORS_KEPT = 3;
const ANSI = /\u001b\[[0-9;]*m/g;

function outcome(test, result) {
  if (result.status === 'timedOut' || result.status === 'interrupted') return 'error';
  if (result.status === 'skipped' || test.expectedStatus === 'skipped') return 'skipped';
  if (test.expectedStatus === 'passed') {
    if (result.status === 'passed') return 'passed';
    if (result.status === 'failed') return 'failed';
    return 'error';
  }
  if (test.expectedStatus === 'failed') {
    if (result.status === 'failed') return 'xfailed';
    if (result.status === 'passed') return 'xpassed';
  }
  return 'error';
}

/** The call a failure message names: the matcher of an expect, or the action that failed. */
function callOf(message) {
  const text = String(message || '');
  const matcher = /expect\([^)]*\)\.(?:not\.)?(to\w+)\(/.exec(text);
  if (matcher) return matcher[1];
  const action = /^\w*Error: [\w.]*\.(\w+): /.exec(text);
  return action ? action[1] : null;
}

function excType(message) {
  const match = /^(\w*Error)\b/.exec(String(message || ''));
  return match ? match[1] : null;
}

function clean(message) {
  return String(message || '').replace(ANSI, '');
}

class Reporter {
  constructor(options = {}) {
    this.out = options.out || OUT;
    this.items = [];
    this.tests = new Map();
    this.records = [];
    this.errors = [];
    this.root = null;
  }

  printsToStdio() {
    return false;
  }

  nodeid(test) {
    const titles = [];
    for (let suite = test.parent; suite && suite.type === 'describe'; suite = suite.parent) {
      titles.unshift(suite.title);
    }
    titles.push(test.title);
    const file = path.relative(this.root, test.location.file).split(path.sep).join('/');
    return `${file}::${titles.join(' > ')}`;
  }

  onBegin(config, suite) {
    const testDir = config.projects.length ? config.projects[0].testDir : process.cwd();
    // Node ids are relative to the parent of the tests directory, as pytest's are to the root:
    // tests/<file>::<title>.
    this.root = path.dirname(path.resolve(testDir));
    this.items = suite.allTests().map((test) => this.nodeid(test));
  }

  onTestEnd(test, result) {
    const nodeid = this.nodeid(test);
    const first = this.tests.get(nodeid);
    if (first) {
      // Another attempt of a test whose retries were raised (the config sets none): the study
      // judges a run on a fresh stack, so the first attempt keeps the outcome and the record.
      first.attempts += 1;
      return;
    }
    const message = result.error ? clean(result.error.message).slice(0, MESSAGE_LIMIT) : null;
    const location = result.error && result.error.location;
    this.tests.set(nodeid, {
      nodeid,
      outcome: outcome(test, result),
      phase: 'call',
      lineno: location && location.file === test.location.file ? location.line : null,
      exc_type: excType(message),
      call: callOf(message),
      message,
      duration_s: Math.round(result.duration) / 1000,
      status: result.status,
      expected_status: test.expectedStatus,
      attempts: 1,
    });
    for (const attachment of result.attachments || []) {
      if (attachment.name === 'pwfix-record' && attachment.body) {
        try {
          this.records.push(JSON.parse(attachment.body.toString('utf8')));
        } catch {
          this.errors.push('a fixture record could not be read');
        }
      }
    }
  }

  onError(error) {
    this.errors.push(clean(error && (error.message || error.value)).slice(0, MESSAGE_LIMIT));
  }

  report() {
    const settle = { calls: 0, timeouts: 0, rechecks: 0 };
    const pageErrors = { count: 0, first: [] };
    let envError = null;
    let finalUrl = null;
    for (const record of this.records) {
      for (const key of Object.keys(settle)) settle[key] += (record.settle && record.settle[key]) || 0;
      if (record.page_errors) {
        pageErrors.count += record.page_errors.count || 0;
        for (const text of record.page_errors.first || []) {
          if (pageErrors.first.length < PAGE_ERRORS_KEPT) pageErrors.first.push(text);
        }
      }
      envError ??= record.env_error || null;
      if (record.final_url !== undefined) finalUrl = record.final_url;
    }
    return {
      schema: 1,
      env_error: envError,
      collected: this.items.length,
      deselected: 0,
      skipped_collectors: 0,
      collect_errors: this.errors.length,
      errors: this.errors,
      items: this.items,
      tests: [...this.tests.values()],
      final_url: finalUrl,
      settle,
      page_errors: pageErrors,
      records: this.records,
    };
  }

  async onEnd() {
    if (!this.out) return;
    fs.mkdirSync(path.dirname(this.out), { recursive: true });
    const partial = `${this.out}.part`;
    fs.writeFileSync(partial, `${JSON.stringify(this.report(), null, 2)}\n`);
    fs.renameSync(partial, this.out);
  }
}

module.exports = Reporter;
module.exports.outcome = outcome;
module.exports.callOf = callOf;
