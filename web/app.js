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

  // Driver -> pit wall menu (docs/12): backend-owned, a 5-row window around
  // the highlight. Fixed overlay below the call banner; no zone reflows.
  function renderMenu(m) {
    var box = el("drvmenu");
    if (!box) return;
    if (!m || !m.open || !m.items || !m.items.length) { box.hidden = true; return; }
    var list = el("drvmenu-list"), n = m.items.length, rows = Math.min(5, n);
    var start = Math.max(0, Math.min(m.index - 2, n - rows));
    list.textContent = "";
    for (var i = start; i < start + rows; i++) {
      var li = document.createElement("li");
      li.textContent = m.items[i];
      if (i === m.index) li.className = "sel";
      list.appendChild(li);
    }
    setText("drvmenu-left", (m.index + 1) + "/" + n +
      (m.left_s !== null && m.left_s !== undefined ? " · " + Math.ceil(m.left_s) + "s" : ""));
    box.hidden = false;
  }

  function renderState(p) {
    renderMenu(p.menu);
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
    renderDuel(p.strategy, p.quali);
    renderCarPage(p, p.strategy);
    renderTrackPage(p.track_info);

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
    // behind shrinking = we're being caught. Word only; the rate is the pace line.
    if (!t) return null;
    var closing = t > 0;
    var word = side === "ahead" ? (closing ? "closing" : "dropping") : (closing ? "being caught" : "pulling away");
    var cls = side === "behind" && closing ? "crit" : side === "ahead" && closing ? "ok" : "";
    return { text: (closing ? "▲ " : "▼ ") + word, cls: cls };
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
    n.appendChild(span((r.pos ? "P" + r.pos + " " : "") + String(r.name || "--").toUpperCase(), "name"));
    n.appendChild(span(gapText(gap), "gap"));
    n.appendChild(compoundBadge(r.compound, r.tyre_age));
    n.appendChild(span(paceText(r), "words " + paceClass(r.pace_delta_s, side)));
    var flags = [];
    if (r.drs) flags.push(span("DRS", "badge " + (side === "behind" ? "crit" : "ok")));
    if (side === "ahead" && s.undercut_s > 0) flags.push(span("UC", "badge ok"));
    if (side === "behind" && s.overcut_s > 0) flags.push(span("OC", "badge ok"));
    if (r.pitted) flags.push(span("PIT", "badge warn"));
    var fl = span("", "flags");
    flags.forEach(function (b) { fl.appendChild(b); });
    n.appendChild(fl);
  }

  function renderStrategy(s, q) {
    var box = el("strategy"), ph = el("strat-ph");
    if (!box) return;
    box.hidden = !s || !!q;
    if (ph && !q) ph.hidden = !!s;
    if (!s || q) return;
    setText("strat-title", "STRATEGY");
    var w = el("s-window");
    var ap = activePlan(s);
    w.innerHTML = "";
    w.className = "s-window";
    var lapNow = lastState ? lastState.lap_num : 0;
    if (ap) {
      var win = ap.window;
      w.appendChild(span("PLAN " + ap.id, ""));
      w.appendChild(span(s.on_plan === false ? "OFF PLAN +" + fmt(s.plan_off_s, 1) + "s" : "ON PLAN",
        s.on_plan === false ? "warn" : "ok"));
      w.appendChild(boxWord(win ? win[0] : null, win ? win[1] : null, lapNow));
    } else if (s.pit_window) {
      w.appendChild(boxWord(s.pit_window.start, s.pit_window.end, lapNow));
      if (s.plan) w.appendChild(span(String(s.plan.kind).toUpperCase().replace("_", " "), ""));
    } else if (s.plan && s.plan.kind) {
      w.appendChild(span(String(s.plan.kind).toUpperCase().replace("_", " ") +
        (s.plan.lap ? " L" + s.plan.lap : ""), ""));
    } else {
      w.appendChild(span("NO STOP", ""));
      if (s.laps_remaining) w.appendChild(span(s.laps_remaining + " to go", "dim"));
    }
    rivalRow("s-ahead", s.ahead, "ahead", s);
    rivalRow("s-behind", s.behind, "behind", s);
    renderStint(el("s-stint"), s, ap);
  }

  // BOX L33–35: white while far off, amber inside two laps, red once open.
  function boxWord(a, b, lapNow) {
    if (a === null || a === undefined) return span("NO STOP", "");
    var st = lapNow >= a ? "box" : lapNow >= a - 2 ? "soon" : "";
    return span((st === "box" ? "BOX NOW " : "BOX ") + "L" + a + "–" + b, st);
  }

  // Plan line: active plan as compound chips + stop count, then each alternative
  // as a lettered chip with its race-time delta (negative = faster = green).
  function renderStint(n, s, ap) {
    if (!n) return;
    n.innerHTML = "";
    if (!ap) { n.appendChild(span(s.stint_plan || "", "dim")); return; }
    var act = span("", "plan act");
    act.appendChild(span(ap.id, "pid"));
    act.appendChild(compoundSeq(ap.compounds));
    act.appendChild(span(ap.stops === 0 ? "no stop" : ap.stops + (ap.stops === 1 ? " stop" : " stops"), "dim"));
    n.appendChild(act);
    (s.plans || []).filter(function (p) { return !p.active; }).forEach(function (p) {
      var c = span("", "plan alt" + (p.kind === "reactive" ? " sc" : ""));
      c.appendChild(span(p.id, "pid"));
      if (p.kind === "reactive") c.appendChild(span("SC", "tag"));
      c.appendChild(compoundSeq(p.compounds));
      var d = p.delta_s;
      if (d === null || d === undefined) c.appendChild(span(p.kind === "reactive" ? "SC box now" : p.kind, "dim"));
      else if (Math.abs(d) < 0.05) c.appendChild(span("same time", "dim"));
      else c.appendChild(span((d < 0 ? "−" : "+") + fmt(Math.abs(d), 1) + " s " + (d < 0 ? "faster" : "slower"),
        d < 0 ? "ok" : "warn"));
      n.appendChild(c);
    });
    if (s.restricted) n.appendChild(span("rival data restricted", "dim"));
  }

  function compoundSeq(cs) {
    var q = span("", "cseq");
    (cs || []).forEach(function (c, i) {
      if (i) q.appendChild(span("›", "arr"));
      q.appendChild(span(String(c || "?").charAt(0).toUpperCase(), "comp " + compoundClass(c)));
    });
    return q;
  }

  function activePlan(s) {
    var ps = s && s.plans ? s.plans : [];
    for (var i = 0; i < ps.length; i++) if (ps[i].active) return ps[i];
    return null;
  }

  // Rival vs us. pace_delta_s + = he is slower than our pace; last_lap_delta_s
  // + = his last lap was slower than ours. Colour = threat: green good for us,
  // red an immediate threat (behind and faster / in DRS), amber caution.
  function paceClass(delta, side) {
    if (delta === null || delta === undefined || Math.abs(delta) < 0.03) return "";
    if (delta > 0) return "ok";
    return side === "behind" ? "crit" : "warn";
  }
  function paceText(r) {
    // Numeric pace line: how much the rival gains or loses on us per lap.
    var d = r.pace_delta_s;
    if (d === null || d === undefined) return "";
    if (Math.abs(d) < 0.03) return "= same pace";
    return (d < 0 ? "▲ " : "▼ ") + fmt(Math.abs(d), 2) + "/lap " + (d < 0 ? "faster" : "slower");
  }
  function compoundClass(c) {
    var w = String(c || "").toLowerCase();
    return /^c\d/.test(w) ? "slick" : w;
  }
  function compoundBadge(c, age) {
    var b = span("", "cb");
    var word = c ? String(c).toUpperCase() : "--";
    b.appendChild(span(word.length > 6 ? word.slice(0, 1) : word, "comp " + compoundClass(c)));
    b.appendChild(span((age === null || age === undefined ? "--" : age) + "L", "age"));
    return b;
  }
  function infringementBadges(inf) {
    var out = [];
    if (!inf) return out;
    if (inf.penalty_s) out.push(span("PEN +" + inf.penalty_s + "s", "badge pen"));
    if (inf.drive_throughs) out.push(span(inf.drive_throughs + "× DRIVE-THRU", "badge pen"));
    if (inf.stop_gos) out.push(span(inf.stop_gos + "× STOP-GO", "badge pen"));
    if (inf.warnings) out.push(span(inf.warnings + " WARN", "badge inf"));
    if (inf.corner_cut_warnings) out.push(span(inf.corner_cut_warnings + " CUT", "badge inf"));
    return out;
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
    var abs = r.gap_s === null || r.gap_s === undefined ? null : Math.abs(r.gap_s);
    var inDrs = abs !== null && abs <= 1;

    var head = span("", "b-head");
    head.appendChild(span(side === "ahead" ? "AHEAD" : "BEHIND", "side"));
    head.appendChild(span(r.pos ? "P" + r.pos : "", "pos"));
    head.appendChild(span(String(r.name || "--").toUpperCase(), "name"));
    head.appendChild(span(gapText(gap), "gap" + (inDrs ? (side === "behind" ? " crit" : " ok") : "")));
    n.appendChild(head);

    n.appendChild(rail);
    rail.hidden = abs === null;
    if (abs !== null) {
      rail.style.setProperty("--g", String(Math.min(abs, 3) / 3));
      rail.className = "rail " + side + (inDrs ? " in" : "");
    }

    // Row 1: pace per lap + tyre badge.
    var pace = span("", "b-pace");
    var pw = paceText(r);
    pace.appendChild(span(pw || "pace unknown", "words " + (pw ? paceClass(r.pace_delta_s, side) : "dim")));
    pace.appendChild(compoundBadge(r.compound, r.tyre_age));
    n.appendChild(pace);

    // Row 2: his last lap and the delta to ours; gap trend on the right.
    var lap = span("", "b-lap");
    lap.appendChild(span("LAST", "k"));
    lap.appendChild(span(lapTime(r.last_lap_ms), "t"));
    var d = r.last_lap_delta_s;
    lap.appendChild(span(d === null || d === undefined ? "" :
      (d > 0 ? "+" : d < 0 ? "−" : "±") + fmt(Math.abs(d), 3) + (d > 0 ? " slower" : d < 0 ? " faster" : ""),
      "d " + paceClass(d, side)));
    var tr = trendText(r.gap_trend_s, side);
    lap.appendChild(span(tr ? tr.text : "gap steady", "tr " + (tr ? tr.cls : "dim")));
    n.appendChild(lap);

    // Row 3: badges — DRS, strategy lever, pit state, stewards. Always present
    // (min-height) so the card never changes height as badges come and go.
    var bad = span("", "b-badges");
    if (r.drs) bad.appendChild(span("DRS", "badge " + (side === "behind" ? "crit" : "ok")));
    if (side === "ahead" && s.undercut_s > 0) bad.appendChild(span("UNDERCUT +" + fmt(s.undercut_s, 1), "badge ok"));
    if (side === "behind" && s.overcut_s > 0) bad.appendChild(span("OVERCUT +" + fmt(s.overcut_s, 1), "badge ok"));
    if (r.pitted) bad.appendChild(span("PITTED", "badge warn"));
    infringementBadges(r.infringements).forEach(function (b) { bad.appendChild(b); });
    n.appendChild(bad);

    var closing = r.gap_trend_s > 0;
    var threat = side === "behind" && (r.drs || inDrs || closing || paceClass(r.pace_delta_s, side) === "crit");
    var edge = side === "ahead" && (r.drs || inDrs);
    n.className = "b-card " + side + (threat ? " threat" : side === "behind" ? " calm" : edge ? " edge" : "");
  }

  var BATTLE_WORDS = { free_air: "FREE AIR", catching: "CATCHING", attacking: "ATTACKING",
    defending: "DEFENDING", under_threat: "UNDER THREAT", managing: "MANAGING" };
  var BATTLE_CLS = { attacking: "ok", catching: "ok", defending: "warn", under_threat: "crit" };
  var RESULT_WORDS = { passed: "PASSED", failed: "ATTACK FAILED", held: "HELD", lost: "LOST THE PLACE" };
  var RESULT_CLS = { passed: "ok", held: "ok", failed: "warn", lost: "crit" };

  function renderBattleState(id, b, s) {
    var n = el(id);
    if (!n) return;
    n.innerHTML = "";
    var mode = b ? b.mode : null;
    if (!mode) { n.hidden = true; return; }
    n.hidden = false;
    var res = b.result;
    n.appendChild(span(res ? (RESULT_WORDS[res] || String(res).toUpperCase()) :
      (BATTLE_WORDS[mode] || String(mode).replace("_", " ").toUpperCase()),
      "mode " + (res ? RESULT_CLS[res] || "" : BATTLE_CLS[mode] || "")));
    if (res) n.appendChild(span(BATTLE_WORDS[mode] || String(mode).replace("_", " ").toUpperCase(),
      "sub " + (BATTLE_CLS[mode] || "")));
    var bits = [];
    if (b.mode_laps) bits.push(b.mode_laps + (b.mode_laps === 1 ? " lap" : " laps"));
    if ((mode === "catching" || mode === "attacking") && b.catch_laps !== null && b.catch_laps !== undefined)
      bits.push("catch in " + fmt(b.catch_laps, 1) + " laps");
    if ((mode === "defending" || mode === "under_threat") && b.threat_laps !== null && b.threat_laps !== undefined)
      bits.push("caught in " + fmt(b.threat_laps, 1) + " laps");
    if ((mode === "catching" || mode === "attacking") && b.pass_prob !== null && b.pass_prob !== undefined)
      bits.push("pass " + fmt(100 * b.pass_prob, 0) + "%");
    if ((mode === "defending" || mode === "under_threat") && b.hold_prob !== null && b.hold_prob !== undefined)
      bits.push("hold " + fmt(100 * b.hold_prob, 0) + "%");
    n.appendChild(span(bits.join(" · "), "detail"));
  }

  // Race page right column: battle state, ahead/behind cards, pit exit; the
  // radio log sits below. Cards hide (placeholder stays) when no rival is in scope.
  function renderDuel(s, q) {
    var on = !!(s && !q);
    document.body.classList.toggle("duel-on", on);
    if (!on) return;
    var ph = el("d-ph");
    if (ph) ph.hidden = !!(s.ahead || s.behind);
    renderBattleState("d-state", s.battle, s);
    battleCard("d-ahead", s.ahead, "ahead", s);
    battleCard("d-behind", s.behind, "behind", s);
    var pe = s.pit_exit || {}, f = el("d-plan");
    if (!f) return;
    f.innerHTML = "";
    var ap = activePlan(s);
    if (ap) {
      f.appendChild(span("PLAN " + ap.id, "k"));
      if (s.plan_target_lap) f.appendChild(span("target L" + s.plan_target_lap, ""));
    } else if (s.plan && s.plan.kind) {
      f.appendChild(span(String(s.plan.kind).toUpperCase().replace("_", " ") +
        (s.plan.lap ? " L" + s.plan.lap : ""), ""));
    }
    f.appendChild(span("PIT EXIT", "k"));
    f.appendChild(span(pe.clean ? "CLEAR AIR" : "TRAFFIC", pe.clean ? "ok" : "warn"));
    f.appendChild(span((pe.rival ? String(pe.rival.name || "").toUpperCase() + " " + gapText(pe.rival.gap_s) : "") +
      (s.pit_loss_s ? " · loss " + fmt(s.pit_loss_s, 1) + " s (" + (s.pit_loss_source || "prior") + ")" : ""), "dim"));
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
    setClass("cp-life", "big" + (s && s.laps_of_pace !== null && s.laps_remaining && s.laps_of_pace < s.laps_remaining ?
      (s.laps_of_pace < s.laps_remaining - 3 ? " delta-crit" : " delta-warn") : ""));
    setText("cp-life-sub", s ? "of pace left · " + (s.laps_remaining || 0) + " to go" +
      (s.tyres && s.tyres.wear_per_lap_pct ? " · " + fmt(s.tyres.wear_per_lap_pct, 1) + "%/lap wear" : "") : "deg model needs race laps");
    var f = [];
    if (s && s.tyres) {
      if (s.tyres.overheat) f.push("OVERHEATING");
      if (s.tyres.graining) f.push("GRAINING");
      if (s.tyres.blister_max_pct) f.push("BLISTER " + s.tyres.blister_max_pct + "%");
    }
    setText("cp-flags", f.length ? f.join(" · ") : "tyres nominal");
    meter("cp-energy-bar", p.ers_pct === null || p.ers_pct === undefined ? null : p.ers_pct / 100,
      p.ers_pct < 20 ? "short" : "");
    meter("cp-fuel-bar", fd === null || fd === undefined ? null : Math.max(0, Math.min(1, 0.5 + fd / 4)),
      fd < 0 ? "crit" : fd < 0.5 ? "short" : "");
    var lop = s ? s.laps_of_pace : null, togo = s ? s.laps_remaining : 0;
    meter("cp-life-bar", lop === null || lop === undefined || !togo ? null : Math.min(1, lop / togo),
      lop !== null && lop !== undefined && togo && lop < togo ? (lop < togo - 3 ? "crit" : "short") : "");
    setClass("cp-flags", "cp-flags" + (f.length ? " warn" : " ok"));
  }

  function meter(id, frac, cls) {
    var n = el(id);
    if (!n) return;
    n.parentNode.hidden = frac === null;
    if (frac === null) return;
    n.style.setProperty("--f", String(Math.max(0, Math.min(1, frac))));
    n.className = cls || "";
  }

  var WEATHER_WORDS = { 0: "clear", 1: "light cloud", 2: "overcast", 3: "light rain", 4: "heavy rain", 5: "storm" };
  var WEATHER_ICONS = { 0: "☀", 1: "☀☁", 2: "☁", 3: "☂", 4: "☔", 5: "⚡" };
  function tpRow(id, k, v, cls) {
    var n = el(id);
    if (!n) return;
    n.innerHTML = "";
    n.appendChild(span(k, "k"));
    n.appendChild(span(v, "v"));
    n.className = "tp-row" + (cls ? " " + cls : "");
  }
  // Weather tiles: sky type + rain *chance* (forecast probability, never intensity).
  function wxTile(i, w, pct) {
    var n = el("tp-wx-" + i);
    if (!n) return;
    var known = w !== null && w !== undefined && w >= 0;
    n.querySelector(".ico").textContent = known ? WEATHER_ICONS[w] || "·" : "·";
    n.querySelector(".sky").textContent = known ? WEATHER_WORDS[w] || "--" : "--";
    n.querySelector(".pct").textContent = known && pct !== null && pct !== undefined ? pct + "%" : "--";
    // Neutral unless rain is actually coming: blue = wet/likely, red = heavy rain / storm.
    n.className = "tp-wxt" + (!known ? "" : w >= 4 ? " storm" : w >= 3 || pct >= 40 ? " wet" : " dry");
  }
  function crossRow(c) {
    var n = el("tp-cross");
    if (!n) return;
    n.innerHTML = "";
    n.appendChild(span("☂ CROSSOVER", "k"));
    var v = span("", "v");
    if (c) {
      v.appendChild(span("to", "dim"));
      var cb = span("", "cb");
      cb.appendChild(span(String(c).toUpperCase(), "comp " + compoundClass(c)));
      v.appendChild(cb);
      v.appendChild(span("likely inside the race", ""));
    } else v.appendChild(span("none forecast", "dim"));
    n.appendChild(v);
    n.className = "tp-row" + (c ? " cold" : "");
  }
  function renderTrackPage(t) {
    if (!t) return;
    var st = el("tp-status");
    if (st) {
      st.textContent = t.red_flag ? "RED FLAG" : t.safety_car ? (SC_WORDS[t.safety_car] || "SC") +
        (t.sc_laps ? " · " + t.sc_laps + " laps" : "") : String(t.phase || "--").replace("_", " ").toUpperCase();
      st.className = "tp-status" + (t.red_flag ? " red" : t.safety_car ? " sc" : "");
    }
    var r = t.rain_chance_pct || [null, null, null];
    var wf = t.weather_forecast || [t.weather, -1, -1];
    for (var i = 0; i < 3; i++) wxTile(i, wf[i], r[i]);
    crossRow(t.weather_crossover);
    tpRow("tp-flags", "⚑ FLAG", t.blue_flag ? "BLUE · let the leader by" : "none", t.blue_flag ? "cold" : "");
    var pens = [];
    if (t.penalty_s) pens.push("+" + t.penalty_s + " s");
    if (t.unserved) pens.push(t.unserved + " UNSERVED");
    if (t.warnings) pens.push(t.warnings + (t.warnings === 1 ? " warning" : " warnings"));
    if (t.corner_cut_warnings) pens.push(t.corner_cut_warnings + (t.corner_cut_warnings === 1 ? " cut" : " cuts"));
    tpRow("tp-pens", "⚠ PENALTIES", pens.length ? pens.join(" · ") : "none", t.unserved ? "crit" : t.penalty_s ? "warn" : "");
    tpRow("tp-traffic", "GAPS", "ahead " + gapText(t.gap_ahead_s) + " · behind " +
      gapText(t.gap_behind_s === null || t.gap_behind_s === undefined ? null : -t.gap_behind_s), "");
    tpRow("tp-exit", "PIT EXIT", t.pit_exit_clean ? "CLEAR AIR" : "TRAFFIC", t.pit_exit_clean ? "ok" : "warn");
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

  var LOG_ROWS = 3;  // older calls shown under the banner (docs/15 §4)

  function renderLog(cur, prev) {
    var log = el("log");
    if (!log) return;
    log.innerHTML = "";
    var rows = calls.filter(function (c) { return c !== cur && c !== prev; }).reverse()
      .slice(0, LOG_ROWS);
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
  // Arrow Up/Down = menu up/down (UDP Actions 2/3), Enter = confirm,
  // Escape = close (docs/12). Space still confirms like UDP Action 1.
  var MENU_KEYS = { ArrowUp: "up", ArrowDown: "down", Enter: "confirm", Escape: "close" };
  document.addEventListener("keydown", function (ev) {
    var op = MENU_KEYS[ev.code];
    if (!op || ev.repeat || !document.hasFocus()) return;
    if (ev.target && ev.target.closest && ev.target.closest("input, textarea, select, button")) return;
    ev.preventDefault();
    sendCtl({ type: "menu", op: op });
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
    touch0 = null;
    if (ev.touches.length === 1 && !ev.target.closest(".transport")) {
      touch0 = { x: ev.touches[0].clientX, y: ev.touches[0].clientY };
    }
  }, { passive: true });
  document.addEventListener("touchcancel", function () { touch0 = null; }, { passive: true });
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
