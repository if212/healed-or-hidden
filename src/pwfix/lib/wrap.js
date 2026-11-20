'use strict';
// Make every browser call a test makes wait for a quiet page: the study runner's hooks.py, for
// Playwright Test.
//
// In Python every call of the synchronous API passes through one method, which the runner
// wraps. Playwright for TypeScript has no such method, so the page the test gets is a proxy,
// and so is every Playwright object reached from it by a method or a property: locators, frame
// locators, frames, the keyboard, the mouse, element and JS handles, the context and its pages.
// Which methods talk to the browser comes from the type definitions (api.json, made by
// tools/pwfix_api.js): a method that returns a promise waits for a quiet page first; one that
// returns at once (a locator built, an address the client already holds) does not wait,
// because it cannot without changing what it returns. Page.url() is such a method: in Python
// the address is read after a wait, here it is not.
//
// The reads in READS are made again while the page's stamp changes under them, at most
// MAX_PASSES times, as hooks.py does: a value that a test reads and then checks with a plain
// expect is an assertion as well. A failed read is thrown as it is.
//
// A handler a test adds with one of LISTEN gets its arguments wrapped, so it never holds an
// unwrapped page, frame or locator: Playwright's objects as above, and the event objects of
// EVENT_CLASSES (a console message, a dialog, a request) with whatever their methods return
// wrapped, since they lead to the page (message.page()). Their own methods do not wait: they
// read what the client already holds, or the network. off and removeListener take the handler
// off as the test gave it.

const MAX_PASSES = 3;

// hooks.READS, in TypeScript's names; on Locator, and on Page and Frame where those take a
// selector.
const READS = [
  'allInnerTexts',
  'allTextContents',
  'ariaSnapshot',
  'count',
  'getAttribute',
  'innerHTML',
  'innerText',
  'inputValue',
  'isChecked',
  'isDisabled',
  'isEditable',
  'isEnabled',
  'isHidden',
  'isVisible',
  'textContent',
];
const READ_CLASSES = new Set(['Locator', 'Page', 'Frame']);
const FILE_URL = /^\s*(?:view-source:)?file:/i;
const LISTEN = new Set(['on', 'once', 'addListener', 'prependListener', 'prependOnceListener']);
const UNLISTEN = new Set(['off', 'removeListener']);
// The objects of Page's and BrowserContext's events that are not in api.json.
const EVENT_CLASSES = new Set([
  'ConsoleMessage', 'Dialog', 'Download', 'FileChooser', 'Request', 'Response', 'WebError',
  'WebSocket', 'Worker',
]);

/** The name of the first class in `object`'s prototype chain that `known` holds, or null. */
function knownClass(object, known) {
  if (object === null || typeof object !== 'object') return null;
  let proto = Object.getPrototypeOf(object);
  while (proto !== null && proto !== Object.prototype) {
    const ctor = proto.constructor;
    if (typeof ctor === 'function' && typeof ctor.name === 'string') {
      const name = ctor.name.replace(/^_+/, '').replace(/\d+$/, '');
      if (known(name)) return name;
    }
    proto = Object.getPrototypeOf(proto);
  }
  return null;
}

/** The API class of `object`: its constructor's name, or a parent's, without the bundle's marks. */
function className(object, api) {
  return knownClass(object, (name) => Object.prototype.hasOwnProperty.call(api.classes, name));
}

function isThenable(value) {
  return value !== null && (typeof value === 'object' || typeof value === 'function')
    && typeof value.then === 'function';
}

function isPlainObject(value) {
  if (value === null || typeof value !== 'object') return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

class Wrapper {
  /**
   * @param api the content of api.json
   * @param hooks settle() -> {ok, stamp}, stamp() -> stamp|null, onCall(name), onRecheck(),
   *   onUnsettled(name)
   */
  constructor(api, hooks) {
    this.api = api;
    this.hooks = hooks;
    this.proxies = new WeakMap(); // target -> proxy
    this.targets = new WeakMap(); // proxy -> target
    this.kinds = new Map(); // class name -> Map(method -> kind)
    this.listeners = new WeakMap(); // a test's handler -> the listener added for it
  }

  kindsOf(name) {
    let kinds = this.kinds.get(name);
    if (kinds === undefined) {
      kinds = new Map();
      const add = (cls) => {
        const entry = this.api.classes[cls];
        for (const parent of entry.extends || []) add(parent);
        for (const kind of ['async', 'sync', 'mixed']) {
          for (const method of entry[kind] || []) kinds.set(method, kind);
        }
      };
      add(name);
      this.kinds.set(name, kinds);
    }
    return kinds;
  }

  isWrapped(value) {
    return value !== null && typeof value === 'object' && this.targets.has(value);
  }

  /** The real object behind a proxy; anything else as it is. */
  unwrap(value) {
    return this.isWrapped(value) ? this.targets.get(value) : value;
  }

  /** Arguments for a real method: proxies replaced by their objects, also one level down. */
  unwrapArgs(args) {
    return args.map((arg) => {
      if (this.isWrapped(arg)) return this.unwrap(arg);
      if (Array.isArray(arg)) return arg.map((item) => this.unwrap(item));
      if (isPlainObject(arg)) {
        const copy = {};
        for (const [key, value] of Object.entries(arg)) {
          copy[key] = Array.isArray(value) ? value.map((item) => this.unwrap(item)) : this.unwrap(value);
        }
        return copy;
      }
      return arg;
    });
  }

  /** The proxy for a Playwright object (the same one every time), arrays element by element. */
  wrap(value) {
    if (Array.isArray(value)) return value.map((item) => this.wrap(item));
    if (value === null || typeof value !== 'object' || this.isWrapped(value)) return value;
    const cls = className(value, this.api);
    if (cls === null) return value;
    let proxy = this.proxies.get(value);
    if (proxy === undefined) {
      proxy = new Proxy(value, this.handler(cls));
      this.proxies.set(value, proxy);
      this.targets.set(proxy, value);
    }
    return proxy;
  }

  /** What a listener's handler gets: Playwright's objects and event objects wrapped. */
  wrapEvent(value) {
    if (Array.isArray(value)) return value.map((item) => this.wrapEvent(item));
    if (this.isWrapped(value) || className(value, this.api) !== null) return this.wrap(value);
    if (knownClass(value, (name) => EVENT_CLASSES.has(name)) === null) return value;
    let proxy = this.proxies.get(value);
    if (proxy === undefined) {
      proxy = new Proxy(value, this.eventHandler());
      this.proxies.set(value, proxy);
      this.targets.set(proxy, value);
    }
    return proxy;
  }

  /** The listener added for a test's handler: the same one every time, its arguments wrapped. */
  listener(handler) {
    let listener = this.listeners.get(handler);
    if (listener === undefined) {
      const wrapper = this;
      listener = function listener(...args) {
        return handler.apply(this, args.map((arg) => wrapper.wrapEvent(arg)));
      };
      this.listeners.set(handler, listener);
    }
    return listener;
  }

  /** The arguments of a listener method, with the test's handler replaced by its listener. */
  listenerArgs(method, args) {
    if (typeof args[1] !== 'function') return args;
    if (LISTEN.has(method)) return [args[0], this.listener(args[1]), ...args.slice(2)];
    if (UNLISTEN.has(method)) {
      return [args[0], this.listeners.get(args[1]) || args[1], ...args.slice(2)];
    }
    return args;
  }

  eventHandler() {
    const wrapper = this;
    return {
      get(target, prop) {
        const value = Reflect.get(target, prop, target);
        if (typeof prop === 'symbol' || prop.startsWith('_') || prop === 'constructor') {
          return typeof value === 'function' ? value.bind(target) : value;
        }
        if (typeof value !== 'function') return wrapper.wrap(value);
        return function event(...args) {
          const result = value.apply(target, wrapper.unwrapArgs(args));
          return isThenable(result) ? result.then((resolved) => wrapper.wrap(resolved)) : wrapper.wrap(result);
        };
      },
    };
  }

  /** Run a read or an assertion, again while the page changes under it (hooks.py _rechecked). */
  async rechecked(name, run) {
    for (let attempt = 1; ; attempt++) {
      const { stamp: before } = await this.hooks.settle();
      this.hooks.onCall(name);
      const result = await run();
      const after = await this.hooks.stamp();
      if (attempt === MAX_PASSES || (before !== null && before === after)) return result;
      this.hooks.onRecheck();
    }
  }

  handler(cls) {
    const wrapper = this;
    const kinds = this.kindsOf(cls);
    const reads = READ_CLASSES.has(cls);
    return {
      get(target, prop) {
        const value = Reflect.get(target, prop, target);
        if (typeof prop === 'symbol' || prop.startsWith('_') || prop === 'constructor') {
          return typeof value === 'function' ? value.bind(target) : value;
        }
        if (typeof value !== 'function') return wrapper.wrap(value);
        const name = `${cls}.${prop}`;
        const kind = kinds.get(prop);
        if (kind === 'mixed') return value.bind(target);
        if (kind === 'sync' || kind === undefined) {
          return function synchronous(...args) {
            const given = wrapper.listenerArgs(prop, args);
            const result = value.apply(target, wrapper.unwrapArgs(given));
            if (isThenable(result)) {
              wrapper.hooks.onUnsettled(name);
              return result.then((resolved) => wrapper.wrap(resolved));
            }
            return wrapper.wrap(result);
          };
        }
        if (reads && READS.includes(prop)) {
          return async function read(...args) {
            const real = wrapper.unwrapArgs(args);
            return wrapper.wrap(await wrapper.rechecked(name, () => value.apply(target, real)));
          };
        }
        return async function asynchronous(...args) {
          if (prop === 'goto' && typeof args[0] === 'string' && FILE_URL.test(args[0])) {
            throw new Error(`${name}: net::ERR_ACCESS_DENIED at ${args[0]}`);
          }
          const real = wrapper.unwrapArgs(args);
          await wrapper.hooks.settle();
          wrapper.hooks.onCall(name);
          return wrapper.wrap(await value.apply(target, real));
        };
      },
    };
  }
}

module.exports = { EVENT_CLASSES, LISTEN, MAX_PASSES, READS, Wrapper, className, isThenable };
