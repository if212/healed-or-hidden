// Writes src/pwfix/lib/api.json: for each Playwright class a test can reach from the fixture's
// page, which methods return a promise (they talk to the browser, so the fixture waits for a
// quiet page before them) and which return at once (they build a locator or read what the
// client already holds, so the fixture must not make them wait).
//
// The source is the type definitions of the installed playwright-core: the declared return
// type of every overload. A method whose overloads disagree is listed as mixed (in 1.65.1 only
// removeAllListeners, which returns a promise only when asked to wait for running listeners);
// the fixture passes such a method through without a wait.
//
//   NODE_PATH=<dir with typescript> node tools/pwfix_api.js <playwright-core>/types/types.d.ts \
//       src/pwfix/lib/api.json
//
// Run it again, and commit the result, whenever the pinned @playwright/test version changes.
'use strict';

const fs = require('fs');
const path = require('path');
const ts = require('typescript');

const CLASSES = [
  'Page', 'Frame', 'BrowserContext', 'Locator', 'FrameLocator', 'Keyboard', 'Mouse',
  'Touchscreen', 'ElementHandle', 'JSHandle',
];

function table(typesPath) {
  const text = fs.readFileSync(typesPath, 'utf8');
  const source = ts.createSourceFile(typesPath, text, ts.ScriptTarget.Latest, true);
  const found = {};
  const visit = (node) => {
    if (ts.isInterfaceDeclaration(node) && CLASSES.includes(node.name.text)) {
      const entry = (found[node.name.text] ??= { kinds: new Map(), properties: new Set(), extends: new Set() });
      for (const clause of node.heritageClauses ?? []) {
        for (const type of clause.types) entry.extends.add(type.expression.getText(source));
      }
      for (const member of node.members) {
        if (ts.isMethodSignature(member)) {
          const name = member.name.getText(source);
          const kind = member.type && /^Promise\s*</.test(member.type.getText(source)) ? 'async' : 'sync';
          const before = entry.kinds.get(name);
          entry.kinds.set(name, before && before !== kind ? 'mixed' : kind);
        } else if (ts.isPropertySignature(member)) {
          entry.properties.add(member.name.getText(source));
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  const missing = CLASSES.filter((name) => !found[name]);
  if (missing.length) throw new Error(`not in ${typesPath}: ${missing.join(', ')}`);
  const out = {};
  for (const name of CLASSES) {
    const entry = found[name];
    const of = (kind) => [...entry.kinds].filter(([, k]) => k === kind).map(([m]) => m).sort();
    out[name] = {
      extends: [...entry.extends].sort(),
      async: of('async'),
      sync: of('sync'),
      mixed: of('mixed'),
      properties: [...entry.properties].sort(),
    };
  }
  return out;
}

function main(argv) {
  const [typesPath, outPath] = argv;
  if (!typesPath || !outPath) {
    console.error('usage: node tools/pwfix_api.js <types.d.ts> <out.json>');
    return 2;
  }
  const version = JSON.parse(
    fs.readFileSync(path.join(path.dirname(typesPath), '..', 'package.json'), 'utf8')).version;
  const data = { 'playwright-core': version, classes: table(typesPath) };
  fs.writeFileSync(outPath, JSON.stringify(data, null, 2) + '\n');
  return 0;
}

if (require.main === module) process.exitCode = main(process.argv.slice(2));

module.exports = { table, CLASSES };
