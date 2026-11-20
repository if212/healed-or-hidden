'use strict';
// The fixture's settings: the study runner's HOH_ variables (preflight.Settings) and the
// fidelity check's own PWFIX_ ones. Read when the package loads. In a worker process they then
// leave process.env, before any test code runs, as the runner's plugin clears the HOH_
// settings: a test has no business with where the browser is. (The runner's main process keeps
// them: its workers inherit them from it.)

// [variable, default]; a null default means the variable must be set. HOH_QUIET_MS has none:
// the Python runner falls back to 500 ms and the study always sets 800, and without a default
// a healer's run and a judge's run cannot wait differently without a trace. The settle cap keeps
// the Python runner's default (hoh_runner.quiet.TIMEOUT_MS), since the study never sets it.
const NUMBERS = {
  quietMs: ['HOH_QUIET_MS', null],
  settleTimeoutMs: ['HOH_SETTLE_TIMEOUT_MS', 5000],
  preflightTimeoutS: ['HOH_PREFLIGHT_TIMEOUT_S', 60],
  resetTimeoutS: ['PWFIX_RESET_TIMEOUT_S', 900],
};
const PREFIXES = ['HOH_', 'PWFIX_'];

/** {values, problem}: problem is a sentence when the settings cannot be used, else null. */
function read(env) {
  const text = (name) => (typeof env[name] === 'string' ? env[name].trim() : '');
  let problem = null;
  const number = (name, fallback) => {
    const raw = text(name);
    if (!raw) {
      if (fallback === null) problem ??= `${name} is not set`;
      return fallback;
    }
    const value = Number(raw);
    if (!Number.isFinite(value)) {
      problem ??= `${name} is not a number: ${JSON.stringify(raw)}`;
      return fallback;
    }
    if (value < 0) {
      problem ??= `${name} is negative: ${JSON.stringify(raw)}`;
      return fallback;
    }
    return value;
  };
  const cdpUrl = text('HOH_CDP_URL');
  if (!cdpUrl) problem = 'HOH_CDP_URL is not set';
  const values = {
    cdpUrl,
    startUrl: text('HOH_START_URL') || null,
    readyKey: text('HOH_READY_KEY') || null,
  };
  for (const [key, [name, fallback]] of Object.entries(NUMBERS)) {
    if (key !== 'resetTimeoutS') values[key] = number(name, fallback);
  }
  values.controlDir = text('PWFIX_CONTROL_DIR') || null;
  values.reset = text('PWFIX_RESET') === '1';
  values.resetTimeoutS = number(...NUMBERS.resetTimeoutS);
  return { values, problem };
}

/** The waiting values a run's record names, in the record's words. */
function timing(values) {
  return { quiet_ms: values.quietMs, settle_timeout_ms: values.settleTimeoutMs };
}

/** read(env); in a worker, every HOH_ and PWFIX_ variable then leaves env. */
function take(env = process.env) {
  const result = read(env);
  if (env.TEST_WORKER_INDEX !== undefined) {
    for (const name of Object.keys(env)) {
      if (PREFIXES.some((prefix) => name.startsWith(prefix))) delete env[name];
    }
  }
  return result;
}

module.exports = { NUMBERS, read, take, timing };
