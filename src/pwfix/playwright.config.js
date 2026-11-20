'use strict';
// The configuration of every fidelity run, given with -c: the judge's runs (pwrun.entry) and
// the official healer's MCP server. The tests are the workspace's tests/ and the results go
// to its test-results/, the workspace being the working directory.
//
// No retries and one worker, as the study's runs. The test timeout is the study's
// HOH_RUN_TIMEOUT_S (300 s). The expect timeout (5 s) and the action and navigation timeouts
// (30 s) are Playwright for Python's defaults, which the Python tests run with; Playwright
// Test's own defaults would be no limit for actions and navigations.

const path = require('node:path');

const report = process.env.PWFIX_REPORT;

module.exports = {
  testDir: path.resolve(process.cwd(), 'tests'),
  outputDir: path.resolve(process.cwd(), 'test-results'),
  retries: 0,
  workers: 1,
  fullyParallel: false,
  timeout: 300_000,
  expect: { timeout: 5_000 },
  use: { actionTimeout: 30_000, navigationTimeout: 30_000 },
  reporter: report ? [['list'], [path.join(__dirname, 'reporter.js')]] : [['list']],
};
