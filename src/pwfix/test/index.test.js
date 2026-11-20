'use strict';
// The package's entry, loaded as a ported test loads it. It needs @playwright/test installed
// (npm ci in this directory, as for the browser tests); without it the test is skipped.
const test = require('node:test');
const assert = require('node:assert/strict');

let installed = true;
try {
  require.resolve('@playwright/test');
} catch {
  installed = false;
}

test('test.expect is the exported expect, which waits for a quiet page', { skip: !installed && 'no @playwright/test here' }, () => {
  const pwfix = require('../index');
  assert.equal(typeof pwfix.test, 'function');
  assert.equal(pwfix.test.expect, pwfix.expect);
});

test('the entry exports the reEscape the ports import', { skip: !installed && 'no @playwright/test here' }, () => {
  assert.equal(require('../index').reEscape, require('../lib/escape').reEscape);
});
