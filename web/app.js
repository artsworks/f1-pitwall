// pitwall dashboard client — vanilla, no deps.
(function () {
  "use strict";
  var ws = null, lastSeq = null, backoff = 500, mismatched = false;
  var calls = []; // {id, seq, rule_id, priority, text, lap, spoken}

  function el(id) { return document.getElementById(id); }
  function setText(id, txt) { var n = el(id); if (n) n.textContent = txt; }

  function setStale(on) { document.body.classList.toggle("stale", on); }

  function fmt(n, digits) {
    if (n === null || n === undefined || isNaN(n)) return "--";
    return Number(n).toFixed(digits === undefined ? 1 : digits);
  }

  function renderState(p) {
    var stale = !p.live || (p.packet_age_ms !== null && p.packet_age_ms > 1000);
    setStale(stale);
    setText("live", p.live ? "LIVE" : "STALE");
    setText("rate", p.rate_hz ? fmt(p.rate_hz, 0) + " Hz" : "--");
    setText("age", p.packet_age_ms !== null ? fmt(p.packet_age_ms, 0) + " ms" : "--");
    setText("session", p.session_kind + " " + (p.session_type || "") +
      " · " + (p.track || "--"));
    setText("lap", p.lap_num + "/" + p.total_laps);
    setText("fuel", "fuel " + fmt(p.fuel_remaining_laps, 1) + " laps");
    setText("position", "P" + (p.position || "--"));
    setText("phase", p.phase || "--");
    setText("compound", "compound " + p.tyre_compound + " age " + p.tyre_age_laps);
    setText("ers", "ERS " + fmt(p.ers_pct, 0) + "%");
    setText("sc", "SC " + (p.safety_car || 0));
    setText("mindset", p.mindset + " · " + p.verbosity + (p.quiet ? " · quiet" : ""));
    ["fl", "fr", "rl", "rr"].forEach(function (k) {
      var t = p.tyres[k], c = el("tyre-" + k);
      if (!c) return;
      c.querySelector(".temp").textContent = fmt(t.inner, 0) + "°";
      var st = c.querySelector(".status");
      st.textContent = t.status;
      st.className = "status status-" + t.status.toLowerCase();
      c.querySelector(".wear").textContent =
        fmt(t.surface, 0) + "° surf · wear " + fmt(t.wear, 0) + "%";
    });
    setText("brakes",
      "brakes FL " + fmt(p.brakes.fl, 0) + " FR " + fmt(p.brakes.fr, 0) +
      " RL " + fmt(p.brakes.rl, 0) + " RR " + fmt(p.brakes.rr, 0));
    if (p.damage) {
      var d = p.damage;
      var parts = [
        ["front_left_wing", "FW-L"], ["front_right_wing", "FW-R"],
        ["rear_wing", "RW"], ["floor", "floor"], ["diffuser", "diffuser"],
        ["sidepod", "sidepod"], ["gearbox", "gearbox"], ["engine", "engine"],
      ];
      var items = [], warn = false;
      parts.forEach(function (pair) {
        var v = d[pair[0]];
        if (v > 0) { items.push(pair[1] + " " + v + "%"); if (v >= 20) warn = true; }
      });
      if (d.drs_fault) { items.push("DRS fault"); warn = true; }
      if (d.ers_fault) { items.push("ERS fault"); warn = true; }
      var dmg = el("damage");
      if (!dmg) return;
      dmg.textContent = items.length ? "damage " + items.join(" · ") : "damage none";
      dmg.className = warn ? "warn" : "dim";
    }
    if (p.latency) {
      setText("latency",
        "p99 " + fmt(p.latency.trigger_to_speak_p99_ms, 0) + "ms call · " +
        fmt(p.latency.packet_to_ws_p99_ms, 0) + "ms ws");
    }
  }

  function renderLog() {
    var log = el("log");
    if (!log) return;
    log.innerHTML = "";
    calls.slice(-12).forEach(function (c) {
      var li = document.createElement("li");
      var lap = document.createElement("span");
      lap.className = "lap"; lap.textContent = "L" + c.lap;
      var txt = document.createElement("span");
      txt.textContent = " " + c.text;
      txt.className = "p" + c.priority;
      li.appendChild(lap); li.appendChild(txt);
      if (c.spoken) {
        var tick = document.createElement("span");
        tick.className = "spoken"; tick.textContent = " ✓";
        li.appendChild(tick);
      }
      log.appendChild(li);
    });
    var banner = el("banner");
    if (banner) {
      var top = null;
      for (var i = calls.length - 1; i >= 0; i--) {
        if (calls[i].priority <= 2) { top = calls[i]; break; }
      }
      banner.textContent = top ? top.text : "";
      banner.className = "banner" + (top ? " p" + top.priority : "");
    }
  }

  function showMismatch() {
    mismatched = true;
    var b = el("banner");
    if (b) { b.textContent = "protocol mismatch — reload"; b.className = "banner mismatch"; }
    if (ws) try { ws.close(); } catch (e) {}
  }

  function onFrame(m) {
    if (m.v !== 1) { showMismatch(); return; }
    lastSeq = m.seq;
    var p = m.payload;
    if (m.type === "state" || m.type === "snapshot") {
      renderState(p);
      if (p.calls) {
        calls = p.calls.map(function (c) { return { ...c }; });
        renderLog();
      }
    } else if (m.type === "call") {
      calls.push(p); if (calls.length > 12) calls = calls.slice(-12);
      renderLog();
    } else if (m.type === "cancel") {
      calls = calls.filter(function (c) { return c.id !== p.id; });
      renderLog();
    } else if (m.type === "spoken") {
      calls.forEach(function (c) { if (c.id === p.id) c.spoken = true; });
      renderLog();
    }
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
      setStale(true);
      setText("live", "STALE");
      setText("age", "--");
      if (ev.code === 4001) { showMismatch(); return; }
      if (!mismatched) setTimeout(connect, backoff = Math.min(backoff * 2, 5000));
    };
  }

  connect();
})();
