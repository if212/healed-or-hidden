'use strict';

// reEscape stands in for Python's re.escape in the ports, so a pattern built with it has the
// same source text as the Python test's: the source is what a failure message shows. It lives
// in a file that needs nothing installed, so these tests run without the package's dependencies.

const test = require('node:test');
const assert = require('node:assert/strict');
const { reEscape } = require('../lib/escape');

// Each pair is a string and re.escape(string) from Python 3.12.
const PYTHON = [
  ['Admin User, "Lecture 1"', 'Admin\\ User,\\ "Lecture\\ 1"'],
  ['a-b&c~d#e', 'a\\-b\\&c\\~d\\#e'],
  ['1.5*(x+y)?', '1\\.5\\*\\(x\\+y\\)\\?'],
  ['[a]{2}|^$\\', '\\[a\\]\\{2\\}\\|\\^\\$\\\\'],
  ['tab\tnl\ncr\rvt\u000bff\f', 'tab\\\tnl\\\ncr\\\rvt\\\u000bff\\\f'],
  ['!"%\',/:;<=>@`_9é', '!"%\',/:;<=>@`_9é'],
];

test('reEscape escapes what Python re.escape escapes, and nothing else', () => {
  for (const [text, escaped] of PYTHON) {
    assert.equal(reEscape(text), escaped, JSON.stringify(text));
  }
});

test('a pattern built with reEscape matches its text and nothing wider', () => {
  for (const [text] of PYTHON) {
    for (const flags of ['', 'i']) {
      const pattern = new RegExp('^' + reEscape(text) + '$', flags);
      assert.ok(pattern.test(text), JSON.stringify(text));
      assert.ok(!pattern.test(text + 'x'), JSON.stringify(text));
    }
  }
  assert.ok(!new RegExp(reEscape('a.c')).test('abc'));
  assert.ok(!new RegExp('^[' + reEscape('a-c') + ']$').test('b'));
});

test('reEscape takes what String() takes, as re.escape takes a str', () => {
  assert.equal(reEscape(1.5), '1\\.5');
});
