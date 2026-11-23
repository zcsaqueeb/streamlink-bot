/* ============================================================================
   Signal — Shared front-end runtime
   Theme system + toast + ripple + scroll reveal + copy helpers + counters.
   Loaded on every page. Kept dependency-free and tiny.
   ========================================================================== */
(function () {
  "use strict";

  /* ── Theme manager: light / dark / system, persisted, smooth ──────────── */
  var KEY = "fl_theme";          // 'light' | 'dark' | 'system'
  var root = document.documentElement;

  function systemPref() {
    return window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }

  function resolve(mode) { return mode === "system" ? systemPref() : mode; }

  function apply(mode) {
    root.setAttribute("data-theme", resolve(mode));
    root.setAttribute("data-theme-mode", mode);
    // Keep the mobile browser-bar colour in step with a manual choice.
    var tc = document.querySelectorAll('meta[name="theme-color"]');
    for (var i = 0; i < tc.length; i++) {
      tc[i].setAttribute("content", resolve(mode) === "light" ? "#F5F6F8" : "#0B0F14");
      tc[i].removeAttribute("media");
    }
    var btn = document.querySelector("[data-theme-toggle]");
    if (btn) {
      var eff = resolve(mode);
      var icon = mode === "system" ? "system" : (eff === "light" ? "moon" : "sun");
      btn.setAttribute("data-icon", icon);
      btn.setAttribute("aria-label", "Theme: " + mode + " (click to change)");
      btn.title = "Theme: " + mode[0].toUpperCase() + mode.slice(1);
    }
  }

  function getMode() {
    // localStorage throws in private/blocked-storage browsers; an uncaught
    // throw here used to abort the whole script (no copy, toast or reveal).
    try { return localStorage.getItem(KEY) || "system"; } catch (e) { return "system"; }
  }
  function setMode(m) { try { localStorage.setItem(KEY, m); } catch (e) {} }

  function cycle() {
    var order = ["system", "light", "dark"];
    var next = order[(order.indexOf(getMode()) + 1) % order.length];
    setMode(next);
    apply(next);
  }

  // Apply ASAP to avoid flash (also called inline in <head>).
  apply(getMode());

  // React to OS theme changes while in 'system' mode.
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: light)")
      .addEventListener("change", function () { if (getMode() === "system") apply("system"); });
  }

  /* ── Toast ─────────────────────────────────────────────────────────────── */
  function toast(msg, isError) {
    var t = document.getElementById("toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "toast"; t.className = "toast";
      t.setAttribute("role", "status"); t.setAttribute("aria-live", "polite");
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.toggle("error", !!isError);
    t.classList.add("show");
    clearTimeout(t._timer);
    t._timer = setTimeout(function () { t.classList.remove("show"); }, 2600);
  }

  /* ── Clipboard copy ────────────────────────────────────────────────────── */
  function copy(text, okMsg) {
    var done = function () { toast(okMsg || "Copied to clipboard"); };
    var fail = function () { toast("Copy failed", true); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(function () {
        legacyCopy(text) ? done() : fail();
      });
    } else {
      legacyCopy(text) ? done() : fail();
    }
  }
  function legacyCopy(text) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      var ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (e) { return false; }
  }

  /* ── Button ripple effect ──────────────────────────────────────────────── */
  function bindRipples() {
    document.addEventListener("click", function (e) {
      var btn = e.target.closest(".btn");
      if (!btn) return;
      var rect = btn.getBoundingClientRect();
      var r = document.createElement("span");
      r.className = "ripple";
      var size = Math.max(rect.width, rect.height);
      r.style.width = r.style.height = size + "px";
      r.style.left = (e.clientX - rect.left - size / 2) + "px";
      r.style.top = (e.clientY - rect.top - size / 2) + "px";
      if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      btn.style.overflow = "hidden";
      btn.appendChild(r);
      setTimeout(function () { r.remove(); }, 550);
    });
  }

  /* ── Scroll reveal (data-reveal attribute) ───────────────────────────────
     Matches the [data-reveal] CSS in theme.css — an element starts faded/
     offset and animates in once it enters the viewport. */
  function bindReveal() {
    var els = document.querySelectorAll("[data-reveal]");
    if (!els.length) return;
    if (!("IntersectionObserver" in window)) {
      els.forEach(function (el) { el.classList.add("in"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); }
      });
    }, { threshold: 0.12 });
    els.forEach(function (el) { io.observe(el); });
  }

  /* ── Animated counters (data-count="123") ────────────────────────────── */
  function animateCounters() {
    document.querySelectorAll("[data-count]").forEach(function (el) {
      var target = parseFloat(el.getAttribute("data-count")) || 0;
      var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (reduce) { el.textContent = Math.round(target).toLocaleString(); return; }
      var dur = 900, start = performance.now();
      function step(now) {
        var p = Math.min((now - start) / dur, 1);
        var eased = 1 - Math.pow(1 - p, 3);
        el.textContent = Math.round(target * eased).toLocaleString();
        if (p < 1) requestAnimationFrame(step);
      }
      requestAnimationFrame(step);
    });
  }

  /* ── Init ──────────────────────────────────────────────────────────────── */

  /* ── Scroll progress + condensed header ────────────────────────────────── */
  function bindScroll() {
    var hdr = document.querySelector(".site-header");
    if (!hdr) return;
    var ticking = false;
    function upd() {
      var h = document.documentElement;
      var max = h.scrollHeight - h.clientHeight;
      hdr.style.setProperty("--p", max > 0 ? Math.min(1, h.scrollTop / max).toFixed(4) : 0);
      hdr.classList.toggle("scrolled", h.scrollTop > 8);
      ticking = false;
    }
    window.addEventListener("scroll", function () {
      if (!ticking) { ticking = true; requestAnimationFrame(upd); }
    }, { passive: true });
    upd();
  }

  /* ── Cursor spotlight on cards ─────────────────────────────────────────── */
  function bindSpotlight() {
    if (window.matchMedia && !window.matchMedia("(hover:hover)").matches) return;
    document.addEventListener("pointermove", function (e) {
      var c = e.target.closest && e.target.closest(".card");
      if (!c) return;
      var r = c.getBoundingClientRect();
      c.style.setProperty("--mx", (e.clientX - r.left) + "px");
      c.style.setProperty("--my", (e.clientY - r.top) + "px");
    }, { passive: true });
  }

  /* ── Stagger siblings revealed together ────────────────────────────────── */
  function staggerReveal() {
    document.querySelectorAll(".feature-grid,.flow-rail,.recent-rail,.batch-grid").forEach(function (g) {
      Array.prototype.forEach.call(g.children, function (el, i) {
        if (el.hasAttribute("data-reveal") || g.classList.contains("feature-grid")) {
          el.setAttribute("data-reveal", "");
          el.style.setProperty("--d", Math.min(i, 8) * 70 + "ms");
        }
      });
    });
  }

  /* ── Colours extracted from the file thumbnail ─────────────────────────────
     Samples a tiny downscaled copy of the thumbnail, keeps the most saturated
     pixel buckets and exposes them as --dyn-a/b/c so the aurora, waves, side
     rails and card glow all pick up the file's own colours. Same-origin image
     (CSP allows it), wrapped in try/catch so a tainted canvas never matters. */
  function hsl(r, g, b) {
    r /= 255; g /= 255; b /= 255;
    var mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2, h = 0, s = 0, d = mx - mn;
    if (d) {
      s = l > .5 ? d / (2 - mx - mn) : d / (mx + mn);
      h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
      h *= 60;
    }
    return [h, s, l];
  }
  function extractColors() {
    var img = document.querySelector(".thumb-wrap img, video[poster]");
    var src = img && (img.currentSrc || img.src || img.getAttribute("poster"));
    if (!src) return;
    var im = new Image();
    im.onload = function () {
      try {
        var c = document.createElement("canvas"), n = 32;
        c.width = c.height = n;
        var x = c.getContext("2d", { willReadFrequently: true });
        x.drawImage(im, 0, 0, n, n);
        var d = x.getImageData(0, 0, n, n).data, buckets = {};
        for (var i = 0; i < d.length; i += 4) {
          var p = hsl(d[i], d[i + 1], d[i + 2]);
          if (p[1] < .28 || p[2] < .2 || p[2] > .85) continue;
          var k = Math.round(p[0] / 40);
          var b = buckets[k] || (buckets[k] = { n: 0, h: 0, s: 0 });
          b.n++; b.h += p[0]; b.s += p[1];
        }
        var list = Object.keys(buckets).map(function (k) { return buckets[k]; })
          .sort(function (a, b) { return b.n * b.s - a.n * a.s; }).slice(0, 3);
        if (!list.length) return;
        var light = document.documentElement.getAttribute("data-theme") === "light";
        var L = light ? "46%" : "62%";
        var root = document.documentElement.style;
        ["--dyn-a", "--dyn-b", "--dyn-c"].forEach(function (v, i) {
          var b = list[Math.min(i, list.length - 1)];
          var h = Math.round(b.h / b.n) + (list.length === 1 ? i * 38 : 0);
          root.setProperty(v, "hsl(" + h + " 82% " + L + ")");
        });
        document.documentElement.classList.add("has-dyn");
      } catch (e) { /* tainted or unreadable — keep brand colours */ }
    };
    im.src = src;
  }

  function init() {
    var btn = document.querySelector("[data-theme-toggle]");
    if (btn) btn.addEventListener("click", cycle);
    bindRipples();
    staggerReveal();
    bindReveal();
    bindScroll();
    bindSpotlight();
    extractColors();
    animateCounters();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else { init(); }

  /* Expose a tiny public API for inline handlers */
  window.FL = { copy: copy, toast: toast, cycleTheme: cycle };
})();
