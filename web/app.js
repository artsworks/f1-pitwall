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
        q.className = "quiet"; q.textContent = " QUIET";
        v.appendChild(q);
      }
    }

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
