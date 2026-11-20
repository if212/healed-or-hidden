'use strict';
// The control directory a fidelity session shares with its watcher (hoh.fidelity). The study
// gives every run a fresh stack; the official healer's runs happen inside its MCP server, so
// before it connects the fixture asks for one: it writes requests/<id>.json and waits for
// answers/<id>.json, {"ok": true} or {"ok": false, "error": ...}. After each test it writes
// records/<id>.json, what the fixture saw, so the session can tell an environment failure from
// a result of the test. In the judge's runs there is no control directory.

const fs = require('node:fs');
const path = require('node:path');

const sleepFor = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function newId() {
  return `${Date.now().toString(36)}-${process.pid}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Write a JSON file whole: the reader never sees half of it. */
function writeWhole(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const partial = `${file}.part`;
  fs.writeFileSync(partial, `${JSON.stringify(data, null, 2)}\n`);
  fs.renameSync(partial, file);
  return file;
}

/** Ask for a fresh stack and wait for the answer; {id, seconds}, or an Error. */
async function requestReset(dir, { timeoutMs, pollMs = 250, sleep = sleepFor } = {}) {
  const id = newId();
  const started = Date.now();
  writeWhole(path.join(dir, 'requests', `${id}.json`), { id, pid: process.pid, at: new Date().toISOString() });
  const answer = path.join(dir, 'answers', `${id}.json`);
  for (;;) {
    if (fs.existsSync(answer)) {
      const data = JSON.parse(fs.readFileSync(answer, 'utf8'));
      if (data.ok !== true) throw new Error(`the reset failed: ${data.error || 'no reason given'}`);
      return { id, seconds: (Date.now() - started) / 1000 };
    }
    if (Date.now() - started >= timeoutMs) {
      throw new Error(`no answer to reset request ${id} within ${timeoutMs / 1000} s`);
    }
    await sleep(pollMs);
  }
}

/** Write one test's record; returns its path. */
function writeRecord(dir, record) {
  return writeWhole(path.join(dir, 'records', `${record.id}.json`), record);
}

module.exports = { newId, requestReset, writeRecord, writeWhole };
