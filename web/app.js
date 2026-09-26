// pitwall dashboard client — vanilla, no deps. Layout/lifecycle: docs/15-dashboard-design.md.
(function () {
  "use strict";
  var STALE_MS = 1000, CONNECTING_MS = 3000, MAX_CALLS = 12;
  var IDLE_S = { 1: 30, 2: 20, 3: 20 };
  var COMPOUNDS = { 16: "SOFT", 17: "MEDIUM", 18: "HARD", 7: "INTER", 8: "WET" };
  var SC_WORDS = { 1: "SAFETY CAR", 2: "VSC", 3: "FORMATION" };

  var ws = null, lastSeq = null, backoff = 500, mismatched = false;
  var calls = []; // {id, seq, t, priority, text, lap, audio}
  var clockOffset = 0; // server epoch seconds - local epoch seconds
  var startedAt = performance.now(), lastFrameAt = null, lastState = null, lastStateAt = null;
  var stateTimes = [], shownCurrentId = null, shownPreviousId = null;
  var pressTimer = null;
  // motion state: a class stays on for a fixed window so the 4 Hz re-render
  // never restarts or cancels a running animation (docs/15 §11).
  var until = {}, shownPage = null, shownPos = null, shownLap = null, tyreSt = {}, seenLog = null;
  var HIT_MS = 900, FLIP_MS = 700, SWAP_MS = 420;
  function hold(key, ms) { until[key] = performance.now() + ms; }
  function on(key) { return (until[key] || 0) > performance.now(); }
  function mark(key) { return on(key) ? " " + key.split(":")[0] : ""; }

  function el(id) { return document.getElementById(id); }
  function setText(id, txt) { var n = el(id); if (n) n.textContent = txt; }
  function setClass(id, cls) { var n = el(id); if (n) n.className = cls; }
  function fmt(n, digits) {
    if (n === null || n === undefined || isNaN(n)) return "--";
    return Number(n).toFixed(digits === undefined ? 1 : digits);
  }
  function serverNow() { return Date.now() / 1000 + clockOffset; }
  function ageS(c) { return c.t ? Math.max(0, serverNow() - c.t) : null; }
  function ageText(c) {
    var a = ageS(c);
    if (a === null) return "";
    return a < 60 ? Math.floor(a) + "s ago" : Math.floor(a / 60) + "m ago";
  }
  function heard(c) { return c.audio === "started" || c.audio === "interrupted"; }

  // -- freshness ------------------------------------------------------------

  function packetAgeMs() {
    if (!lastState || lastState.packet_age_ms === null) return null;
    return lastState.packet_age_ms + (performance.now() - lastStateAt);
  }

  function isStale() {
    if (mismatched) return true;
    if (lastFrameAt === null || performance.now() - lastFrameAt > STALE_MS) return true;
    if (!lastState || !lastState.live) return true;
    var age = packetAgeMs();
    return age === null || age > STALE_MS;
  }

  function renderFreshness() {
    var connecting = lastState === null && performance.now() - startedAt < CONNECTING_MS;
    var stale = !connecting && isStale();
    document.body.classList.toggle("connecting", connecting);
    document.body.classList.toggle("stale", stale);
    var age = packetAgeMs();
    if (connecting) {
      setText("live", "CONNECTING…");
      setText("age", "");
    } else if (stale) {
      setText("live", "STALE");
      setText("age", age === null ? "no telemetry" : "last packet " + fmt(age / 1000, 1) + " s ago");
    } else {
      setText("live", "LIVE");
      setText("age", "age " + fmt(age, 0) + " ms");
    }
    var ageEl = el("age");
    if (ageEl) ageEl.className = !stale && age !== null && age >= 500 ? "age-warn" : "dim";
    var now = performance.now();
    stateTimes = stateTimes.filter(function (t) { return now - t < 1000; });
    setText("rate", stale ? "" : stateTimes.length + " WS/s");
    return stale;
  }

  // -- telemetry zones --------------------------------------------------------

  function renderState(p) {
    var phase = (p.phase || "--").replace("_", " ").toUpperCase();
    setText("phase", phase);
    setClass("phase", p.phase === "out_lap" ? "amber" : "");
    setText("session", (p.session_kind || "--").toUpperCase() + " · " +
      String(p.track || "--").toUpperCase());
    setText("lap", "LAP " + (p.lap_num || "--") + (p.total_laps && p.session_kind === "race" ? "/" + p.total_laps : ""));
    setText("position", p.position ? "P" + p.position : "P--");
    if (p.position && shownPos && p.position !== shownPos) {
      hold(p.position < shownPos ? "gain:pos" : "loss:pos", 1600);
      delete until[p.position < shownPos ? "loss:pos" : "gain:pos"];
    }
    if (p.position) shownPos = p.position;
    setClass("position", mark("gain:pos") + mark("loss:pos"));
    if (p.lap_num && shownLap && p.lap_num !== shownLap) hold("tick:lap", 900);
    if (p.lap_num) shownLap = p.lap_num;
    setClass("lap", mark("tick:lap"));
    var m = el("mindset");
    if (m) {
      m.textContent = String(p.mindset || "--").toUpperCase();
      m.className = "pill" + (p.mindset === "aggressive" ? " aggr" : "");
    }
    var v = el("verbosity");
    if (v) {
      v.innerHTML = "";
      v.appendChild(document.createTextNode(p.verbosity || ""));
      if (p.silent) {
        var s = document.createElement("span");
        s.className = "silent";
        s.textContent = " RADIO SILENT";
        v.appendChild(s);
      }
      if (p.quiet) {
        var q = document.createElement("span");
        q.className = "quiet";
        q.textContent = " QUIET" + (p.quiet_left_s ? " " + clock(p.quiet_left_s) : "");
        v.appendChild(q);
      }
    }
    var flag = el("flag");
    if (flag) {
      var word = p.red_flag ? "RED FLAG" : p.paused ? "PAUSED" : "";
      flag.hidden = !word;
      flag.textContent = word;
      flag.className = "flag" + (p.red_flag ? " red" : "");
    }
    document.body.classList.toggle("redflag", !!p.red_flag);
    renderQuali(p.quali);
    renderCool(p.quali ? p.quali.cool : null, p.quali);
    renderPitBoard(p.pit_board, p.quali, p.phase);
    renderPage(p);
    renderStrategy(p.strategy, p.quali);
    renderBattle(p.strategy);
    renderCarPage(p, p.strategy);
    renderTrackPage(p.track_info);
    renderSetupPage(p.setup);

    var comp = COMPOUNDS[p.tyre_visual] || (p.tyre_visual ? "C" + p.tyre_visual : "--");
    var compEl = el("compound");
    if (compEl) {
      compEl.textContent = comp;
      compEl.className = "comp " + comp.toLowerCase();
    }
    setText("tyre-age", p.tyre_age_laps !== undefined ? p.tyre_age_laps + "L old" : "--");

    if (p.tyres) {
      ["fl", "fr", "rl", "rr"].forEach(function (k) {
        var t = p.tyres[k], c = el("tyre-" + k);
        if (!c || !t) return;
        var st = String(t.status || "").toLowerCase();
        if (tyreSt[k] && tyreSt[k] !== st) hold("flip:" + k, FLIP_MS);
        tyreSt[k] = st;
        c.className = "corner " + st + mark("flip:" + k);
        c.querySelector(".temp").textContent = fmt(t.inner, 0) + "°";
        c.querySelector(".word").textContent = t.status || "--";
        var det = c.querySelector(".det");
        det.innerHTML = "";
        det.appendChild(document.createTextNode("surf " + fmt(t.surface, 0) + "° · wear "));
        var w = document.createElement("span");
        w.textContent = fmt(t.wear, 0) + "%";
        if (t.wear >= 70) w.className = "wear-crit"; else if (t.wear >= 50) w.className = "wear-warn";
        det.appendChild(w);
        var brk = c.querySelector(".brk");
        if (brk && p.brakes) brk.textContent = "BRK " + fmt(p.brakes[k], 0) + "°";
      });
    }

    var toGo = p.total_laps && p.lap_num ? p.total_laps - p.lap_num + 1 : null;
    var fdl = p.strategy ? p.strategy.fuel_delta_laps : null;
    if (fdl !== null && fdl !== undefined) {
      // Backend owns the target (docs/15 open question 1): delta vs the flag.
      setText("fuel", (fdl >= 0 ? "+" : "") + fmt(fdl, 1) + " laps");
      setClass("fuel", "big" + (fdl < 0 ? " delta-crit" : fdl < 0.5 ? " delta-warn" : " delta-ok"));
      setText("fuel-sub", (fdl >= 0 ? "spare" : "SHORT") + " vs flag · " +
        fmt(p.fuel_remaining_laps, 1) + " laps in tank");
    } else {
      setText("fuel", fmt(p.fuel_remaining_laps, 1) + " laps");
      setClass("fuel", "big");
      setText("fuel-sub", toGo !== null && (p.session_kind === "race")
        ? "left · " + toGo + " to finish" : "laps of fuel left");
    }

    renderDamage(p.damage);

    setText("ers", "ERS " + fmt(p.ers_pct, 0) + "%");
    var sc = p.safety_car || 0;
    setText("sc", sc ? (SC_WORDS[sc] || "SC " + sc) : "SC 0");
    document.body.classList.toggle("sc", !!sc);
    if (p.latency) {
      setText("latency", "call p99 " + fmt(p.latency.trigger_to_speak_p99_ms, 0) +
        " ms · ws p99 " + fmt(p.latency.packet_to_ws_p99_ms, 0) + " ms");
    }
  }


  // -- M3 race: zone F strategy + pages ------------------------------------

  function gapText(g) { return g === null || g === undefined ? "--" : (g >= 0 ? "+" : "") + fmt(g, 1); }
  function trendText(t, side) {
    // t = gap shrinking per lap (s). Ahead shrinking = we're closing;
    // behind shrinking = we're being caught. Colour always paired with a word.
    if (!t) return null;
    var closing = t > 0;
    var word = side === "ahead" ? (closing ? "closing" : "dropping") : (closing ? "being caught" : "pulling away");
    var cls = side === "behind" && closing ? "warn" : side === "ahead" && closing ? "ok" : "";
    return { text: (closing ? "▲ +" : "▼ ") + fmt(t, 1) + "/lap " + word, cls: cls };
  }
  function span(txt, cls) {
    var n = document.createElement("span");
    n.textContent = txt;
    if (cls) n.className = cls;
    return n;
  }
  function rivalRow(id, r, side, s) {
    var n = el(id);
    if (!n) return;
    n.innerHTML = "";
    n.appendChild(span(side === "ahead" ? "AHEAD" : "BEHIND", "lbl"));
    if (!r) { n.appendChild(span("clear", "dim")); return; }
    var gap = side === "ahead" ? r.gap_s : (r.gap_s === null ? null : -r.gap_s);
    n.appendChild(document.createTextNode((r.pos ? "P" + r.pos + " " : "") +
      String(r.name || "--").toUpperCase() + " " + gapText(gap) + " " + (r.compound || "")));
    if (r.drs) n.appendChild(span(" · DRS", side === "behind" ? "crit" : "ok"));
    if (side === "ahead" && s.undercut_s > 0) n.appendChild(span(" · UC +" + fmt(s.undercut_s, 1), "ok"));
    if (side === "behind" && s.overcut_s > 0) n.appendChild(span(" · OC +" + fmt(s.overcut_s, 1), "ok"));
    if (r.pitted) n.appendChild(span(" · PITTED", "warn"));
    var tr = trendText(r.gap_trend_s, side);
    if (tr) n.appendChild(span(" · " + tr.text, tr.cls));
  }

  function renderStrategy(s, q) {
    var box = el("strategy"), ph = el("strat-ph");
    if (!box) return;
    box.hidden = !s || !!q;
    if (ph && !q) ph.hidden = !!s;
    if (!s || q) return;
    setText("strat-title", "STRATEGY");
    var w = el("s-window");
    if (s.pit_window) {
      var lap = lastState ? lastState.lap_num : 0;
      w.textContent = "PIT WINDOW L" + s.pit_window.start + "–" + s.pit_window.end +
        (s.plan ? " · " + String(s.plan.kind).toUpperCase().replace("_", " ") : "");
      w.className = "s-window" + (lap >= s.pit_window.start ? " box" :
        lap >= s.pit_window.start - 2 ? " soon" : "");
    } else if (s.plan && s.plan.kind) {
      w.textContent = String(s.plan.kind).toUpperCase().replace("_", " ") +
        (s.plan.lap ? " L" + s.plan.lap : "");
      w.className = "s-window";
    } else {
      w.textContent = s.laps_remaining ? "NO STOP · " + s.laps_remaining + " to go" : "NO STOP";
      w.className = "s-window";
    }
    rivalRow("s-ahead", s.ahead, "ahead", s);
    rivalRow("s-behind", s.behind, "behind", s);
    setText("s-stint", s.stint_plan + (s.restricted ? " · rival data restricted" : ""));
  }

  function battleCard(id, r, side, s) {
    var n = el(id);
    if (!n) return;
    n.hidden = !r;
    if (!r) return;
    // The gap rail persists across renders so its marker glides between gaps.
    var rail = n.querySelector(".rail");
    if (!rail) {
      rail = document.createElement("div");
      rail.className = "rail";
      rail.appendChild(span("DRS", "rz"));
      rail.appendChild(span("", "rc"));
    }
    while (n.firstChild) n.removeChild(n.firstChild);
    var gap = side === "ahead" ? r.gap_s : (r.gap_s === null ? null : -r.gap_s);
    var head = span((side === "ahead" ? "AHEAD " : "BEHIND ") + (r.pos ? "P" + r.pos + " " : "") +
      String(r.name || "--").toUpperCase() + " " + gapText(gap), "b-head");
    n.appendChild(head);
    n.appendChild(rail);
    var abs = r.gap_s === null || r.gap_s === undefined ? null : Math.abs(r.gap_s);
    rail.hidden = abs === null;
    if (abs !== null) {
      rail.style.setProperty("--g", String(Math.min(abs, 3) / 3));
      rail.className = "rail " + side + (abs <= 1 ? " in" : "");
    }
    function kv(k, v, cls) { n.appendChild(span(k, "k")); n.appendChild(span(v, cls)); }
    kv("tyre", (r.compound || "--") + " · " + (r.tyre_age || 0) + "L");
    kv("pace", r.pace_delta_s === null || r.pace_delta_s === undefined ? "--" :
      (r.pace_delta_s > 0 ? "+" + fmt(r.pace_delta_s, 2) + " s/lap slower" : fmt(-r.pace_delta_s, 2) + " s/lap faster"),
      r.pace_delta_s < 0 ? "warn" : "");
    var tr = trendText(r.gap_trend_s, side);
    kv("trend", tr ? tr.text : "steady", tr ? tr.cls : "");
    var threat = [];
    if (r.drs) threat.push("DRS");
    if (side === "ahead" && s.undercut_s > 0) threat.push("UNDERCUT +" + fmt(s.undercut_s, 1));
    if (side === "behind" && s.overcut_s > 0) threat.push("OVERCUT +" + fmt(s.overcut_s, 1));
    if (r.pitted) threat.push("PITTED");
    kv("threat", threat.length ? threat.join(" · ") : "none", threat.length ? (side === "behind" ? "crit" : "ok") : "");
    n.className = "b-card" + (r.drs ? " drs" : threat.length ? " threat" : "");
  }

  function renderBattle(s) {
    var any = s && (s.ahead || s.behind);
    var ph = el("b-ph");
    if (ph) ph.hidden = !!any;
    battleCard("b-ahead", s ? s.ahead : null, "ahead", s || {});
    battleCard("b-behind", s ? s.behind : null, "behind", s || {});
    if (!s) { setText("b-exit", "--"); setText("b-plan", "--"); return; }
    var pe = s.pit_exit || {};
    setText("b-exit", "PIT EXIT " + (pe.clean ? "CLEAR AIR" : "TRAFFIC") +
      (pe.rival ? " · " + String(pe.rival.name || "").toUpperCase() + " " + gapText(pe.rival.gap_s) : "") +
      (s.pit_loss_s ? " · loss " + fmt(s.pit_loss_s, 1) + " s (" + (s.pit_loss_source || "prior") + ")" : ""));
    setText("b-plan", s.plan ? String(s.plan.kind).toUpperCase() + " · " + (s.plan.reason || "") +
      " · conf " + fmt(100 * (s.plan.confidence || 0), 0) + "%" : s.stint_plan);
  }

  function renderCarPage(p, s) {
    var e = s ? s.energy : null;
    if (e && e.per_lap_mj) {
      setText("cp-energy", (e.lap_delta_mj >= 0 ? "+" : "") + fmt(e.lap_delta_mj, 2) + " MJ");
      setText("cp-energy-sub", fmt(e.per_lap_mj, 2) + " MJ/lap budget" +
        (e.laps_to_floor !== null ? " · floor in " + fmt(e.laps_to_floor, 1) + " laps" : "") +
        (e.mode ? " · " + e.mode : ""));
    } else {
      setText("cp-energy", "ERS " + fmt(p.ers_pct, 0) + "%");
      setText("cp-energy-sub", "no energy budget yet");
    }
    var fd = s ? s.fuel_delta_laps : null;
    setText("cp-fuel", fd === null || fd === undefined ? fmt(p.fuel_remaining_laps, 1) + " laps" :
      (fd >= 0 ? "+" : "") + fmt(fd, 1) + " laps");
    setClass("cp-fuel", "big" + (fd === null || fd === undefined ? "" : fd < 0 ? " delta-crit" : fd < 0.5 ? " delta-warn" : " delta-ok"));
    setText("cp-fuel-sub", fd === null || fd === undefined ? "laps of fuel left" :
      (fd >= 0 ? "spare vs flag" : "SHORT vs flag — lift and coast"));
    setText("cp-life", s && s.laps_of_pace !== null ? fmt(s.laps_of_pace, 0) + " laps" : "--");
    setText("cp-life-sub", s ? "of pace left · " + (s.laps_remaining || 0) + " to go" +
      (s.tyres && s.tyres.wear_per_lap_pct ? " · " + fmt(s.tyres.wear_per_lap_pct, 1) + "%/lap wear" : "") : "deg model needs race laps");
    var f = [];
    if (s && s.tyres) {
      if (s.tyres.overheat) f.push("OVERHEATING");
      if (s.tyres.graining) f.push("GRAINING");
      if (s.tyres.blister_max_pct) f.push("BLISTER " + s.tyres.blister_max_pct + "%");
    }
    setText("cp-flags", f.length ? f.join(" · ") : "tyres nominal");
    meter("cp-energy-bar", p.ers_pct === null || p.ers_pct === undefined ? null : p.ers_pct / 100);
    var lop = s ? s.laps_of_pace : null, togo = s ? s.laps_remaining : 0;
    meter("cp-life-bar", lop === null || lop === undefined || !togo ? null : Math.min(1, lop / togo),
      lop !== null && lop !== undefined && togo && lop < togo ? "short" : "");
    setClass("cp-flags", "cp-flags" + (f.length ? " warn" : " dim"));
  }

  function meter(id, frac, cls) {
    var n = el(id);
    if (!n) return;
    n.parentNode.hidden = frac === null;
    if (frac === null) return;
    n.style.setProperty("--f", String(Math.max(0, Math.min(1, frac))));
    n.className = cls || "";
  }

  function tpRow(id, txt, cls) { setText(id, txt); setClass(id, "tp-row" + (cls ? " " + cls : "")); }
  function renderTrackPage(t) {
    if (!t) return;
    var st = el("tp-status");
    if (st) {
      st.textContent = t.red_flag ? "RED FLAG" : t.safety_car ? (SC_WORDS[t.safety_car] || "SC") +
        (t.sc_laps ? " · " + t.sc_laps + " laps" : "") : String(t.phase || "--").replace("_", " ").toUpperCase();
      st.className = "tp-status" + (t.red_flag ? " red" : t.safety_car ? " sc" : "");
    }
    var r = t.rain_pct || [0, 0, 0];
    tpRow("tp-weather", "RAIN now " + r[0] + "% · 10 min " + r[1] + "% · 30 min " + r[2] + "%" +
      (t.weather_crossover ? " · CROSSOVER " + String(t.weather_crossover).toUpperCase() : ""),
      t.weather_crossover ? "warn" : "");
    tpRow("tp-flags", t.blue_flag ? "BLUE FLAG · let the leader by" : "no blue flag", t.blue_flag ? "warn" : "");
    tpRow("tp-pens", "PENALTY " + (t.penalty_s || 0) + " s · warnings " + (t.warnings || 0) +
      " · cuts " + (t.corner_cut_warnings || 0) + (t.unserved ? " · " + t.unserved + " UNSERVED" : ""),
      t.unserved ? "crit" : t.penalty_s ? "warn" : "");
    tpRow("tp-traffic", "GAPS ahead " + gapText(t.gap_ahead_s) + " · behind " +
      gapText(t.gap_behind_s === null ? null : -t.gap_behind_s) + " · pit exit " +
      (t.pit_exit_clean ? "clear" : "traffic"), t.pit_exit_clean ? "" : "warn");
  }

  function renderSetupPage(su) {
    var ol = el("sp-list");
    if (!ol) return;
    ol.innerHTML = "";
    if (!su) { var d = document.createElement("li"); d.className = "dim"; d.textContent = "no setup packet yet"; ol.appendChild(d); return; }
    Object.keys(su.values).forEach(function (k) {
      var li = document.createElement("li");
      li.appendChild(span(k.replace(/_/g, " "), "k"));
      li.appendChild(span(fmt(su.values[k], 1)));
      ol.appendChild(li);
    });
    ["fl", "fr", "rl", "rr"].forEach(function (k) {
      if (!su.pressures[k]) return;
      var li = document.createElement("li");
      li.appendChild(span(k.toUpperCase() + " pressure", "k"));
      li.appendChild(span(fmt(su.pressures[k], 1) + " psi"));
      ol.appendChild(li);
    });
  }

  function renderPage(p) {
    var page = p.page || "race", pages = p.pages || ["race"];
    if (shownPage !== null && page !== shownPage) {
      var from = pages.indexOf(shownPage), to = pages.indexOf(page);
      var back = from >= 0 && to >= 0 && (to === from - 1 || (from === 0 && to === pages.length - 1));
      document.body.style.setProperty("--swap-dx", back ? "-1.6rem" : "1.6rem");
      hold("swap", SWAP_MS);
    }
    shownPage = page;
    pages.concat(["race"]).forEach(function (n) {
      document.body.classList.toggle("page-" + n, n === page);
    });
    document.body.classList.toggle("swap", on("swap"));
    var pe = el("page");
    if (pe && pe.dataset.key !== page + "|" + pages.join()) {
      pe.dataset.key = page + "|" + pages.join();
      pe.innerHTML = "";
      pe.appendChild(span(page.toUpperCase(), "pg-name"));
      var dots = span("", "pg-dots");
      pages.forEach(function (n) { dots.appendChild(span("", n === page ? "on" : "")); });
      pe.appendChild(dots);
      if (on("swap")) restart(pe, "pg-flip");
    }
  }

  function sendCtl(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }

  function clock(s) {
    if (s === null || s === undefined || isNaN(s)) return "--:--";
    s = Math.max(0, Math.floor(s));
    return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2);
  }
  function lapTime(ms) {
    if (!ms) return "--";
    var s = ms / 1000, m = Math.floor(s / 60);
    return m + ":" + ("0" + (s - m * 60).toFixed(3)).slice(-6);
  }
  function signed(ms) {
    if (ms === null || ms === undefined) return "--";
    return (ms > 0 ? "+" : ms < 0 ? "−" : "±") + (Math.abs(ms) / 1000).toFixed(3);
  }

  // Zone F in qualifying (docs/15 §8): release window in the garage, lap vs cut-off
  // while flying. Race/practice keep the M3 placeholder.
  function renderQuali(q) {
    var box = el("quali"), ph = el("strat-ph");
    if (!box) return;
    box.hidden = !q;
    if (ph) ph.hidden = !!q;
    setText("strat-title", q ? "QUALIFYING" : "STRATEGY");
    if (!q) return;
    var main = el("q-main");
    var sub = [clock(q.session_time_left) + " left", q.fresh_sets + " fresh set" +
      (q.fresh_sets === 1 ? "" : "s"), "best " + lapTime(q.best_lap_ms),
      "cut " + lapTime(q.cutoff_ms)];
    if (q.release) {
      var r = q.release;
      if (r.clean) {
        main.textContent = "RELEASE NOW" +
          (r.gap_ahead_s !== null ? " · clear " + fmt(r.gap_ahead_s, 0) + " s ahead" : "");
        main.className = "qmain ok";
      } else if (r.wait_s !== null) {
        main.textContent = "release in " + fmt(r.wait_s, 0) + " s";
        main.className = "qmain warn";
      } else {
        main.textContent = "TRAFFIC · no gap in 60 s";
        main.className = "qmain crit";
      }
      sub.unshift(r.cars_on_track + " on track");
    } else if (q.lap) {
      var d = q.lap.delta_ms;
      main.textContent = "proj " + lapTime(q.lap.projected_ms) + " · " +
        (d === null ? "no cut-off" : signed(d) + " to cut") +
        (q.lap.abort ? " · ABORT" : "");
      main.className = "qmain " + (q.lap.abort ? "crit" : d !== null && d <= 0 ? "ok" : "warn");
    } else {
      main.textContent = q.through ? "THROUGH" : "--";
      main.className = "qmain" + (q.through ? " ok" : "");
    }
    if (q.through) sub.push("THROUGH");
    if (q.margin_ms !== null && q.margin_ms !== undefined) {
      sub.push((q.margin_kind === "pole" ? "vs P2 " : "vs cut ") + signed(-q.margin_ms));
    }
    setText("q-sub", sub.join(" · "));
    var press = el("q-press");
    if (press) {
      press.hidden = !q.pressure;
      if (q.pressure) {
        press.textContent = "PRESSURES " + (q.pressure.length ? q.pressure.map(function (p) {
          return p.corner.toUpperCase() + " " + (p.delta_psi > 0 ? "+" : "") +
            p.delta_psi.toFixed(1) + (p.target_psi ? " → " + p.target_psi.toFixed(1) : "") +
            " (" + p.size + ", " + Math.round(p.avg_c) + "°)";
        }).join(" · ") : "in window");
      }
    }
  }


  // Pit board (garage / pitting): release light, per-corner pressure target,
  // next-run summary and the car setup in game-menu order.
  var SETUP_GROUPS = [
    ["AERODYNAMICS", [["front_wing", "F wing", 0], ["rear_wing", "R wing", 0]]],
    ["TRANSMISSION", [["on_throttle", "on-thr", 0, "%"], ["off_throttle", "off-thr", 0, "%"],
      ["engine_braking", "eng brk", 0, "%"]]],
    ["SUSP. GEOMETRY", [["front_camber", "F camber", 2, "°"], ["rear_camber", "R camber", 2, "°"],
      ["front_toe", "F toe", 2, "°"], ["rear_toe", "R toe", 2, "°"]]],
    ["SUSPENSION", [["front_suspension", "F spr", 0], ["rear_suspension", "R spr", 0],
      ["front_anti_roll_bar", "F arb", 0], ["rear_anti_roll_bar", "R arb", 0],
      ["front_suspension_height", "F ride", 0], ["rear_suspension_height", "R ride", 0]]],
    ["BRAKES", [["brake_pressure", "pressure", 0, "%"], ["brake_bias", "bias", 0, "%"]]],
    ["TYRES", null],
    ["FUEL", [["fuel_load", "load", 1, " kg"]]]
  ];
  var CORNERS = ["fl", "fr", "rl", "rr"];

  function releaseState(q, phase) {
    var r = q && q.release;
    if (!r) return { text: phase === "pitting" ? "PITTING" : "IN GARAGE", cls: "", sub: "" };
    var sub = [r.cars_on_track + " on track"];
    if (r.gap_ahead_s !== null) sub.push(fmt(r.gap_ahead_s, 0) + " s clear ahead");
    if (r.gap_behind_s !== null) sub.push(fmt(r.gap_behind_s, 0) + " s behind");
    if (r.time_for_out_lap === false) {
      return { text: "STAY IN · NO TIME FOR A LAP", cls: "crit", sub: sub.join(" · ") };
    }
    if (r.clean) return { text: "GO · TRACK CLEAR", cls: "ok", sub: sub.join(" · ") };
    if (r.wait_s !== null) return { text: "HOLD · " + fmt(r.wait_s, 0) + " s", cls: "warn", sub: sub.join(" · ") };
    return { text: "HOLD · TRAFFIC", cls: "crit", sub: "no gap in 60 s · " + sub.join(" · ") };
  }

  function renderCorner(k, t) {
    var node = el("pb-" + k);
    if (!node) return;
    var move = node.querySelector(".pb-move"), psi = node.querySelector(".pb-psi"),
      det = node.querySelector(".pb-det");
    var now = t.psi !== null ? fmt(t.psi, 1) : "--", cls = "none", word = "--", psiTxt = now;
    if (t.limited) {
      cls = "limit"; word = t.edge === "min" ? "AT MIN" : "AT MAX";
    } else if (t.target_psi !== null && t.delta_psi) {
      cls = t.applied ? "set" : t.delta_psi > 0 ? "up" : "down";
      word = t.applied ? "SET ✓" : (t.delta_psi > 0 ? "▲ +" : "▼ −") + fmt(Math.abs(t.delta_psi), 1);
      psiTxt = t.applied ? now + " psi" : now + " → " + fmt(t.target_psi, 1);
    } else if (t.avg_c !== null) {
      cls = "hold"; word = "HOLD";
    }
    node.className = "pb-corner " + cls;
    move.textContent = word;
    psi.textContent = psiTxt;
    det.textContent = t.avg_c !== null ? "run avg " + Math.round(t.avg_c) + "°" : "no run data";
  }

  function setupRow(list, group, value, state, tag) {
    var li = document.createElement("li");
    if (state) li.className = state;
    var box = document.createElement("span");
    box.className = "box"; box.textContent = state === "todo" ? "☐" : state === "done" ? "☑" : "·";
    var g = document.createElement("span");
    g.className = "grp"; g.textContent = group;
    var v = document.createElement("span");
    v.textContent = value;
    var t = document.createElement("span");
    t.className = "tag"; t.textContent = tag;
    [box, g, v, t].forEach(function (n) { li.appendChild(n); });
    list.appendChild(li);
  }

  function renderSetup(b) {
    var list = el("pb-setup");
    if (!list) return;
    list.innerHTML = "";
    if (!b.setup) {
      var e = document.createElement("li");
      e.className = "dim"; e.textContent = "no setup packet yet";
      list.appendChild(e);
      return;
    }
    SETUP_GROUPS.forEach(function (g) {
      if (g[1] === null) {
        var todo = CORNERS.filter(function (k) {
          var t = b.tyres[k];
          return t.target_psi !== null && t.delta_psi && !t.limited;
        });
        var left = todo.filter(function (k) { return !b.tyres[k].applied; });
        var value = CORNERS.map(function (k) {
          var t = b.tyres[k];
          return k.toUpperCase() + " " + fmt(t.psi, 1) +
            (todo.indexOf(k) >= 0 && !t.applied ? "→" + fmt(t.target_psi, 1) : "");
        }).join("  ");
        setupRow(list, g[0], value, todo.length ? (left.length ? "todo" : "done") : "",
          todo.length ? (left.length ? left.length + " to change" : "changed") : "no change");
        return;
      }
      var parts = g[1].map(function (f) {
        var v = b.setup[f[0]];
        return f[1] + " " + (v === undefined ? "--" : fmt(v, f[2]) + (f[3] || ""));
      });
      setupRow(list, g[0], parts.join("  "), "", "no change");
    });
  }

  function renderPitBoard(b, q, phase) {
    var box = el("pitboard");
    if (!box) return;
    box.hidden = !b;
    document.body.classList.toggle("pit", !!b);
    if (!b) return;
    var rel = releaseState(q, phase);
    setText("pb-release", rel.text);
    setClass("pb-release", "pb-release " + rel.cls);
    setText("pb-release-sub", rel.sub || (b.has_advice ? "" : "no run data yet"));
    setText("pb-press-note", b.has_advice ? "" : "· no flying laps this run");
    CORNERS.forEach(function (k) { renderCorner(k, b.tyres[k]); });
    var plan = q && q.plan;
    setText("pb-plan", plan ? "PLAN " + plan.plan.toUpperCase() +
      (plan.reason ? " · " + plan.reason : "") : "PLAN --");
    var fuelOk = b.fuel_laps >= b.fuel_need_laps;
    setText("pb-fuel", "FUEL " + fmt(b.fuel_laps, 1) + " laps · need " + fmt(b.fuel_need_laps, 1));
    setClass("pb-fuel", "pb-row " + (fuelOk ? "ok" : "crit"));
    setText("pb-ers", "BATTERY " + fmt(b.ers_pct, 0) + "% · want " + fmt(b.ers_need_pct, 0) + "%");
    setClass("pb-ers", "pb-row " + (b.ers_pct >= b.ers_need_pct ? "ok" : "warn"));
    setText("pb-time", q ? clock(q.session_time_left) + " left · " + q.fresh_sets + " fresh set" +
      (q.fresh_sets === 1 ? "" : "s") : "--");
    var pole = el("pb-pole");
    if (pole) {
      pole.hidden = !q;
      if (q) {
        pole.textContent = "BEST " + lapTime(q.best_lap_ms) +
          (q.margin_ms !== null && q.margin_ms !== undefined ?
            (q.margin_kind === "pole" ? " · vs P2 " : " · vs cut ") + signed(-q.margin_ms) : "") +
          (q.through ? " · THROUGH" : "");
        pole.className = "pb-row " + (q.through || (q.margin_ms > 0) ? "ok" : "");
      }
    }
    renderSetup(b);
  }

  // Cool-down lap (docs/17): shown only while the payload carries `cool`; the
  // server drops it at the hot-lap-mode point so the normal layout returns.
  var PLAN_WORDS = { battery: "battery low", tyres: "tyres hot" };
  function renderCool(c, q) {
    var box = el("cool");
    if (!box) return;
    box.hidden = !c;
    document.body.classList.toggle("cool", !!c);
    if (!c) return;
    var minPct = c.ers_min_pct, ready = c.ers_pct >= minPct;
    var ersTile = el("c-ers-tile");
    if (ersTile) ersTile.className = "c-tile " + (ready ? "ok" : "warn");
    setText("c-ers", fmt(c.ers_pct, 0) + "%");
    var bar = el("c-ers-bar");
    if (bar) bar.style.width = Math.max(0, Math.min(100, c.ers_pct)) + "%";
    var mark = el("c-ers-min");
    if (mark) mark.style.left = minPct + "%";
    var mode = el("c-mode");
    if (mode) {
      mode.textContent = (c.recharging ? "RECHARGE" : "NOT IN RECHARGE · mode " + c.ers_mode) +
        " · target " + fmt(minPct, 0) + "%";
      mode.className = "sub" + (c.recharging ? "" : " c-warn");
    }
    var hotTile = el("c-hot-tile"), d = c.dist_to_hot_m;
    setText("c-hot", d === null ? "--" : d >= 1000 ? fmt(d / 1000, 1) + " km" : fmt(d, 0) + " m");
    if (hotTile) hotTile.className = "c-tile" + (d !== null && d < 300 ? " warn" : "");
    setText("c-plan", c.extend ? "ONE MORE COOL LAP" : c.plan_reason ?
      "cooling: " + (PLAN_WORDS[c.plan_reason] || c.plan_reason) : "--");
    var behind = el("c-behind");
    if (behind) {
      var s = c.car_behind_s;
      behind.className = "c-alert " + (s === null ? "clear" : s <= 3 ? "crit" : "warn");
      behind.textContent = s === null ? "NO HOT LAP BEHIND" :
        "HOT LAP BEHIND " + fmt(s, 1) + " s — OFF THE LINE";
    }
    var low = c.window_c[0], high = c.window_c[1];
    ["fl", "fr", "rl", "rr"].forEach(function (k) {
      var node = el("c-" + k), v = c.tyres[k];
      if (!node) return;
      node.textContent = k.toUpperCase() + " " + fmt(v, 0) + "°";
      node.className = v > high ? "hot" : v < low ? "cold" : "ok";
    });
    setText("c-hint", c.tyre_hint || "--");
    var pole = el("c-pole");
    if (pole) {
      pole.innerHTML = "";
      if (c.pole) {
        var g = c.pole.sector_gaps_ms, worst = g.indexOf(Math.max.apply(null, g));
        pole.appendChild(document.createTextNode("POLE " + (c.pole.driver || "") + " " +
          signed(-c.pole.gap_ms).replace("−", "-") + " · "));
        g.forEach(function (ms, i) {
          var s = document.createElement("span");
          s.textContent = "S" + (i + 1) + " " + (ms ? signed(ms) : "--") + " ";
          if (i === worst && ms > 0) s.className = "worst";
          pole.appendChild(s);
        });
      } else {
        pole.textContent = "POLE --";
      }
    }
    setText("c-mis", "LAST LAP " + (c.last_hot_ms ? lapTime(c.last_hot_ms) : "--") + " · " +
      (c.mistakes || "clean"));
    setText("c-sub", (q ? clock(q.session_time_left) + " left · " + q.fresh_sets + " fresh · " : "") +
      "fuel " + fmt(c.fuel_laps, 1) + " laps");
  }

  function renderDamage(d) {
    var box = el("damage");
    if (!box || !d) return;
    var parts = [
      ["front_left_wing", "FW L"], ["front_right_wing", "FW R"], ["rear_wing", "RW"],
      ["floor", "Floor"], ["diffuser", "Diffuser"], ["sidepod", "Sidepod"],
      ["gearbox", "Gearbox"], ["engine", "Engine"],
    ];
    box.innerHTML = "";
    parts.forEach(function (pair) {
      var val = d[pair[0]];
      if (!(val > 0)) return;
      var row = document.createElement("div");
      row.appendChild(document.createTextNode(pair[1] + " "));
      var s = document.createElement("span");
      s.textContent = val + "%";
      if (val >= 30) s.className = "crit"; else if (val >= 10) s.className = "warn";
      row.appendChild(s);
      box.appendChild(row);
    });
    [["drs_fault", "DRS fault"], ["ers_fault", "ERS fault"]].forEach(function (pair) {
      if (!d[pair[0]]) return;
      var row = document.createElement("div");
      row.className = "crit"; row.textContent = pair[1];
      box.appendChild(row);
    });
    if (!box.childNodes.length) {
      var n = document.createElement("span");
      n.className = "none"; n.textContent = "none";
      box.appendChild(n);
    }
  }

  // -- radio: current / previous / log ---------------------------------------

  function pickRadio(stale) {
    var live = calls.filter(function (c) { return c.audio !== "dropped"; });
    var latest = live.length ? live[live.length - 1] : null;
    var current = null, previous = null;
    if (stale) {
      for (var i = live.length - 1; i >= 0; i--) if (heard(live[i])) { previous = live[i]; break; }
      return { current: null, previous: previous };
    }
    if (latest) {
      var a = ageS(latest);
      if (a === null || a < (IDLE_S[latest.priority] || 20)) current = latest;
    }
    if (current) {
      for (var j = live.indexOf(current) - 1; j >= 0; j--) {
        if (heard(live[j])) { previous = live[j]; break; }
      }
    }
    return { current: current, previous: previous };
  }

  function audioLabel(c) {
    switch (c.audio) {
      case "started": return "▶ started";
      case "interrupted": return "interrupted";
      case "dropped": return "dropped";
      default: return "awaiting audio";
    }
  }

  function restart(node, cls) {
    node.classList.remove(cls);
    void node.offsetWidth;
    node.classList.add(cls);
  }

  function renderRadio(stale) {
    var pick = pickRadio(stale);
    var cur = pick.current, prev = pick.previous;
    var banner = el("banner"), text = el("call-text"), meta = el("call-meta"),
      prevEl = el("previous"), evid = el("call-evidence");
    if (banner && text && meta) {
      meta.innerHTML = "";
      if (mismatched) {
        banner.className = "banner mismatch";
        text.textContent = "PROTOCOL MISMATCH — RELOAD";
      } else if (stale && lastState !== null) {
        banner.className = "banner stalewarn";
        text.textContent = "TELEMETRY STALE · ADVICE PAUSED";
      } else if (cur) {
        if (cur.id !== shownCurrentId) {
          hold("hit", HIT_MS);
          var life = el("call-life");
          if (life) {
            life.style.setProperty("--life", (IDLE_S[cur.priority] || 20) + "s");
            life.style.setProperty("--life-at", "-" + (ageS(cur) || 0) + "s");
            restart(life, "run");
          }
          if (cur.priority === 1 && (ageS(cur) || 0) < 3 && navigator.vibrate &&
              document.visibilityState === "visible") {
            try { navigator.vibrate([90, 60, 90]); } catch (e) { /* unsupported */ }
          }
        }
        banner.className = "banner p" + cur.priority + mark("hit");
        if (text.textContent !== cur.text) text.textContent = cur.text;
        var pri = document.createElement("span");
        pri.className = "pri"; pri.textContent = "P" + cur.priority;
        meta.appendChild(pri);
        meta.appendChild(document.createTextNode(" · L" + cur.lap));
        meta.appendChild(document.createElement("br"));
        var st = document.createElement("span");
        st.className = cur.audio === "started" ? "started" : "";
        st.textContent = audioLabel(cur);
        meta.appendChild(st);
        var ag = ageText(cur);
        if (ag) meta.appendChild(document.createTextNode(" · " + ag));
        if (cur.id !== shownCurrentId && cur.priority !== 1) restart(text, "arrive");
      } else {
        banner.className = "banner idle";
        text.textContent = "— radio quiet —";
      }
      if (evid) evid.hidden = true;
    }
    if (prevEl) {
      if (prev && !mismatched) {
        prevEl.hidden = false;
        prevEl.innerHTML = "";
        var b = document.createElement("b");
        b.textContent = (stale ? "LAST RADIO (STARTED) · L" : "PREV · L") + prev.lap +
          (stale && ageText(prev) ? " · " + ageText(prev) : "");
        prevEl.appendChild(b);
        prevEl.appendChild(document.createTextNode(" · " + prev.text));
        if (prev.id !== shownPreviousId && !stale && cur && cur.priority !== 1) {
          restart(prevEl, "settle");
        }
      } else {
        prevEl.hidden = true;
      }
    }
    shownCurrentId = cur ? cur.id : null;
    shownPreviousId = prev ? prev.id : null;
    renderLog(cur, prev);
  }

  function renderLog(cur, prev) {
    var log = el("log");
    if (!log) return;
    log.innerHTML = "";
    var rows = calls.filter(function (c) { return c !== cur && c !== prev; }).reverse();
    if (!rows.length) {
      var e = document.createElement("li");
      e.className = "empty"; e.textContent = "no earlier radio";
      log.appendChild(e);
      return;
    }
    var fresh = seenLog === null ? {} : null;
    rows.forEach(function (c) {
      var li = document.createElement("li");
      if (seenLog !== null && !seenLog[c.id]) hold("enter:" + c.id, 700);
      if (fresh) fresh[c.id] = true; else seenLog[c.id] = true;
      li.className = ((c.audio === "dropped" ? "drop" : "") + mark("enter:" + c.id)).trim();
      // rows are rebuilt each render: resume the entrance where it left off
      if (on("enter:" + c.id)) {
        li.style.animationDelay = (until["enter:" + c.id] - 700 - performance.now()) + "ms";
      }
      var lap = document.createElement("span");
      lap.className = "lap"; lap.textContent = "L" + c.lap;
      var mk = document.createElement("span");
      mk.className = "mk p" + c.priority;
      var st = document.createElement("span");
      st.className = "st" + (c.audio === "dropped" ? " x" : "");
      st.textContent = heard(c) ? "▶" : c.audio === "dropped" ? "✗" : "";
      var txt = document.createElement("span");
      txt.className = "txt"; txt.textContent = c.text;
      var tag = document.createElement("span");
      if (c.audio === "dropped" || c.audio === "interrupted") {
        tag.className = "tag"; tag.textContent = c.audio;
      }
      [lap, mk, st, txt, tag].forEach(function (n) { li.appendChild(n); });
      log.appendChild(li);
    });
    if (fresh) seenLog = fresh;
  }

  function render() {
    var stale = renderFreshness();
    renderRadio(stale);
  }

  // -- protocol -------------------------------------------------------------

  function showMismatch() {
    mismatched = true;
    render();
    if (ws) try { ws.close(); } catch (e) { /* already closed */ }
  }

  function onFrame(m) {
    if (m.v !== 1) { showMismatch(); return; }
    lastSeq = m.seq;
    lastFrameAt = performance.now();
    if (typeof m.t === "number") clockOffset = m.t - Date.now() / 1000;
    var p = m.payload;
    if (m.type === "hello") {
      if (p && p.review) {
        document.body.classList.add("review");
        if (window.pitwallReviewInit) window.pitwallReviewInit();
      }
    } else if (m.type === "state" || m.type === "snapshot") {
      lastState = p; lastStateAt = performance.now();
      stateTimes.push(lastStateAt);
      renderState(p);
      if (p.calls) {
        calls = p.calls.map(function (c) {
          return Object.assign({ audio: "dispatched" }, c);
        });
      }
    } else if (m.type === "call") {
      if (!calls.some(function (c) { return c.id === p.id; })) {
        calls.push(Object.assign({ t: m.t, seq: m.seq, audio: "dispatched" }, p));
        if (calls.length > MAX_CALLS) calls = calls.slice(-MAX_CALLS);
      }
    } else if (m.type === "cancel") {
      calls.forEach(function (c) {
        if (c.id === p.id) c.audio = heard(c) ? "interrupted" : "dropped";
      });
    } else if (m.type === "spoken") {
      calls.forEach(function (c) { if (c.id === p.id && c.audio === "dispatched") c.audio = "started"; });
    } else if (m.type === "press") {
      var pe = el("press");
      if (pe) {
        var label = { ack: "ACK", neg: "NEG", silent: "RADIO SILENT", unsilent: "RADIO ON" }[p.kind] || "BOOKMARK";
        pe.hidden = false;
        pe.className = "press " + p.kind;
        clearTimeout(pressTimer);
        pressTimer = setTimeout(function () { pe.hidden = true; }, 8000);
        pe.textContent = label + " L" + (p.lap || "--") +
          (p.text ? " ▸ " + p.text : "");
      }
    }
    render();
  }

  function connect() {
    if (mismatched) return;
    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") +
      location.host + "/ws");
    ws.onopen = function () {
      backoff = 500;
      ws.send(JSON.stringify({ type: "hello", v: 1, last_seq: lastSeq }));
    };
    ws.onmessage = function (ev) { onFrame(JSON.parse(ev.data)); };
    ws.onclose = function (ev) {
      lastFrameAt = null;
      if (ev.code === 4001) { showMismatch(); return; }
      render();
      if (!mismatched) setTimeout(connect, backoff = Math.min(backoff * 2, 5000));
    };
  }

  // Spacebar = driver ack/neg/bookmark input (docs/12). Only while the
  // dashboard has focus; auto-repeat keydowns are ignored.
  function sendPress(down) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "press", down: down }));
    }
  }
  document.addEventListener("keydown", function (ev) {
    if (ev.code === "Space" && !ev.repeat && document.hasFocus()) {
      ev.preventDefault();
      sendPress(true);
    }
  });
  // P = next page, M = next mindset: same backend state the UDP Actions 4/2
  // drive, so every client (/ and /radio) stays in sync.
  document.addEventListener("keydown", function (ev) {
    if (ev.repeat || !document.hasFocus()) return;
    if (ev.code === "KeyP") sendCtl({ type: "page" });
    else if (ev.code === "KeyM") sendCtl({ type: "mindset" });
  });
  ["page", "mindset"].forEach(function (id) {
    var n = el(id);
    if (n) n.addEventListener("click", function () { sendCtl({ type: id }); });
  });
  document.addEventListener("keyup", function (ev) {
    if (ev.code === "Space" && document.hasFocus()) {
      ev.preventDefault();
      sendPress(false);
    }
  });

  // Phone: swipe left/right steps pages through the same backend page state.
  var touch0 = null;
  document.addEventListener("touchstart", function (ev) {
    if (ev.touches.length === 1 && !ev.target.closest(".transport")) {
      touch0 = { x: ev.touches[0].clientX, y: ev.touches[0].clientY };
    }
  }, { passive: true });
  document.addEventListener("touchend", function (ev) {
    if (!touch0 || !lastState) return;
    var dx = ev.changedTouches[0].clientX - touch0.x, dy = ev.changedTouches[0].clientY - touch0.y;
    touch0 = null;
    if (Math.abs(dx) < 70 || Math.abs(dx) < 2 * Math.abs(dy)) return;
    var pages = lastState.pages || ["race"], i = pages.indexOf(lastState.page || "race");
    var next = pages[(i + (dx < 0 ? 1 : pages.length - 1)) % pages.length];
    if (next) sendCtl({ type: "page", name: next });
  }, { passive: true });

  // Phone on the wheel stand: keep the screen awake where the browser allows.
  var wake = null;
  function keepAwake() {
    if (!navigator.wakeLock || document.visibilityState !== "visible" || wake) return;
    navigator.wakeLock.request("screen").then(function (w) {
      wake = w;
      w.addEventListener("release", function () { wake = null; });
    }).catch(function () { /* insecure origin or denied */ });
  }
  document.addEventListener("visibilitychange", keepAwake);
  document.addEventListener("pointerdown", keepAwake);
  keepAwake();

  // Sticky banner on phone sits directly under the (wrapping) status bar.
  var statusEl = document.querySelector(".status");
  if (statusEl && window.ResizeObserver) {
    new ResizeObserver(function () {
      document.body.style.setProperty("--status-h", statusEl.offsetHeight + "px");
    }).observe(statusEl);
  }

  setInterval(render, 250);
  render();
  connect();
})();
