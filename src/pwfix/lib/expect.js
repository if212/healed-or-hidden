'use strict';
// The exported expect. A web-first assertion passes as soon as the content is there, which can
// be before the page has finished changing. On a wrapped page or locator each such matcher
// waits for a quiet page and is made again while the page's stamp changes under it, at most
// three passes (Wrapper.rechecked), as hooks.py does for Python's assertions. A failed
// assertion is thrown as it is. Every other use of expect is Playwright's own.

// The matchers of LocatorAssertions, PageAssertions and APIResponseAssertions in
// @playwright/test 1.65.1 (types/test.d.ts).
const ASYNC_MATCHERS = new Set([
  'toBeAttached', 'toBeChecked', 'toBeDisabled', 'toBeEditable', 'toBeEmpty', 'toBeEnabled',
  'toBeFocused', 'toBeHidden', 'toBeInViewport', 'toBeVisible', 'toContainClass',
  'toContainText', 'toHaveAccessibleDescription', 'toHaveAccessibleErrorMessage',
  'toHaveAccessibleName', 'toHaveAttribute', 'toHaveCSS', 'toHaveClass', 'toHaveCount',
  'toHaveId', 'toHaveJSProperty', 'toHaveRole', 'toHaveScreenshot', 'toHaveText', 'toHaveValue',
  'toHaveValues', 'toMatchAriaSnapshot', 'toHaveTitle', 'toHaveURL', 'toBeOK',
]);

function wrapMatchers(matchers, state, prefix) {
  return new Proxy(matchers, {
    get(target, prop) {
      const value = Reflect.get(target, prop, target);
      if (prop === 'not') return wrapMatchers(value, state, `${prefix}not.`);
      if (typeof prop !== 'string' || typeof value !== 'function' || !ASYNC_MATCHERS.has(prop)) {
        return typeof value === 'function' ? value.bind(target) : value;
      }
      return (...args) => {
        const real = state.wrapper.unwrapArgs(args);
        return state.wrapper.rechecked(`expect.${prefix}${prop}`, () => value.apply(target, real));
      };
    },
  });
}

/**
 * Wrap Playwright's expect. `currentState()` gives the page fixture's state while one is in
 * use ({wrapper}), else null.
 */
function wrapExpect(base, currentState) {
  const call = (fn) => (actual, ...rest) => {
    const state = currentState();
    if (state === null || !state.wrapper.isWrapped(actual)) return fn(actual, ...rest);
    return wrapMatchers(fn(state.wrapper.unwrap(actual), ...rest), state, '');
  };
  return new Proxy(base, {
    apply(target, thisArg, args) {
      return call((...a) => Reflect.apply(target, thisArg, a))(...args);
    },
    get(target, prop) {
      const value = Reflect.get(target, prop, target);
      if (prop === 'soft') return call((...a) => value.apply(target, a));
      if (prop === 'configure' || prop === 'extend') {
        return (...args) => wrapExpect(value.apply(target, args), currentState);
      }
      return value;
    },
  });
}

module.exports = { ASYNC_MATCHERS, wrapExpect };
