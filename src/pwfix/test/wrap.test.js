'use strict';
// The proxy that puts the wait in front of every browser call a test makes, on fake classes.
const test = require('node:test');
const assert = require('node:assert/strict');

const { Wrapper, READS, className } = require('../lib/wrap');

const API = {
  classes: {
    Page: {
      extends: [],
      async: ['click', 'goto', 'title', 'count', 'textContent', 'evaluate'],
      // prependOnceListener is not in the real table either: a method it does not list is
      // called as a synchronous one.
      sync: ['getByRole', 'locator', 'url', 'context', 'on', 'once', 'addListener', 'prependListener',
        'off', 'removeListener'],
      mixed: ['removeAllListeners'],
      properties: ['keyboard'],
    },
    Frame: { extends: [], async: ['goto'], sync: ['page'], mixed: [], properties: [] },
    BrowserContext: { extends: [], async: [], sync: ['pages'], mixed: [], properties: [] },
    Locator: {
      extends: [],
      async: ['click', 'count', 'isVisible', 'elementHandle', 'all'],
      sync: ['filter', 'first', 'page', 'nth'],
      mixed: [],
      properties: [],
    },
    Keyboard: { extends: [], async: ['press'], sync: [], mixed: [], properties: [] },
    JSHandle: { extends: [], async: ['evaluate'], sync: ['asElement'], mixed: [], properties: [] },
    ElementHandle: { extends: ['JSHandle'], async: ['click'], sync: [], mixed: [], properties: [] },
  },
};

/** A recorder of the order in which waits and browser calls happen. */
function makeHooks({ stamps = [] } = {}) {
  const log = [];
  return {
    log,
    async settle() {
      log.push('settle');
      return { ok: true, stamp: stamps.length ? stamps.shift() : 's:0' };
    },
    async stamp() {
      log.push('stamp');
      return stamps.length ? stamps.shift() : 's:0';
    },
    onCall(name) {
      log.push(`call ${name}`);
    },
    onRecheck() {
      log.push('recheck');
    },
    onUnsettled(name) {
      log.push(`unsettled ${name}`);
    },
  };
}

class Keyboard {
  constructor(log) {
    this.log = log;
  }

  async press(key) {
    this.log.push(`press ${key}`);
  }
}

class Locator {
  constructor(page, selector) {
    this._page = page;
    this._selector = selector;
  }

  filter(options) {
    return new Locator(this._page, `${this._selector} >> filter(${options.has ? options.has._selector : options.hasText})`);
  }

  first() {
    return new Locator(this._page, `${this._selector} >> nth=0`);
  }

  page() {
    return this._page;
  }

  async click() {
    this._page.log.push(`click ${this._selector}`);
  }

  async count() {
    this._page.log.push(`count ${this._selector}`);
    return this._page.counts.length ? this._page.counts.shift() : 0;
  }

  async isVisible() {
    throw new Error('target closed');
  }

  async all() {
    return [new Locator(this._page, 'a'), new Locator(this._page, 'b')];
  }

  async elementHandle() {
    // eslint-disable-next-line no-use-before-define
    return new ElementHandle(this._page.log);
  }
}

class JSHandle {
  constructor(log) {
    this.log = log;
  }

  async evaluate() {
    this.log.push('handle evaluate');
    return 1;
  }

  asElement() {
    return this;
  }
}

class ElementHandle extends JSHandle {
  async click() {
    this.log.push('handle click');
  }
}

class BrowserContext {
  constructor(page) {
    this.page = page;
  }

  pages() {
    return [this.page];
  }
}

// Named as the bundled client names its class.
const _Page = class _Page {
  constructor() {
    this.log = [];
    this.counts = [];
    this._keyboard = new Keyboard(this.log);
    this._context = new BrowserContext(this);
    this.handlers = {};
  }

  get keyboard() {
    return this._keyboard;
  }

  getByRole(role, options) {
    return new Locator(this, `role=${role}[name=${options.name}]`);
  }

  locator(selector) {
    return new Locator(this, selector);
  }

  url() {
    return 'http://app/start';
  }

  context() {
    return this._context;
  }

  on(event, handler) {
    (this.handlers[event] ??= []).push(handler);
    return this;
  }

  once(event, handler) {
    return this.on(event, handler);
  }

  addListener(event, handler) {
    return this.on(event, handler);
  }

  prependListener(event, handler) {
    (this.handlers[event] ??= []).unshift(handler);
    return this;
  }

  prependOnceListener(event, handler) {
    return this.prependListener(event, handler);
  }

  off(event, handler) {
    this.handlers[event] = (this.handlers[event] || []).filter((h) => h !== handler);
    return this;
  }

  removeListener(event, handler) {
    return this.off(event, handler);
  }

  /** The browser side: an event reaches every handler, with the real objects. */
  emit(event, ...args) {
    for (const handler of this.handlers[event] || []) handler(...args);
  }

  removeAllListeners() {
    this.log.push('removeAll');
    return this;
  }

  async click(selector) {
    this.log.push(`page click ${selector}`);
  }

  async goto(url) {
    this.log.push(`goto ${url}`);
  }

  async title() {
    return this === this.self ? 'raw this' : 'title';
  }

  async evaluate(arg) {
    this.log.push(`evaluate ${typeof arg}`);
    this.lastArg = arg;
    return arg;
  }

  async textContent() {
    return 'text';
  }

  _private() {
    return this;
  }
};

// An event object a handler gets, named as the bundled client names its class: not one of the
// classes the table lists, but it leads to the page.
const _ConsoleMessage = class _ConsoleMessage {
  constructor(page) {
    this._page = page;
  }

  page() {
    return this._page;
  }

  text() {
    return 'hello';
  }

  async args() {
    return [new Locator(this._page, 'arg')];
  }
};

function setup(options) {
  const page = new _Page();
  page.self = page;
  const hooks = makeHooks(options);
  const wrapper = new Wrapper(API, hooks);
  return { page, hooks, wrapper, wrapped: wrapper.wrap(page) };
}

test('the class of an object is its constructor\'s name without the bundle\'s marks', () => {
  assert.equal(className(new _Page(), API), 'Page');
  assert.equal(className(new ElementHandle([]), API), 'ElementHandle');
  const Keyboard2 = class {};
  assert.equal(className(new Keyboard2(), { classes: { Keyboard: {} } }), 'Keyboard');
  assert.equal(className({}, API), null);
});

test('an asynchronous method waits for a quiet page first', async () => {
  const { page, hooks, wrapped } = setup();
  await wrapped.click('#a');
  assert.deepEqual(hooks.log, ['settle', 'call Page.click']);
  assert.deepEqual(page.log, ['page click #a']);
});

test('a synchronous method returns at once, without a wait, and its result is wrapped', async () => {
  const { hooks, wrapper, wrapped } = setup();
  const locator = wrapped.getByRole('button', { name: 'Save' });
  assert.ok(!(locator instanceof Promise));
  assert.ok(wrapper.isWrapped(locator));
  assert.deepEqual(hooks.log, []);
  assert.equal(wrapped.url(), 'http://app/start');
  await locator.click();
  assert.deepEqual(hooks.log, ['settle', 'call Locator.click']);
});

test('the original runs with the real object as this', async () => {
  const { wrapped } = setup();
  assert.equal(await wrapped.title(), 'raw this');
});

test('a property that holds a Playwright object is wrapped, and its calls wait', async () => {
  const { page, hooks, wrapped } = setup();
  await wrapped.keyboard.press('Enter');
  assert.deepEqual(hooks.log, ['settle', 'call Keyboard.press']);
  assert.deepEqual(page.log, ['press Enter']);
});

test('an object is wrapped once: the same proxy every time', () => {
  const { wrapper, wrapped } = setup();
  assert.equal(wrapped.context().pages()[0], wrapped);
  const locator = wrapped.locator('#x');
  assert.equal(locator.page(), wrapped);
  assert.equal(wrapper.wrap(wrapper.unwrap(locator)), locator);
});

test('arrays of Playwright objects are wrapped one by one', async () => {
  const { hooks, wrapper, wrapped } = setup();
  const all = await wrapped.locator('li').all();
  assert.equal(all.length, 2);
  assert.ok(all.every((locator) => wrapper.isWrapped(locator)));
  hooks.log.length = 0;
  await all[1].click();
  assert.deepEqual(hooks.log, ['settle', 'call Locator.click']);
});

test('wrapped arguments reach the original unwrapped, also inside an options object', async () => {
  const { page, wrapper, wrapped } = setup();
  const inner = wrapped.locator('h3');
  const outer = wrapped.locator('form').filter({ has: inner });
  assert.equal(wrapper.unwrap(outer)._selector, 'form >> filter(h3)');
  const result = await wrapped.evaluate(inner);
  assert.equal(page.lastArg, wrapper.unwrap(inner));
  assert.equal(result, inner); // what comes back is wrapped again
  assert.deepEqual(page.log, ['evaluate object']);
});

test('names that start with an underscore are the real object\'s, bound to it, with no wait', () => {
  const { page, hooks, wrapped } = setup();
  assert.equal(wrapped._private(), page);
  assert.equal(wrapped.locator('a')._selector, 'a');
  assert.deepEqual(hooks.log, []);
});

test('a read is made again while the page changes under it, three passes at most', async () => {
  const { page, hooks, wrapped } = setup({ stamps: ['a:1', 'a:2', 'a:2', 'a:3', 'a:4', 'a:5'] });
  page.counts.push(1, 2, 3);
  const count = await wrapped.locator('li').count();
  assert.equal(count, 3);
  assert.deepEqual(hooks.log, [
    'settle', 'call Locator.count', 'stamp', 'recheck',
    'settle', 'call Locator.count', 'stamp', 'recheck',
    'settle', 'call Locator.count', 'stamp',
  ]);
});

test('a read on a page that held still is made once', async () => {
  const { page, hooks, wrapped } = setup({ stamps: ['a:1', 'a:1'] });
  page.counts.push(7);
  assert.equal(await wrapped.locator('li').count(), 7);
  assert.deepEqual(hooks.log, ['settle', 'call Locator.count', 'stamp']);
});

test('a read whose stamp before could not be taken is made again', async () => {
  const hooks = makeHooks();
  hooks.settle = async () => {
    hooks.log.push('settle');
    return { ok: false, stamp: null };
  };
  const page = new _Page();
  page.counts.push(1, 2, 3);
  const wrapped = new Wrapper(API, hooks).wrap(page);
  assert.equal(await wrapped.locator('li').count(), 3);
  assert.equal(hooks.log.filter((entry) => entry === 'recheck').length, 2);
});

test('a read that fails fails at once, as it is', async () => {
  const { hooks, wrapped } = setup();
  await assert.rejects(wrapped.locator('li').isVisible(), /target closed/);
  assert.deepEqual(hooks.log, ['settle', 'call Locator.isVisible']);
});

test('reads on a page are made again too', async () => {
  const { hooks, wrapped } = setup({ stamps: ['a:1', 'a:1'] });
  assert.equal(await wrapped.textContent('#x'), 'text');
  assert.deepEqual(hooks.log, ['settle', 'call Page.textContent', 'stamp']);
  assert.ok(READS.includes('textContent'));
});

test('goto refuses file: addresses, as the browser refuses them to a web page', async () => {
  const { page, hooks, wrapped } = setup();
  await assert.rejects(wrapped.goto(' FILE:///etc/passwd'), /Page.goto: net::ERR_ACCESS_DENIED at  FILE:\/\/\/etc\/passwd/);
  await assert.rejects(wrapped.goto('view-source:file:///x'), /ERR_ACCESS_DENIED/);
  assert.deepEqual(page.log, []);
  assert.deepEqual(hooks.log, []);
  await wrapped.goto('http://app/books');
  assert.deepEqual(page.log, ['goto http://app/books']);
});

test('a method listed as synchronous that returns a promise is recorded, and still works', async () => {
  const { hooks, wrapper, wrapped } = setup();
  const raw = wrapper.unwrap(wrapped);
  raw.url = async () => 'later';
  assert.equal(await wrapped.url(), 'later');
  assert.deepEqual(hooks.log, ['unsettled Page.url']);
});

test('a method with mixed overloads goes straight through', () => {
  const { page, hooks, wrapped } = setup();
  assert.equal(wrapped.removeAllListeners(), page);
  assert.deepEqual(hooks.log, []);
});

test('an element handle has its own methods and those of a JS handle, all waited for', async () => {
  const { hooks, wrapper, wrapped } = setup();
  const handle = await wrapped.locator('a').elementHandle();
  assert.ok(wrapper.isWrapped(handle));
  hooks.log.length = 0;
  await handle.click();
  await handle.evaluate();
  assert.deepEqual(hooks.log, ['settle', 'call ElementHandle.click', 'settle', 'call ElementHandle.evaluate']);
  assert.equal(handle.asElement(), handle);
});

test('an object that is not Playwright\'s, or a primitive, is returned as it is', () => {
  const { wrapper } = setup();
  const plain = { a: 1 };
  assert.equal(wrapper.wrap(plain), plain);
  assert.equal(wrapper.wrap(null), null);
  assert.equal(wrapper.wrap('x'), 'x');
  assert.equal(wrapper.unwrap(plain), plain);
});

for (const method of ['on', 'once', 'addListener', 'prependListener', 'prependOnceListener']) {
  test(`a handler added with ${method} gets Playwright's objects wrapped`, async () => {
    const { page, hooks, wrapper, wrapped } = setup();
    const seen = [];
    assert.equal(wrapped[method]('popup', (popup) => seen.push(popup)), wrapped);
    page.emit('popup', page);
    assert.equal(seen.length, 1);
    assert.equal(seen[0], wrapped);
    assert.equal(wrapper.isWrapped(seen[0]), true);
    await seen[0].click('#x');
    assert.deepEqual(hooks.log, ['settle', 'call Page.click']);
    assert.deepEqual(page.log, ['page click #x']);
  });
}

test('the page an event object leads to is wrapped, so a read in a handler waits', async () => {
  const { page, hooks, wrapper, wrapped } = setup();
  const seen = [];
  wrapped.on('console', (message) => seen.push(message));
  page.emit('console', new _ConsoleMessage(page));
  const [message] = seen;
  assert.equal(message.text(), 'hello');
  assert.equal(message.page(), wrapped);
  const [arg] = await message.args();
  assert.equal(wrapper.isWrapped(arg), true);
  assert.equal(await message.page().textContent('#d'), 'text');
  assert.deepEqual(hooks.log, ['settle', 'call Page.textContent', 'stamp']);
});

test('a handler is taken off with off or removeListener as the test gave it', () => {
  for (const method of ['off', 'removeListener']) {
    const { page, wrapped } = setup();
    const seen = [];
    const handler = (popup) => seen.push(popup);
    wrapped.on('popup', handler);
    wrapped.on('popup', handler);
    assert.equal(wrapped[method]('popup', handler), wrapped);
    page.emit('popup', page);
    assert.deepEqual(seen, [], method);
  }
});

test('other arguments of a listener method pass as they are', () => {
  const { page, wrapped } = setup();
  wrapped.on('console', 'not a function');
  assert.deepEqual(page.handlers.console, ['not a function']);
});
