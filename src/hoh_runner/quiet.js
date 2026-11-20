// Keeps track of activity in one document: DOM changes, finished requests and
// the load event. Registered to run when a document is created; running it
// again in the same document does nothing.
(() => {
  const KEY = '__pageActivity__';
  if (Object.prototype.hasOwnProperty.call(window, KEY)) return;

  // Taken before any page script runs, so a page that replaces its clock later
  // does not change what is measured here.
  const now = performance.now.bind(performance);

  const id = Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  let count = 0;
  let last = now();
  const seen = (n) => {
    count += n;
    last = now();
  };

  new MutationObserver((records) => seen(records.length)).observe(document, {
    subtree: true,
    childList: true,
    attributes: true,
    characterData: true,
  });
  try {
    new PerformanceObserver((list) => seen(list.getEntries().length)).observe({ type: 'resource' });
  } catch (err) {
    // No resource timing in this document; DOM changes and load are still seen.
  }
  window.addEventListener('load', () => seen(1));
  window.addEventListener('pageshow', () => seen(1));

  // "<document id>:<activity count>"
  const stamp = () => id + ':' + count;
  // The stamp once the document has finished loading and nothing has happened
  // for `ms`; false until then.
  const quiet = (ms) =>
    document.readyState === 'complete' && now() - last >= ms ? stamp() : false;

  Object.defineProperty(window, KEY, { value: Object.freeze({ stamp, quiet }) });
})();
