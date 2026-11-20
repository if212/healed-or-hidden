/*
 * One expression: a function ({ quietMs, timeoutMs }) => Promise<void>.
 * It resolves once the document is no longer loading and no DOM change (or
 * ready-state change) has been seen for quietMs, or once timeoutMs have passed
 * since the call, whichever comes first. It observes `document` itself, so it
 * works before the root element exists, keeps no global state, and removes its
 * observer and listener when done. Each call is independent.
 */
(function (opts) {
  var quietMs = opts && opts.quietMs >= 0 ? Number(opts.quietMs) : 300;
  var timeoutMs = opts && opts.timeoutMs >= 0 ? Number(opts.timeoutMs) : 3000;
  return new Promise(function (resolve) {
    var start = performance.now();
    var last = start;
    var timer = 0;
    var done = false;
    var observer = new MutationObserver(function () {
      last = performance.now();
    });
    function onState() {
      last = performance.now();
      check();
    }
    function finish() {
      if (done) return;
      done = true;
      clearTimeout(timer);
      observer.disconnect();
      document.removeEventListener("readystatechange", onState);
      resolve();
    }
    function check() {
      if (done) return;
      clearTimeout(timer);
      var now = performance.now();
      var left = timeoutMs - (now - start);
      if (left <= 0) return finish();
      var loading = document.readyState === "loading";
      var quietFor = now - last;
      if (!loading && quietFor >= quietMs) return finish();
      var wait = loading ? quietMs : quietMs - quietFor;
      timer = setTimeout(check, Math.max(1, Math.min(wait, left)));
    }
    observer.observe(document, {
      subtree: true,
      childList: true,
      attributes: true,
      characterData: true
    });
    document.addEventListener("readystatechange", onState);
    check();
  });
})
