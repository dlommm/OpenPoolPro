/* OpenPoolPro website. Everything here is progressive: the page reads in full
   without it. */
(function () {
  "use strict";
  var doc = document.documentElement;
  doc.classList.add("js");
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- the rail docks as the header once the hero photo has passed ---------- */
  var rail = document.querySelector("[data-rail]");
  var detail = document.querySelector(".hero-detail");
  if (rail && detail && "IntersectionObserver" in window) {
    new IntersectionObserver(function (es) {
      es.forEach(function (e) { rail.classList.toggle("is-docked", e.boundingClientRect.top < 80); });
    }, { threshold: [0, 0.01, 0.1, 0.5, 1] }).observe(detail);
    window.addEventListener("scroll", function () {
      rail.classList.toggle("is-docked", detail.getBoundingClientRect().top < 80);
    }, { passive: true });
  }

  /* ---------- section entrances: heading, then text, then picture ---------- */
  var groups = [
    [".intro .h2", ".intro .lead", ".intro .facts li"],
    [".show-h", ".state-row", ".show-note"],
    ["#water-h", ".water .lead", ".calc"],
    [".features > .h2"],
    ["#yours-h", ".yours .lead", ".yours-clip", ".yours-list li"],
    [".proof-say", ".proof-row li"],
    [".faq-h", ".faq .q"]
  ];
  var items = [];
  groups.forEach(function (sels) {
    var n = 0;
    sels.forEach(function (sel) {
      document.querySelectorAll(sel).forEach(function (el) {
        el.classList.add("rv"); el.style.setProperty("--d", (n++ * 75) + "ms"); items.push(el);
      });
    });
  });
  document.querySelectorAll(".mod").forEach(function (m) {
    var k = 0;
    [m.querySelector(".mod-pic"), m.querySelector(".h3")].concat([].slice.call(m.querySelectorAll(".mod-text p"))).forEach(function (el) {
      if (!el) return; el.classList.add("rv"); el.style.setProperty("--d", (k++ * 80) + "ms"); items.push(el);
    });
  });
  if ("IntersectionObserver" in window && !reduce) {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
    }, { rootMargin: "0px 0px 12% 0px" });
    items.forEach(function (el) { io.observe(el); });
    // A fast jump (keyboard End, an anchor link) can skip past a section
    // before the observer reports it; anything already passed shows at once.
    window.addEventListener("scroll", function () {
      var lim = innerHeight * 1.1;
      items.forEach(function (el) { if (!el.classList.contains("in") && el.getBoundingClientRect().top < lim) el.classList.add("in"); });
    }, { passive: true });
  } else {
    items.forEach(function (el) { el.classList.add("in"); });
  }
  // Never leave content hidden: anything still waiting after 2.5s shows.
  setTimeout(function () { items.forEach(function (el) { if (el.getBoundingClientRect().top < innerHeight) el.classList.add("in"); }); }, 2500);

  /* ---------- the live stop: Mark done ---------- */
  var live = document.querySelector("[data-stop]");
  if (live) {
    live.querySelector("[data-done]").addEventListener("click", function () {
      live.classList.remove("is-live"); live.classList.add("is-done");
      live.querySelector("[data-stop-n]").innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M5 12l5 5L20 7" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg><span class="sr">Done</span>';
      var now = new Date(); var h = now.getHours(), m = now.getMinutes();
      live.querySelector("[data-stop-sub]").textContent =
        "Done " + (h % 12 || 12) + ":" + (m < 10 ? "0" : "") + m + (h < 12 ? " am" : " pm") + " · report emailed to Dana";
      var r = live.querySelector("[data-stop-r]");
      r.innerHTML = '<span class="tag">JR</span><span class="pill p-pos">Done</span>';
    });
  }

  /* ---------- how it works: the statement nearest the middle drives the stage ---------- */
  var steps = [].slice.call(document.querySelectorAll(".how-step"));
  var cards = [].slice.call(document.querySelectorAll(".stage-card"));
  function pick() {
    var mid = innerHeight / 2, best = 0, bd = 1e9;
    steps.forEach(function (s, i) {
      var r = s.getBoundingClientRect(), d = Math.abs(r.top + r.height / 2 - mid);
      if (d < bd) { bd = d; best = i; }
    });
    steps.forEach(function (s, i) { s.classList.toggle("is-on", i === best); });
    cards.forEach(function (c, i) { c.classList.toggle("is-on", i === best); });
  }
  if (steps.length) { window.addEventListener("scroll", pick, { passive: true }); pick(); }

  /* ---------- the dose engine: app/dosing.py, line for line ---------- */
  var TARGET = { fc: 4.0, ph: 7.5, ta: 90, cya: 50 };
  var BAND = { fc: 0.5, ph: 0.3, ta: 10, cya: 10 };
  var SCALE = { fc: [0, 8], ph: [6.8, 8.2], ta: [40, 160], cya: [0, 100] };
  var DEC = { fc: 1, ph: 1, ta: 0, cya: 0 };
  // Python's round(): halves go to the even neighbour.
  function pyRound(x) {
    var f = Math.floor(x), d = x - f;
    if (d === 0.5) return f % 2 === 0 ? f : f + 1;
    return Math.round(x);
  }
  function roundAmount(amount, inc) { return Math.round(pyRound(amount / inc) * inc * 100) / 100; }
  function fmt(n, d) { return n.toFixed(d); }
  function recommend(r, gallons) {
    var out = [], g10k = gallons / 10000;
    if (r.fc != null && r.fc < TARGET.fc - BAND.fc) {
      var pure = (TARGET.fc - r.fc) * gallons * 8.34 / 1e6;      // lb of chlorine
      var gal = roundAmount(pure / 0.125 / 10, 0.1);               // 12.5% liquid, 10 lb/gal
      if (gal > 0) out.push({ name: "Liquid chlorine", amt: fmt(gal, 1) + " gal" });
    }
    if (r.ph != null && r.ph > TARGET.ph + BAND.ph) {
      var taf = r.ta != null ? r.ta / 100 : 1;
      var floz = ((r.ph - TARGET.ph) / 0.2) * 26 * g10k * taf;    // 31.45% muriatic
      var cap = 32 * g10k, note = "";
      if (floz > cap) { floz = cap; note = "Capped: re-test before adding more"; }
      var g = roundAmount(floz / 128, 0.1);
      if (g > 0) out.push({ name: "Muriatic acid", amt: fmt(g, 1) + " gal", note: note });
    } else if (r.ph != null && r.ph < TARGET.ph - BAND.ph) {
      var oz = ((TARGET.ph - r.ph) / 0.2) * 6 * g10k;              // soda ash
      var lb = roundAmount(oz / 16, 0.25);
      if (lb > 0) out.push({ name: "Soda ash", amt: String(+lb.toFixed(2)) + " lb" });
    }
    if (r.ta != null && r.ta < TARGET.ta - BAND.ta) {
      var b = roundAmount(((TARGET.ta - r.ta) / 10) * 1.5 * g10k, 0.25);
      if (b > 0) out.push({ name: "Sodium bicarbonate", amt: String(+b.toFixed(2)) + " lb" });
    }
    if (r.cya != null) {
      if (r.cya > 100) out.push({ name: "Stabilizer", amt: "", note: "CYA high: partial drain or dilution recommended" });
      else if (r.cya < TARGET.cya - BAND.cya) {
        var s = roundAmount((TARGET.cya - r.cya) * g10k / 12, 0.25);
        if (s > 0) out.push({ name: "Stabilizer (CYA)", amt: String(+s.toFixed(2)) + " lb" });
      }
    }
    return out;
  }
  var form = document.querySelector("[data-calc]");
  if (form) {
    var galIn = form.querySelector("#c-gal");
    var list = form.querySelector("[data-doses]");
    var rows = [].slice.call(form.querySelectorAll(".rd"));
    function num(el) { var v = parseFloat(el.value); return isFinite(v) && v >= 0 ? v : null; }
    function pc(k, v) { var s = SCALE[k]; return Math.max(0, Math.min(100, (v - s[0]) / (s[1] - s[0]) * 100)); }
    function update() {
      var r = {};
      rows.forEach(function (row) {
        var k = row.dataset.key, v = num(row.querySelector("input")); r[k] = v;
        var lo = TARGET[k] - BAND[k], hi = TARGET[k] + BAND[k];
        var band = row.querySelector(".band"), mk = row.querySelector(".mk"), st = row.querySelector(".st");
        band.style.left = pc(k, lo) + "%"; band.style.width = (pc(k, hi) - pc(k, lo)) + "%";
        var range = fmt(lo, DEC[k]) + "–" + fmt(hi, DEC[k]);
        if (v == null) { mk.style.display = "none"; st.className = "st"; st.textContent = "Not tested · target " + range; return; }
        mk.style.display = ""; mk.style.left = pc(k, v) + "%";
        if (v < lo) { st.className = "st low"; st.textContent = "↓ Low · target " + range; }
        else if (v > hi) { st.className = "st high"; st.textContent = "↑ High · target " + range; }
        else { st.className = "st ok"; st.textContent = "In range · " + range; }
      });
      var gallons = num(galIn) || 0, doses = gallons > 0 ? recommend(r, gallons) : [];
      list.textContent = "";
      if (!doses.length) {
        var li = document.createElement("li"); li.className = "none";
        li.textContent = gallons > 0 ? "Nothing to add. Every reading is inside its band." : "Enter the pool size to get doses.";
        list.appendChild(li); return;
      }
      doses.forEach(function (d, i) {
        var li = document.createElement("li"); li.style.animationDelay = (i * 60) + "ms";
        var b = document.createElement("b"); b.textContent = d.name; li.appendChild(b);
        var s = document.createElement("span"); s.textContent = d.amt; li.appendChild(s);
        if (d.note) { var n = document.createElement("small"); n.textContent = d.note; li.appendChild(n); }
        list.appendChild(li);
      });
    }
    form.addEventListener("input", function (e) {
      if (e.target === galIn) form.querySelectorAll("[data-gal]").forEach(function (b) { b.classList.toggle("on", b.dataset.gal === galIn.value); });
      update();
    });
    form.querySelectorAll("[data-gal]").forEach(function (b) {
      b.addEventListener("click", function () {
        galIn.value = b.dataset.gal;
        form.querySelectorAll("[data-gal]").forEach(function (x) { x.classList.toggle("on", x === b); });
        update();
      });
    });
    update();
  }

  /* ---------- FAQ: questions on the left, the answer large on the right ---------- */
  var faq = document.querySelector("[data-faq]");
  var pane = document.querySelector("[data-pane]");
  if (faq && pane) {
    var qs = [].slice.call(faq.querySelectorAll(".q"));
    var wide = window.matchMedia("(min-width: 901px)");
    function select(q) {
      qs.forEach(function (x) {
        var on = x === q, a = document.getElementById(x.getAttribute("aria-controls"));
        x.classList.toggle("is-on", on); x.setAttribute("aria-expanded", on ? "true" : "false");
        if (wide.matches) a.hidden = !on; else a.hidden = !on;
      });
      var a = document.getElementById(q.getAttribute("aria-controls"));
      if (wide.matches) {
        pane.classList.add("is-fading");
        setTimeout(function () { pane.textContent = a.textContent.trim(); pane.classList.remove("is-fading"); }, reduce ? 0 : 150);
      }
    }
    qs.forEach(function (q) {
      q.addEventListener("click", function () { select(q); });
      q.addEventListener("mouseenter", function () { if (wide.matches) select(q); });
      q.addEventListener("focus", function () { if (wide.matches) select(q); });
    });
    select(qs[0]);
  }

  /* ---------- the clip rests under reduced motion and in hidden tabs ---------- */
  var vid = document.querySelector(".yours-clip video");
  if (vid) {
    if (reduce) { vid.removeAttribute("autoplay"); vid.pause(); }
    document.addEventListener("visibilitychange", function () { if (document.hidden) vid.pause(); else if (!reduce) vid.play().catch(function () {}); });
  }
})();
