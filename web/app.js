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
    var m = el("mindset");
    if (m) {
      m.textContent = String(p.mindset || "--").toUpperCase();
      m.className = "pill" + (p.mindset === "aggressive" ? " aggr" : "");
    }
    var v = el("verbosity");
    if (v) {
      v.innerHTML = "";
      v.appendChild(document.createTextNode(p.verbosity || ""));
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
        c.className = "corner " + st;
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

    setText("fuel", fmt(p.fuel_remaining_laps, 1) + " laps");
    var toGo = p.total_laps && p.lap_num ? p.total_laps - p.lap_num + 1 : null;
    setText("fuel-sub", toGo !== null && (p.session_kind === "race")
      ? "left · " + toGo + " to finish" : "laps of fuel left");

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
    setText("c-mode", "target " + fmt(minPct, 0) + "% · deploy mode " + c.ers_mode);
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
        banner.className = "banner p" + cur.priority;
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
    rows.forEach(function (c) {
      var li = document.createElement("li");
      if (c.audio === "dropped") li.className = "drop";
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
        var label = p.kind === "ack" ? "ACK" : p.kind === "neg" ? "NEG" : "BOOKMARK";
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
  document.addEventListener("keyup", function (ev) {
    if (ev.code === "Space" && document.hasFocus()) {
      ev.preventDefault();
      sendPress(false);
    }
  });

  setInterval(render, 250);
  render();
  connect();
})();
