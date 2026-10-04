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
    var btn = document.querySelector("[data-theme-toggle]");
    if (btn) {
      var eff = resolve(mode);
      var icon = mode === "system" ? "system" : (eff === "light" ? "moon" : "sun");
      btn.setAttribute("data-icon", icon);
      btn.setAttribute("aria-label", "Theme: " + mode + " (click to change)");
      btn.title = "Theme: " + mode[0].toUpperCase() + mode.slice(1);
    }
  }

  function getMode() { return localStorage.getItem(KEY) || "system"; }

  function cycle() {
    var order = ["system", "light", "dark"];
    var next = order[(order.indexOf(getMode()) + 1) % order.length];
    localStorage.setItem(KEY, next);
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
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.toggle("error", !!isError);
    t.classList.add("show");
    clearTimeout(t._timer);
    t._timer = setTimeout(function () { t.classList.remove("show"); }, 2400);
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
      btn.style.position = btn.style.position || "relative";
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
  function init() {
    var btn = document.querySelector("[data-theme-toggle]");
    if (btn) btn.addEventListener("click", cycle);
    bindRipples();
    bindReveal();
    animateCounters();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else { init(); }

  /* Expose a tiny public API for inline handlers */
  window.FL = { copy: copy, toast: toast, cycleTheme: cycle };
})();
