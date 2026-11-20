/*
 * Rename or remove one interface control, from document start and for the life
 * of the page. Registered as a page init script; the rule list below is filled
 * in by the Python side. A rule finds elements the way a role locator does: by
 * role, then by accessible name (exact and case-sensitive, or a case-insensitive
 * substring), both with whitespace collapsed.
 */
(() => {
  const RULES = /* RULES */ [];
  // Stay out of the way until the session key says the app is ready (set after login),
  // like the other page script; null means always active.
  const READY = /* READY */ null;
  if (!RULES.length) return;
  if (READY !== null && sessionStorage.getItem(READY) !== "true") return;

  const ROLE_SELECTORS = {
    link: "a[href]:not([role]), area[href]:not([role]), [role~=link]",
    button:
      "button:not([role]), input[type=button]:not([role]), input[type=submit]:not([role])," +
      " input[type=reset]:not([role]), [role~=button]",
    tab: "[role~=tab]",
    menuitem: "[role~=menuitem]",
    heading: "h1:not([role]), h2:not([role]), h3:not([role]), h4:not([role])," +
      " h5:not([role]), h6:not([role]), [role~=heading]",
    cell: "td:not([role]), [role~=cell], [role~=gridcell]"
  };
  const SKIP = new Set(["SCRIPT", "STYLE", "TEMPLATE", "NOSCRIPT"]);

  const norm = (s) => String(s).replace(/\s+/g, " ").trim();
  const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  const rules = RULES.map((r) => {
    const name = norm(r.name);
    const words = name.split(" ").map(escapeRe).join("\\s+");
    return {
      op: r.op,
      exact: !!r.exact,
      name: name,
      lower: name.toLowerCase(),
      newName: r.newName === null ? null : norm(r.newName),
      selector: ROLE_SELECTORS[r.role],
      pattern: new RegExp(words, r.exact ? "g" : "gi")
    };
  });
  const ANY = Array.from(new Set(rules.map((r) => r.selector))).join(", ");

  const isInputButton = (el) =>
    el.tagName === "INPUT" && /^(button|submit|reset)$/i.test(el.getAttribute("type") || "");

  // Pieces of an element's content that make up its name: text nodes, and the
  // attributes that stand in for a child element (aria-label, img alt).
  function contentPieces(el, out) {
    for (let n = el.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 3) {
        out.push({ node: n, attr: null, text: n.data });
      } else if (n.nodeType === 1) {
        if (SKIP.has(n.tagName) || n.hidden || n.getAttribute("aria-hidden") === "true") continue;
        const label = n.getAttribute("aria-label");
        if (label && label.trim()) {
          out.push({ node: n, attr: "aria-label", text: label });
        } else if (n.tagName === "IMG") {
          out.push({ node: n, attr: "alt", text: n.getAttribute("alt") || "" });
        } else {
          contentPieces(n, out);
        }
      }
    }
    return out;
  }

  // The accessible name and the pieces it comes from, in the order of precedence
  // a role locator uses: aria-labelledby, aria-label, value of a button input,
  // content, title.
  function nameOf(el) {
    const ids = (el.getAttribute("aria-labelledby") || "").trim();
    if (ids) {
      const pieces = [];
      for (const id of ids.split(/\s+/)) {
        const ref = el.ownerDocument.getElementById(id);
        if (ref) contentPieces(ref, pieces);
      }
      const text = norm(pieces.map((p) => p.text).join(""));
      if (text) return { text, pieces };
    }
    const label = el.getAttribute("aria-label");
    if (label && label.trim()) {
      return { text: norm(label), pieces: [{ node: el, attr: "aria-label", text: label }] };
    }
    if (isInputButton(el)) {
      let value = el.getAttribute("value");
      if (value === null) {
        const type = (el.getAttribute("type") || "").toLowerCase();
        value = type === "submit" ? "Submit" : type === "reset" ? "Reset" : "";
      }
      if (norm(value)) return { text: norm(value), pieces: [{ node: el, attr: "value", text: value }] };
    } else {
      const pieces = contentPieces(el, []);
      const text = norm(pieces.map((p) => p.text).join(""));
      if (text) return { text, pieces };
    }
    const title = el.getAttribute("title");
    if (title && title.trim()) {
      return { text: norm(title), pieces: [{ node: el, attr: "title", text: title }] };
    }
    return { text: "", pieces: [] };
  }

  function matches(rule, text) {
    return rule.exact ? text === rule.name : text.toLowerCase().includes(rule.lower);
  }

  function rename(rule, name) {
    let changed = false;
    for (const piece of name.pieces) {
      rule.pattern.lastIndex = 0;
      if (!rule.pattern.test(piece.text)) continue;
      rule.pattern.lastIndex = 0;
      const next = piece.text.replace(rule.pattern, rule.newName);
      if (piece.attr === null) piece.node.data = next;
      else piece.node.setAttribute(piece.attr, next);
      changed = true;
    }
    // A name split across pieces (e.g. "Save <b>Book</b>") cannot be renamed piecewise.
    return changed;
  }

  function apply(el) {
    for (const rule of rules) {
      if (!el.isConnected || !el.matches(rule.selector)) continue;
      const name = nameOf(el);
      if (!name.text || !matches(rule, name.text)) continue;
      if (rule.op === "remove") {
        el.remove();
        return;
      }
      rename(rule, name);
    }
  }

  function sweep(root) {
    if (!root || !root.querySelectorAll) return;
    if (root.nodeType === 1 && root.matches(ANY)) apply(root);
    for (const el of root.querySelectorAll(ANY)) apply(el);
  }

  // Elements whose name may have changed with this batch of records.
  function affected(records) {
    const seen = new Set();
    const add = (el) => {
      if (el && el.nodeType === 1) {
        const hit = el.closest(ANY);
        if (hit) seen.add(hit);
      }
    };
    const roots = [];
    for (const r of records) {
      if (r.type === "characterData") {
        add(r.target.parentElement);
      } else if (r.type === "attributes") {
        add(r.target);
      } else {
        add(r.target.nodeType === 1 ? r.target : null);
        for (const n of r.addedNodes) if (n.nodeType === 1) roots.push(n);
      }
    }
    return { seen, roots };
  }

  // Our own writes come back as records too. They are harmless: a renamed element
  // no longer matches (a new name may not contain the old one) and a removed one is
  // gone, so the second look finds nothing to do and writes nothing.
  const observer = new MutationObserver((records) => {
    const { seen, roots } = affected(records);
    for (const root of roots) if (root.isConnected) sweep(root);
    for (const el of seen) apply(el);
  });
  observer.observe(document, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: [
      "aria-label", "aria-labelledby", "aria-hidden", "hidden", "value", "title", "role",
      "href", "type", "alt"
    ]
  });

  const full = () => sweep(document);
  full();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", full, { once: true });
  }
  window.addEventListener("load", full, { once: true });
})();
