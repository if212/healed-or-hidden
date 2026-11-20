'use strict';
// Python's re.escape for the ports: a backslash before each of ()[]{}?*+-|^$\.&~# and the six
// whitespace characters, so that a port's pattern has the source text of the Python test's.
// The package's entry exports it; it needs nothing installed.

/** Escape a string for use in a RegExp, as Python's re.escape does for a pattern. */
function reEscape(text) {
  return String(text).replace(/[()[\]{}?*+\-|^$\\.&~# \t\n\r\v\f]/g, '\\$&');
}

module.exports = { reEscape };
