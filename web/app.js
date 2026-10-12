// pitwall dashboard client — vanilla, no deps. Layout/lifecycle: docs/15-dashboard-design.md.
(function () {
  "use strict";
  var STALE_MS = 1000, CONNECTING_MS = 3000, MAX_CALLS = 12;
  var IDLE_S = { 1: 30, 2: 20, 3: 20 };
  var COMPOUNDS = { 16: "SOFT", 17: "MEDIUM", 18: "HARD", 7: "INTER", 8: "WET" };
  var SC_WORDS = { 1: "SAFETY CAR", 2: "VSC", 3: "FORMATION" };

  var ws = null, lastSeq = null, backoff = 500, mismatched = false;
  var SILENT = "data:audio/wav;base64," +
    "UklGRkQDAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YSADAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==";
  var soundOn = false, serverAudio = false, spokenIds = {}, cancelledIds = {}, audioQueue = [];
  var playingId = null, soundAttempt = 0, soundFailed = false;
  var voiceListening = false, voiceFlash = null, voiceFlashTimer = null;
  var armRadio = document.getElementById("arm-radio");
  var soundButton = document.getElementById("sound");
  var audioSupported = typeof window.Audio === "function";
  var speechSupported = !!window.speechSynthesis;
  var utteranceSupported = !!window.SpeechSynthesisUtterance;
  var player = audioSupported ? new Audio() : null;
  var voiceBlip = audioSupported ? new Audio("/api/voice/blip.wav") : null;
  var mobile = /[?&]sound=1/.test(location.search) ||
    (navigator.maxTouchPoints > 0 && matchMedia("(pointer: coarse)").matches);
  if (/[?&]sound=0/.test(location.search)) mobile = false;
  if (soundButton) soundButton.hidden = !mobile;

  function updateSoundButtons() {
    var blocked = soundFailed && !soundOn;
    if (soundButton) {
      soundButton.setAttribute("aria-pressed", String(soundOn));
      soundButton.textContent = blocked ? "SOUND BLOCKED. TAP AGAIN" : soundOn ? "SOUND ON" : "ENABLE SOUND";
    }
    if (armRadio) {
      armRadio.setAttribute("aria-pressed", String(soundOn));
      armRadio.textContent = blocked ? "SOUND BLOCKED. TAP AGAIN" : soundOn ? "PHONE RADIO ARMED" : "ARM PHONE RADIO";
    }
  }

  function stopSound() {
    soundAttempt += 1;
    soundOn = false;
    soundFailed = false;
    audioQueue = [];
    playingId = null;
    if (player) player.pause();
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    updateSoundButtons();
    sendCtl({ type: "audio", on: false });
  }

  function startSound() {
    var attempt = ++soundAttempt;
    soundFailed = false;
    updateSoundButtons();
    function armed() {
      if (attempt !== soundAttempt) return;
      soundOn = true;
      updateSoundButtons();
      sendCtl({ type: "audio", on: true });
    }
    function blocked() {
      if (attempt !== soundAttempt) return;
      if (!serverAudio && speechSupported) {
        armed();
      } else {
        soundFailed = true;
        updateSoundButtons();
      }
    }
    if (!player) {
      if (speechSupported) armed();
      return;
    }
    player.src = SILENT;
    try {
      var started = player.play();
      if (started && typeof started.then === "function") {
        started.then(armed).catch(blocked);
      } else {
        armed();
      }
    } catch (e) {
      blocked();
    }
  }

  function toggleSound() {
    keepAwake();
    if (soundOn) stopSound();
    else startSound();
  }

  function playNextAudio() {
    if (!soundOn || !player || playingId !== null || !audioQueue.length) return;
    var item = audioQueue.shift();
    playingId = item.id;
    player.src = item.src;
    try {
      var started = player.play();
      if (started && typeof started.catch === "function") {
        started.catch(function () {
          if (playingId === item.id) {
            playingId = null;
            playNextAudio();
          }
        });
      }
    } catch (e) {
      playingId = null;
      playNextAudio();
    }
  }

  function finishAudio() {
    if (playingId === null) return;
    playingId = null;
    playNextAudio();
  }

  function queueAudio(p) {
    if (!soundOn || !player || !p || !p.id || !p.data || cancelledIds[p.id]) return;
    var item = {
      id: p.id,
      priority: p.priority,
      src: "data:audio/wav;base64," + p.data
    };
    if (p.priority === 1) {
      audioQueue = [];
      playingId = null;
      player.pause();
      audioQueue.push(item);
    } else {
      var i = 0;
      while (i < audioQueue.length && audioQueue[i].priority <= item.priority) i += 1;
      audioQueue.splice(i, 0, item);
    }
    playNextAudio();
  }

  function cancelAudio(id) {
    cancelledIds[id] = true;
    if (Object.keys(cancelledIds).length > 200) cancelledIds = {};
    audioQueue = audioQueue.filter(function (item) { return item.id !== id; });
    if (playingId === id) {
      playingId = null;
      if (player) player.pause();
      playNextAudio();
    }
  }

  if (!audioSupported && !speechSupported) {
    [armRadio, soundButton].forEach(function (button) {
      if (!button) return;
      button.disabled = true;
      button.textContent = "PHONE SPEECH UNAVAILABLE";
    });
  } else {
    [armRadio, soundButton].forEach(function (button) {
      if (button) button.addEventListener("click", toggleSound);
    });
    if (player) {
      player.addEventListener("ended", finishAudio);
      player.addEventListener("error", finishAudio);
    }
    updateSoundButtons();
  }

  function phoneCall(c) {
    if (!soundOn || serverAudio || !utteranceSupported || !c || !c.id || spokenIds[c.id]) return;
    spokenIds[c.id] = true;
    if (Object.keys(spokenIds).length > 200) spokenIds = {};
    if (c.priority === 1) window.speechSynthesis.cancel();
    var utterance = new window.SpeechSynthesisUtterance(c.text);
    utterance.rate = 1.15;
    window.speechSynthesis.speak(utterance);
  }
  var calls = []; // {id, seq, t, priority, text, lap, audio}
  var clockOffset = 0; // server epoch seconds - local epoch seconds
  var startedAt = performance.now(), lastFrameAt = null, lastState = null, lastStateAt = null;
  var stateTimes = [], shownCurrentId = null, shownPreviousId = null;
  var pressTimer = null;
  // motion state: a class stays on for a fixed window so the 4 Hz re-render
  // never restarts or cancels a running animation (docs/15 §11).
  var until = {}, shownPage = null, shownPos = null, shownLap = null, tyreSt = {}, seenLog = null, menuCap = null;
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
      setText("age", age === null ? "no data" : fmt(age / 1000, 1) + " s old");
    } else {
      setText("live", "LIVE");
      setText("age", fmt(age, 0) + " ms");
    }
    var ageEl = el("age");
    if (ageEl) ageEl.className = stale ? "crit" : age !== null && age >= 500 ? "warn" : "dim";
    var now = performance.now();
    stateTimes = stateTimes.filter(function (t) { return now - t < 1000; });
    setText("rate", stale ? "" : stateTimes.length + " upd/s");
    return stale;
  }

  // -- telemetry zones (docs/15 §3) --------------------------------------------

  // Driver -> pit wall menu (docs/12): backend-owned, a 5-row window around the
  // highlight. Floating overlay with a scrim below the banner; no zone reflows.
  function renderMenu(m) {
    var open = !!(m && m.open && m.items && m.items.length);
    document.body.classList.toggle("menu", open);
    if (!open) return;
    var list = el("drvmenu-list"), n = m.items.length, rows = Math.min(5, n);
    var start = Math.max(0, Math.min(m.index - 2, n - rows));
    list.textContent = "";
    for (var i = start; i < start + rows; i++) {
      var li = document.createElement("li");
      li.textContent = m.items[i];
      if (i === m.index) li.className = "sel";
      list.appendChild(li);
    }
    var left = m.left_s !== null && m.left_s !== undefined ? Math.ceil(m.left_s) : null;
    setText("drvmenu-left", (m.index + 1) + "/" + n + (left !== null ? " · closes " + left + "s" : ""));
    var bar = el("drvmenu-meter");
    if (!m.left_s) menuCap = null;
    else if (!menuCap || m.left_s > menuCap) menuCap = m.left_s;
    if (bar) bar.style.setProperty("--f", menuCap ? String(m.left_s / menuCap) : "0");
  }

  // One-shot edge glow (ACK / NEG / new call / heard / missed); steady while listening.
  function fx(kind) {
    var n = el("fx");
    if (!n || n.classList.contains("listen")) return;
    n.className = "fx";
    void n.offsetWidth;
    n.className = "fx once " + kind;
  }
  function fxListen(open) {
    var n = el("fx");
    if (!n) return;
    if (open) n.className = "fx listen";
    else if (n.classList.contains("listen")) n.className = "fx";
  }

  function renderState(p) {
    renderMenu(p.menu);
    voiceListening = !!(p.voice && p.voice.listening);
    renderVoice(p.voice);
    var s = p.strategy || null, t = p.track_info || {};
    setText("phase", (p.phase || "--").replace("_", " ").toUpperCase());
    setText("session", (p.session_label || p.session_kind || "--").toUpperCase() + " · " +
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
    var mind = el("mindset");
    if (mind) {
      mind.textContent = p.mindset === "aggressive" ? "AGGR" : p.mindset === "balanced" ? "BAL" :
        String(p.mindset || "--").slice(0, 4).toUpperCase();
      mind.className = "pill" + (p.mindset === "aggressive" ? " aggr" : "");
    }
    var v = el("verbosity");
    if (v) {
      v.innerHTML = "";
      if (p.verbosity && p.verbosity !== "normal") v.appendChild(document.createTextNode(String(p.verbosity).toUpperCase()));
      if (p.silent) v.appendChild(span(" ✕ SILENT", "silent"));
      if (p.quiet) v.appendChild(span(" QUIET" + (p.quiet_left_s ? " " + clock(p.quiet_left_s) : ""), "quiet"));
    }
    var flag = el("flag"), sc = p.safety_car || 0;
    if (flag) {
      var scw = sc ? SC_WORDS[sc] || "SAFETY CAR" : "";
      var word = p.red_flag ? "⚠ RED FLAG" : p.paused ? "❚❚ PAUSED" : scw ? "⚠ " + scw : "";
      flag.hidden = !word;
      flag.textContent = word;
      flag.className = "flag" + (p.red_flag ? " red" : scw ? " sc" : "");
    }
    document.body.classList.toggle("redflag", !!p.red_flag);
    document.body.classList.toggle("sc", !!sc);
    var ov = el("ov");
    if (ov) {
      var otOn = !!(s && s.overtake), otNext = !!(s && s.overtake_earned);
      ov.className = "chip ov" + (otOn ? " active" : otNext ? " avail" : "");
    }
    setClass("sm", "chip sm" + (s && s.aero_mode === 1 ? " on" : ""));

    var q = p.quali || null, cool = q ? q.cool : null;
    document.body.classList.toggle("quali", !!q && p.session_kind === "qualifying");
    renderCool(cool, q);
    renderPitBoard(p.pit_board, q, p.phase);
    if (q) renderQZone(q, p);
    renderTyres(p, s);
    renderSetupStrip(p);
    renderFuel(p, s);
    renderBatt(p, s);
    renderWeather(t);
    renderDamage(p.damage);
    renderCtx(p, s, t);

    setText("ers", "ERS " + fmt(p.ers_pct, 0) + "%");
    setText("sc", "SC " + sc);
    if (p.latency) {
      setText("latency", "voice lag " + fmt(p.latency.trigger_to_speak_p99_ms, 0) +
        " ms · screen lag " + fmt(p.latency.packet_to_ws_p99_ms, 0) + " ms");
    }
  }

  // Tyres: trend arrow from the fast EMA (inner) vs tyre_inner_ema_slow, wear %,
  // window meter on a 60..130 °C scale, HOT WEAR / condition tags, LOCKED border.
  var T_LO = 60, T_HI = 130, TREND_C = 0.5;
  function renderTyres(p, s) {
    var comp = COMPOUNDS[p.tyre_visual] || (p.tyre_visual ? "C" + p.tyre_visual : "--");
    setText("compound", comp);
    setClass("compound", "comp " + comp.toLowerCase());
    setText("tyre-age", p.tyre_age_laps !== undefined && p.tyre_age_laps !== null ?
      p.tyre_age_laps + (p.tyre_age_laps === 1 ? " lap" : " laps") : "--");
    var lop = s ? s.laps_of_pace : null, pace = el("t-pace");
    if (pace) {
      pace.innerHTML = "";
      if (lop === null || lop === undefined) {
        pace.className = "t-pace";
        pace.appendChild(span("deg model warming up", "dim"));
      } else {
        var toGo = s.laps_remaining || 0;
        var cls = lop >= toGo || lop >= 10 ? "ok" : lop >= 4 ? "warn" : "crit";
        pace.className = "t-pace " + cls;
        pace.appendChild(document.createTextNode("~" + fmt(lop, 0) + " laps of pace " + (cls === "ok" ? "▬ " : "▼ ")));
        pace.appendChild(span(cls === "ok" ? "PLENTY" : cls === "warn" ? "SHRINKING" : "NEAR CLIFF", "", "small"));
      }
    }
    if (!p.tyres) return;
    var st = s ? s.tyres || {} : {};
    var worst = null;
    CORNERS.forEach(function (k) {
      if (p.tyres[k] && (worst === null || p.tyres[k].wear > p.tyres[worst].wear)) worst = k;
    });
    CORNERS.forEach(function (k) {
      var t = p.tyres[k], c = el("tyre-" + k);
      if (!c || !t) return;
      var status = String(t.status || "").toLowerCase();
      if (tyreSt[k] && tyreSt[k] !== status) hold("flip:" + k, FLIP_MS);
      tyreSt[k] = status;
      c.className = "corner " + status + mark("flip:" + k) + (t.lockup ? " locked" : "");
      c.querySelector(".temp").textContent = fmt(t.inner, 0) + "°";
      // Owner choice: a coloured arrow alone (no word) skips the colour + word rule here.
      var d = t.inner !== null && t.tyre_inner_ema_slow !== null && t.tyre_inner_ema_slow !== undefined ?
        t.inner - t.tyre_inner_ema_slow : 0;
      var tr = c.querySelector(".trend");
      tr.className = "trend " + (d > TREND_C ? "up" : d < -TREND_C ? "down" : "flat");
      tr.textContent = d > TREND_C ? "▲" : d < -TREND_C ? "▼" : "▬";
      tr.title = d > TREND_C ? "heating" : d < -TREND_C ? "cooling" : "steady";
      var f = t.inner === null ? 0 : Math.max(0, Math.min(1, (t.inner - T_LO) / (T_HI - T_LO)));
      c.querySelector(".meter i").style.setProperty("--f", f.toFixed(3));
      c.querySelector(".wear b").textContent = fmt(t.wear, 0) + "%";
      c.querySelector(".surf").textContent = "surf " + fmt(t.surface, 0) + "°";
      var brk = c.querySelector(".brk");
      if (brk) brk.textContent = p.brakes ? "BRK " + fmt(p.brakes[k], 0) + "°" : "";
      var tags = c.querySelector(".tags");
      tags.innerHTML = "";
      if (t.lockup) tags.appendChild(span("LOCKED", "tag lock"));
      if (st.wear_hot_corner === k) {
        tags.appendChild(span("HOT WEAR" + (st.wear_hot_rate ? " +" + fmt(st.wear_hot_rate, 1) + "%/lap" : ""), "tag crit"));
      }
      if (k === worst) {
        if (st.overheat) tags.appendChild(span("OVERHEAT", "tag crit"));
        if (st.blister_max_pct >= 5) tags.appendChild(span("BLISTER " + fmt(st.blister_max_pct, 0) + "%", "tag warn"));
        if (st.graining) tags.appendChild(span("GRAINING", "tag warn"));
      }
    });
  }

  // BB / DIFF: live values from the setup packet. Arrows only when the pit board
  // carries a setup-advice target for that field (garage sessions).
  function renderSetupStrip(p) {
    var vals = p.setup ? p.setup.values || {} : {};
    var advice = p.pit_board ? p.pit_board.setup_advice || [] : [];
    function one(id, field, sug) {
      var v = vals[field], rec = null;
      advice.forEach(function (r) { if (r.fields.indexOf(field) >= 0) rec = r; });
      setText(id, v === undefined || v === null ? "--" : fmt(v, 0) + "%");
      var suited = rec && v !== undefined && Number(v) === Number(rec.to);
      setClass(id, "v" + (suited ? " suited" : ""));
      if (!sug) return;
      if (!rec) { setText(sug, ""); setClass(sug, "sug none"); return; }
      setText(sug, suited ? "✓ suited" : (rec.to > v ? "▲ try " : "▼ try ") + fmt(rec.to, 0));
      setClass(sug, "sug" + (suited ? " none" : ""));
    }
    one("su-bb", "brake_bias", "su-bb-sug");
    one("su-on", "on_throttle", "su-on-sug");
    one("su-off", "off_throttle", null);
  }

  function renderFuel(p, s) {
    var fdl = s ? s.fuel_delta_laps : null, game = fdl === null || fdl === undefined;
    var d = game ? p.fuel_remaining_laps : fdl;
    setText("fuel", d === null || d === undefined ? "--" : (d >= 0 ? "+" : "−") + fmt(Math.abs(d), 1));
    setClass("fuel", "big" + (d === null || d === undefined ? "" : d < 0 ? " crit" : d < 0.5 ? " warn" : " ok"));
    setText("fuel-sub", "laps " + (d < 0 ? "SHORT" : "SPARE") + (game ? " · game" : ""));
  }

  var BATT_WORDS = { on_budget: "ON BUDGET", over: "OVER · SAVE", under: "UNDER · CHARGING", attack_ok: "SPEND · ATTACK OK" };
  var BATT_ICONS = { on_budget: "M13 2 4 14h7l-1 8 9-12h-7z", attack_ok: "M13 2 4 14h7l-1 8 9-12h-7z",
    over: "M12 3c1 4 5 5 5 10a5 5 0 0 1-10 0c0-3 2-4 2-7 2 1 3 2 3 4",
    under: "M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0zM12 18v4" };
  function renderBatt(p, s) {
    var mode = s && s.energy ? s.energy.mode : null;
    setClass("batt", "tile batt" + (mode ? " " + mode : ""));
    setText("batt-pct", fmt(p.ers_pct, 0) + "%");
    var m = el("batt-mode");
    if (m && m.dataset.mode !== String(mode)) {
      m.dataset.mode = String(mode);
      m.innerHTML = mode && BATT_ICONS[mode] ?
        '<svg class="ico" viewBox="0 0 24 24"><path d="' + BATT_ICONS[mode] + '"/></svg> ' : "";
      m.appendChild(document.createTextNode(BATT_WORDS[mode] || "no budget yet"));
    }
  }

  // Weather: icon + word per forecast slot, rain chance, crossover marker on the
  // first slot at or after weather_crossover_min (slots are now / 10 / 30 min).
  var WEATHER_WORDS = { 0: "CLEAR", 1: "LIGHT CLOUD", 2: "OVERCAST", 3: "LIGHT RAIN", 4: "HEAVY RAIN", 5: "STORM" };
  var CROSS = { to_inter: ["INTER", "inter", "warn"], to_wet: ["WET", "wet", "cold"], to_dry: ["SLICK", "medium", "warn"] };
  var SLOT_MIN = [0, 10, 30];
  function crossChip(c) { return span(CROSS[c][0], "comp " + CROSS[c][1] + " xs"); }
  function renderWeather(t) {
    var wf = t.weather_forecast || [t.weather, -1, -1], r = t.rain_chance_pct || [null, null, null];
    var c = CROSS[t.weather_crossover] ? t.weather_crossover : null, cm = t.weather_crossover_min;
    var slot = -1;
    if (c) {
      slot = 2;
      for (var j = 0; j < 3; j++) if (cm === null || cm === undefined || SLOT_MIN[j] >= cm) { slot = j; break; }
    }
    for (var i = 0; i < 3; i++) {
      var n = el("wxs-" + i);
      if (!n) continue;
      var w = wf[i], pct = r[i], known = w !== null && w !== undefined && w >= 0;
      n.className = (!known ? "" : w >= 4 ? "storm" : w >= 3 || pct >= 40 ? "wet" : "dry") +
        (i === slot ? " xo-" + CROSS[c][2] : "");
      var k = n.querySelector(".k");
      k.textContent = i === 0 ? "NOW" : "IN " + SLOT_MIN[i];
      if (i === slot) { k.appendChild(document.createTextNode(" ")); k.appendChild(crossChip(c)); }
      n.querySelector("use").setAttribute("href", "#wx-" + (known ? w : 2));
      n.querySelector(".pct").textContent = known && pct !== null && pct !== undefined ? pct + "%" : "--";
      n.querySelector(".sky").textContent = known ? WEATHER_WORDS[w] || "--" : "--";
    }
    var x = el("cross");
    if (!x) return;
    x.innerHTML = "";
    if (!c) { x.className = "cross"; x.textContent = "✓ no crossover"; return; }
    x.className = "cross " + CROSS[c][2];
    x.appendChild(document.createTextNode("→ "));
    x.appendChild(crossChip(c));
    x.appendChild(document.createTextNode((cm !== null && cm !== undefined ? " in ~" + fmt(cm, 0) + " min" : " soon") +
      (t.weather_crossover_pct !== null && t.weather_crossover_pct !== undefined ? " · rain " + fmt(t.weather_crossover_pct, 0) + "%" : "")));
  }

  // Damage: amber from 5 %, red from 25 %. The number is what the engineer speaks.
  var DMG_PARTS = [["front_left_wing", "FW L"], ["front_right_wing", "FW R"], ["rear_wing", "RW"],
    ["floor", "Floor"], ["diffuser", "Diffuser"], ["sidepod", "Sidepod"], ["gearbox", "Gearbox"], ["engine", "Engine"]];
  function renderDamage(d) {
    var box = el("damage");
    if (!box || !d) return;
    box.innerHTML = "";
    DMG_PARTS.forEach(function (pair) {
      var val = d[pair[0]];
      if (!(val > 0)) return;
      var row = span(pair[1] + " ", "");
      row.appendChild(span(val + "%", "n" + (val >= 25 ? " crit" : val >= 5 ? " warn" : "")));
      box.appendChild(row);
    });
    [["drs_fault", "DRS fault"], ["ers_fault", "ERS fault"]].forEach(function (pair) {
      if (d[pair[0]]) box.appendChild(span(pair[1], "n crit"));
    });
    if (!box.childNodes.length) box.appendChild(span("✓ none", "none"));
  }

  function card(id, show, cls, head, line, rank) {
    var n = el(id);
    if (!n) return;
    n.hidden = !show;
    if (!show) return;
    if (cls !== null) n.className = cls;
    if (rank) n.querySelector(".rank").textContent = rank;
    var h = n.querySelector(".head");
    h.innerHTML = "";
    (Array.isArray(head) ? head : [head]).forEach(function (x) {
      h.appendChild(typeof x === "string" ? document.createTextNode(x) : x);
    });
    n.querySelector(".line").innerHTML = line;
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (ch) { return "&#" + ch.charCodeAt(0) + ";"; }); }

  // Contextual zone: Safety > Execution > Reply > Tactical > Info, fixed card order.
  function renderCtx(p, s, t) {
    var sc = p.safety_car || 0, scLive = sc === 1 || sc === 2, c = CROSS[t.weather_crossover] ? t.weather_crossover : null;
    var cm = t.weather_crossover_min;
    if (p.red_flag) {
      card("c-safety", true, "card safety red", "⚠ RED FLAG", "box this lap · tyres free to change", "SAFETY");
    } else if (sc) {
      card("c-safety", true, "card safety", "⚠ " + (SC_WORDS[sc] || "SAFETY CAR"),
        (t.sc_laps ? "lap " + t.sc_laps + " under " + (sc === 2 ? "VSC" : "SC") + " · " : "") +
        (s && s.pit_loss_s ? "<b>pit loss " + fmt(s.pit_loss_s, 1) + " s</b>" : ""), "SAFETY");
    } else if (c && cm !== null && cm !== undefined && cm <= 5) {
      card("c-safety", true, "card safety" + (c === "to_wet" ? " cold" : ""), ["☂ RAIN CROSSOVER → ", crossChip(c)],
        (t.weather_crossover_pct !== null && t.weather_crossover_pct !== undefined ? "rain <b>" + fmt(t.weather_crossover_pct, 0) + "%</b> " : "") +
        "in <b>~" + fmt(cm, 0) + " min</b>", "SAFETY");
    } else if (t.blue_flag) {
      card("c-safety", true, "card safety cold", "⚑ BLUE FLAG", "let the leader by", "SAFETY");
    } else card("c-safety", false);

    var ap = activePlan(s), lapNow = p.lap_num || 0, win = ap && ap.window;
    var boxNow = !!(s && ((win && lapNow >= win[0] && lapNow <= win[1]) ||
      (s.plans || []).some(function (x) { return x.kind === "reactive" && scLive; })));
    card("c-exec", boxNow && !p.red_flag, null, "BOX NOW" + (ap ? " · PLAN " + ap.id : ""),
      s ? (win ? "window L" + win[0] + "–" + win[1] : "") + (s.pit_loss_s ? " · costs " + fmt(s.pit_loss_s, 0) + " s" : "") : "",
      "EXECUTION");
    card("c-reply", false);
    renderBattle(s, p.position);
    renderPlans(s, scLive);
    var info = el("c-info"), msg = null;
    if (s && s.ahead && s.ahead.pitted) msg = "◦ INFO · " + String(s.ahead.name || "car ahead") + " pitted";
    else if (s && s.behind && s.behind.pitted) msg = "◦ INFO · " + String(s.behind.name || "car behind") + " pitted";
    else if (t.penalty_s) msg = "◦ INFO · penalty +" + t.penalty_s + " s" + (t.unserved ? " · " + t.unserved + " unserved" : "");
    if (info) { info.hidden = !msg; if (msg) info.textContent = msg; }
  }

  // Plan C (kind "reactive") is sent every tick; show it only under SC/VSC or once it is the active plan.
  function renderPlans(s, scLive) {
    var box = el("c-plans");
    if (!box) return;
    box.innerHTML = "";
    var plans = (s ? s.plans || [] : []).filter(function (pl) { return pl.kind !== "reactive" || pl.active || scLive; });
    if (!plans.length) {
      box.appendChild(span(s ? s.stint_plan || "no stop planned" : "--", "line"));
      return;
    }
    plans.forEach(function (pl) {
      var row = span("", "plan" + (pl.active ? "" : pl.kind === "reactive" ? " c" : " alt"));
      row.appendChild(span("PLAN " + pl.id, "pid"));
      if (pl.kind === "reactive") {
        row.appendChild(span("BOX NOW", "boxnow"));
        if (pl.delta_s !== null && pl.delta_s !== undefined) row.appendChild(document.createTextNode("cheap stop saves " + fmt(Math.abs(pl.delta_s), 1) + " s"));
        box.appendChild(row);
        return;
      }
      var seq = span("", "seq");
      (pl.compounds || []).forEach(function (cp, i) {
        if (i) seq.appendChild(span(" → ", "arr"));
        seq.appendChild(span(String(cp || "?").charAt(0).toUpperCase(), "comp " + compoundClass(cp)));
      });
      row.appendChild(seq);
      if (pl.active) {
        var w = pl.window;
        row.appendChild(document.createTextNode(w ? "window L" + w[0] + "–" + w[1] : pl.stops === 0 ? "no stop" : ""));
        row.appendChild(span(s.on_plan === false ? "✗ OFF PLAN " + fmt(s.plan_off_s, 1) + " s" : "✓ ON PLAN",
          "onp" + (s.on_plan === false ? " off" : "")));
      } else {
        var d = pl.delta_s;
        row.appendChild(document.createTextNode((pl.stops === 1 ? "1 stop" : pl.stops + " stops") +
          (d === null || d === undefined ? "" : " · " + (d < 0 ? "−" : "+") + fmt(Math.abs(d), 1) + " s")));
      }
      box.appendChild(row);
    });
  }

  function span(txt, cls, tag) {
    var n = document.createElement(tag || "span");
    n.textContent = txt;
    if (cls) n.className = cls;
    return n;
  }
  function compoundClass(c) {
    var w = String(c || "").toLowerCase();
    return /^c\d/.test(w) ? "slick" : w;
  }
  function activePlan(s) {
    var ps = s && s.plans ? s.plans : [];
    for (var i = 0; i < ps.length; i++) if (ps[i].active) return ps[i];
    return null;
  }

  // Battle card (docs/18): single rival as before, or per-car rows (2 ahead, 2
  // behind) with a YOU divider when a second car is in play or places swap.
  var MODES = ["free_air", "catching", "attacking", "defending", "under_threat", "managing"];
  var MODE_WORDS = { free_air: "FREE AIR", catching: "▶ CATCHING", attacking: "▶ ATTACKING",
    defending: "◀ DEFENDING", under_threat: "◀ UNDER THREAT", managing: "MANAGING" };
  var MODE_CLS = { catching: "catching", attacking: "attack", defending: "threat", under_threat: "threat" };
  function gapStr(g, side) { return g === null || g === undefined ? "--" : (side === "ahead" ? "+" : "−") + fmt(Math.abs(g), 1); }
  function rail(g, side, train) {
    var r = span("", "rail " + side + (train ? " train" : g <= 1 ? " in" : ""), "div");
    r.appendChild(span(train ? "" : "1 S", "rz", "div"));
    var c = span("", "rc", "div");
    c.style.setProperty("--g", Math.max(0, Math.min(1, g / 3)).toFixed(3));
    r.appendChild(c);
    return r;
  }
  function rvRow(sideTxt, name, ot, st, gap) {
    var rv = span("", "rv", "div");
    rv.appendChild(span(sideTxt, "side"));
    var nm = span(name, "name");
    if (ot) nm.appendChild(span("OV", "tag crit"));
    rv.appendChild(nm);
    if (st !== null) rv.appendChild(span(st, "st"));
    rv.appendChild(span(gap, "gap"));
    return rv;
  }
  function paceRow(b, side) {
    var closing = side === "ahead" ? b.closing_ahead_s : b.closing_behind_s;
    var prob = side === "ahead" ? b.pass_prob : b.hold_prob;
    if (!closing && (prob === null || prob === undefined)) return null;
    var row = span("", "pace", "div");
    row.appendChild(closing > 0 ? span((side === "ahead" ? "▶" : "▲") + " closing " + fmt(closing, 1) + " s/lap",
      side === "ahead" ? "ok" : "crit") : span("", ""));
    if (prob !== null && prob !== undefined) row.appendChild(span((side === "ahead" ? "PASS " : "HOLD ") + fmt(100 * prob, 0) + "%", "prob"));
    return row;
  }
  function renderBattle(s, pos) {
    var n = el("c-battle"), b = s && s.battle;
    if (!n) return;
    if (!b || !b.mode) { n.hidden = true; return; }
    n.hidden = false;
    n.innerHTML = "";
    var mode = b.mode, multi = !!(b.ahead2 || b.behind2 || /^(scrap|swap_)/.test(b.result || ""));
    var cls = MODE_CLS[mode] || (mode === "managing" || mode === "free_air" ? "calm" : "");
    if (!multi) {
      n.className = "card battle single " + cls;
      var modes = span("", "modes", "div");
      MODES.forEach(function (m) { modes.appendChild(span(m.replace("_", " "), m === mode ? "on" : "")); });
      n.appendChild(modes);
      var word = span(MODE_WORDS[mode] || mode.toUpperCase(), "mode", "div");
      var sub = mode === "catching" && b.catch_laps ? "in range in " + fmt(b.catch_laps, 0) + " laps" :
        mode === "under_threat" && b.threat_laps ? "in range in " + fmt(b.threat_laps, 0) + " laps" :
        b.mode_laps ? b.mode_laps + (b.mode_laps === 1 ? " lap" : " laps") : "";
      if (sub) word.appendChild(span(" · " + sub, "sub"));
      n.appendChild(word);
      var side = mode === "defending" || mode === "under_threat" ? "behind" : "ahead";
      var r = s[side], other = s[side === "ahead" ? "behind" : "ahead"];
      if (r) {
        n.appendChild(rvRow(side === "ahead" ? "▲ AHEAD" : "▼ BEHIND", (r.pos ? "P" + r.pos + " " : "") +
          String(r.name || "--").toUpperCase(), r.overtake && side === "behind", null, gapStr(r.gap_s, side)));
        if (r.gap_s !== null && r.gap_s <= 3) n.appendChild(rail(r.gap_s, side, false));
        var pr = paceRow(b, side);
        if (pr) n.appendChild(pr);
      }
      if (other) {
        var o = rvRow(side === "ahead" ? "▼ BEHIND" : "▲ AHEAD", (other.pos ? "P" + other.pos + " " : "") +
          String(other.name || "--").toUpperCase(), false, null, gapStr(other.gap_s, side === "ahead" ? "behind" : "ahead"));
        o.style.opacity = ".6";
        n.appendChild(o);
      }
      return;
    }
    n.className = "card battle " + cls + (b.result === "scrap" ? " scrap" : "");
    var swaps = b.swaps ? " · " + b.swaps + (b.swaps === 1 ? " swap" : " swaps") : "";
    var cars = [[b.ahead2, "ahead", 2], [s.ahead, "ahead", 1], null, [s.behind, "behind", 1], [b.behind2, "behind", 2]];
    var grp = span("", "grp" + (cls === "threat" ? " crit" : ""), "div");
    if (b.result === "scrap") {
      var ps = cars.filter(function (c) { return c && c[0] && c[0].pos; }).map(function (c) { return c[0].pos; }).concat(pos ? [pos] : []);
      grp.appendChild(document.createTextNode("⇄ SCRAP "));
      grp.appendChild(span(ps.length ? "· P" + Math.min.apply(null, ps) + " to P" + Math.max.apply(null, ps) + swaps : swaps, "sub"));
    } else if (b.result === "swap_ahead" || b.result === "swap_behind") {
      var who = b.result === "swap_ahead" ? s.ahead : s.behind;
      grp.appendChild(document.createTextNode("⇄ TRADING PLACES "));
      grp.appendChild(span("· " + (who ? who.name : "--") + swaps, "sub"));
    } else {
      grp.appendChild(document.createTextNode(MODE_WORDS[mode] || mode.toUpperCase()));
    }
    n.appendChild(grp);
    cars.forEach(function (c, i) {
      if (c === null) { n.appendChild(span("YOU · P" + (pos || "--"), "you", "div")); return; }
      var r = c[0], side = c[1];
      if (!r) return;
      var near = cars[side === "ahead" ? 1 : 3][0];
      var train = c[2] === 2 && near && r.gap_s !== null && near.gap_s !== null && Math.abs(r.gap_s - near.gap_s) <= 1;
      // The direct rival's row word is battle.mode; the other rows stay MANAGING (FREE AIR in free air).
      var direct = c[2] === 1 && (side === "ahead" ? mode === "attacking" || mode === "catching" :
        mode === "defending" || mode === "under_threat");
      var st = train ? "IN TRAIN" : direct ? MODE_WORDS[mode].slice(2) : mode === "free_air" ? "FREE AIR" : "MANAGING";
      var calm = !train && !direct;
      var row = span("", "bcar " + (train ? "train" : calm ? "calm" : side === "behind" ? "defend" : "attack"), "div");
      row.appendChild(rvRow((side === "ahead" ? "▲ P" : "▼ P") + (r.pos || "-"), String(r.name || "--").toUpperCase(),
        r.overtake && side === "behind", st, gapStr(r.gap_s, side)));
      if (r.gap_s !== null && r.gap_s <= 3 && !calm) row.appendChild(rail(r.gap_s, side, train));
      if (direct) { var pr = paceRow(b, side); if (pr) row.appendChild(pr); }
      n.appendChild(row);
    });
  }

  // Qualifying contextual zone: phase strip, cut-off, pole / teammate sectors, this run.
  function qRow(id, gaps, worst) {
    var row = el(id);
    if (!row) return;
    var tds = row.querySelectorAll("td");
    for (var i = 0; i < 3; i++) {
      var v = gaps ? gaps[i] : 0;
      tds[i].textContent = v ? signed(v) : "--";
      tds[i].className = (v < 0 ? "ok" : v > 0 ? "crit" : "") + (i === worst && v > 0 ? " worst" : "");
    }
  }
  function renderQZone(q, p) {
    var ph = q.release ? "garage" : p.phase;
    var strip = el("q-phase");
    if (strip) Array.prototype.forEach.call(strip.children, function (c) { c.className = c.dataset.ph === ph ? "on" : ""; });
    var m = q.margin_ms, has = m !== null && m !== undefined;
    var through = q.through || (has && m > 0);
    setText("q-pos", p.position ? "P" + p.position : "P--");
    setText("q-badge", q.margin_kind === "pole" ? (has && m > 0 ? "POLE" : "P2+") : through ? "THROUGH" : has ? "OUT" : "--");
    setClass("q-badge", "badge" + (has && !through ? " out" : ""));
    setText("q-margin", has ? signed(-m) : "--");
    el("q-margin").style.color = has && !through ? "var(--crit)" : "";
    setText("q-cut-line", (q.margin_kind === "pole" ? "vs P2" : "predicted cut-off " + lapTime(q.cutoff_ms)) +
      " · best " + lapTime(q.best_lap_ms) + " · " + clock(q.session_time_left) + " left · " + q.fresh_sets + " fresh");
    var pole = q.pole || (q.cool && q.cool.pole) || null;
    setText("q-pole", pole ? "pole " + (pole.driver || "--") : "no pole time");
    setText("q-pole-gap", pole ? signed(pole.gap_ms) : "--");
    el("q-pole-gap").style.color = pole && pole.gap_ms > 0 ? "var(--crit)" : "";
    var gaps = pole ? pole.sector_gaps_ms : null;
    qRow("q-gaps", gaps, pole && pole.worst_sector ? pole.worst_sector - 1 : -1);
    var mate = q.teammate || null;
    qRow("q-mate-gaps", mate ? mate.sector_gaps_ms : null, mate && mate.worst_sector ? mate.worst_sector - 1 : -1);
    setText("q-mate", mate ? "mate " + (mate.driver || "--") + " " + signed(mate.gap_ms) : "mate --");
    var head = "--", line = "no run plan yet", lap = q.lap;
    if (q.release) {
      var r = q.release;
      head = r.time_for_out_lap === false ? "✗ STAY IN" : r.clean ? "✓ GO NOW" : r.wait_s !== null ? "WAIT " + fmt(r.wait_s, 0) + " s" : "⚠ TRAFFIC";
      line = r.cars_on_track + " on track" + (r.gap_ahead_s !== null ? " · " + fmt(r.gap_ahead_s, 0) + " s clear ahead" : "");
    } else if (lap) {
      head = "PROJ " + lapTime(lap.projected_ms) + (lap.abort ? " · ✗ ABORT" : "");
      line = lap.delta_ms === null ? "no cut-off yet" : signed(lap.delta_ms) + " to cut";
    } else if (q.plan) {
      head = "PLAN " + String(q.plan.plan).toUpperCase();
      line = q.plan.reason || "";
    }
    setText("q-run-head", head);
    setText("q-run-line", line);
  }
  function renderVoice(v) {
    var pill = el("voice"), tag = el("drvmenu-voice");
    var listening = voiceListening && v && v.listening;
    var flash = voiceFlash && voiceFlash.until > Date.now() ? voiceFlash : null;
    if (pill) {
      if (listening) {
        pill.hidden = false;
        pill.className = "voice" + (v.warn ? " warn" : "");
        pill.textContent = "● LISTENING" +
          (v.left_s !== null && v.left_s !== undefined ? " " + Math.ceil(v.left_s) + "s" : "");
      } else if (flash) {
        pill.hidden = false;
        pill.className = "voice " + flash.kind;
        pill.textContent = flash.text;
      } else {
        pill.hidden = true;
      }
    }
    if (tag) {
      tag.hidden = !listening;
      tag.className = "voice-tag" + (v && v.warn ? " warn" : "");
    }
    fxListen(!!listening);
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
    var s = Math.round(ms / 100) / 10, m = Math.floor(s / 60);
    return m + ":" + ("0" + (s - m * 60).toFixed(1)).slice(-4);
  }
  function signed(ms) {
    if (ms === null || ms === undefined) return "--";
    return (ms > 0 ? "+" : ms < 0 ? "−" : "±") + (Math.abs(ms) / 1000).toFixed(1);
  }
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
      return { text: "✗ STAY IN · NO TIME", cls: "crit", sub: sub.join(" · ") };
    }
    if (r.clean) return { text: "✓ GO · TRACK CLEAR", cls: "ok", sub: sub.join(" · ") };
    if (r.wait_s !== null) return { text: "◷ WAIT " + fmt(r.wait_s, 0) + " s", cls: "warn", sub: sub.join(" · ") };
    return { text: "◷ WAIT · TRAFFIC", cls: "crit", sub: "no gap in 60 s · " + sub.join(" · ") };
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
      cls = "hold"; word = "✓ KEEP";
    }
    node.className = "pb-corner " + cls;
    move.textContent = word;
    psi.textContent = psiTxt;
    det.textContent = t.avg_c !== null ? "run avg " + Math.round(t.avg_c) + "°" : "no run data";
  }

  function setupFormat(field) {
    var out = null;
    SETUP_GROUPS.forEach(function (g) {
      if (g[1]) g[1].forEach(function (f) { if (f[0] === field) out = f; });
    });
    return out;
  }
  function setupValue(f, v) {
    return v === undefined || v === null ? "--" : fmt(v, f ? f[2] : 0) + (f ? f[3] || "" : "");
  }
  function changeRow(list, done, name, from, to, why) {
    var li = document.createElement("li");
    li.className = done ? "done" : "todo";
    [span(done ? "☑" : "☐", "box"), span(name, "nm"),
      span(done ? to + " ✓" : from + " → " + to, "val"), span(why || "", "why")]
      .forEach(function (n) { li.appendChild(n); });
    list.appendChild(li);
  }

  // Garage setup: what to change first, in large type, then the full setup for reference.
  function renderSetup(b) {
    var change = el("pb-change"), list = el("pb-setup"), lockBox = el("pb-locked");
    if (!change || !list) return;
    change.innerHTML = ""; list.innerHTML = "";
    var setupAdvice = b.setup_advice || [], setupLocked = b.setup_locked || [];
    var setup = b.setup || {};
    function adviceFor(field) {
      return setupAdvice.find(function (rec) { return rec.fields.indexOf(field) >= 0; });
    }
    function isLocked(field) {
      return setupLocked.some(function (rec) { return rec.fields.indexOf(field) >= 0; });
    }
    function why(rec) {
      return (rec.tier === "alternative" ? "alt · " : "") + rec.reason;
    }
    function matchesTarget(rec, current) {
      if (current === undefined || current === null) return false;
      var tolerance = rec.param.indexOf("pressure") >= 0 ? 0.05 : 0;
      return Math.abs(Number(current) - Number(rec.to)) <= tolerance;
    }
    CORNERS.forEach(function (k) {
      var t = b.tyres[k];
      if (t.target_psi === null || !t.delta_psi || t.limited) return;
      changeRow(change, !!t.applied, k.toUpperCase() + " psi", fmt(t.psi, 1), fmt(t.target_psi, 1), "");
    });
    setupAdvice.forEach(function (rec) {
      if (rec.param === "front_pressure" || rec.param === "rear_pressure") {
        var axle = rec.param === "front_pressure" ? ["fl", "fr"] : ["rl", "rr"];
        if (axle.some(function (k) { return b.tyres[k].target_psi !== null; })) return;
        var done = rec.fields.every(function (f) { return matchesTarget(rec, setup[f]); });
        changeRow(change, done, rec.param === "front_pressure" ? "F psi" : "R psi",
          fmt(rec.from, 1), fmt(rec.to, 1), why(rec));
        return;
      }
      var f = setupFormat(rec.fields[0]);
      var name = rec.fields.map(function (field) {
        var ff = setupFormat(field);
        return ff ? ff[1] : field.replace(/_/g, " ");
      }).join(" + ");
      changeRow(change, matchesTarget(rec, setup[rec.fields[0]]), name,
        setupValue(f, rec.from), setupValue(f, rec.to), why(rec));
    });
    if (!change.children.length) {
      var none = document.createElement("li");
      none.className = "none"; none.textContent = "✓ No changes";
      change.appendChild(none);
    }
    if (!b.setup) {
      var e = document.createElement("li");
      e.className = "dim"; e.textContent = "no setup packet yet";
      list.appendChild(e);
    } else {
      SETUP_GROUPS.forEach(function (g) {
        var li = document.createElement("li"), chips = span("", "chips");
        li.appendChild(span(g[0], "grp"));
        var fields = g[1] || CORNERS.map(function (k) {
          return [k, k.toUpperCase(), 1, "", b.tyres[k].psi];
        });
        fields.forEach(function (f) {
          var v = g[1] ? setup[f[0]] : f[4];
          var rec = g[1] ? adviceFor(f[0]) : null;
          var c = span("", "c" + (rec && !matchesTarget(rec, v) ? " todo" : "") +
            (g[1] && isLocked(f[0]) ? " lk" : ""));
          c.appendChild(span(f[1] + " ", "k"));
          var val = document.createElement("b");
          val.textContent = setupValue(f, v);
          c.appendChild(val);
          chips.appendChild(c);
        });
        li.appendChild(chips);
        list.appendChild(li);
      });
    }
    if (lockBox) {
      var lockList = Array.isArray(b.setup_lock_checklist) ? b.setup_lock_checklist : null;
      lockBox.hidden = !lockList;
      if (lockList) {
        lockBox.textContent = "Locked after this session: " + lockList.map(function (item) {
          var f = setupFormat(item.field);
          return f ? f[1] : item.field.replace(/_/g, " ");
        }).join(", ");
      }
    }
  }

  var PLAN_TEXT = { push: "PUSH", cool: "COOL", box: "BOX", push_now: "PUSH NOW" };
  var PLAN_WHY = { ready: "ready", battery: "battery low", tyres: "tyres hot", safe: "you're through",
    time: "no time to cool", fuel: "fuel low", flag: "flag", invalid: "lap invalid" };
  function pbTile(id, show, cls, v, s, k) {
    var n = el(id);
    if (!n) return;
    n.hidden = !show;
    if (!show) return;
    n.className = "pb-tile " + (cls || "");
    if (k) n.querySelector(".k").textContent = k;
    n.querySelector(".v").textContent = v;
    n.querySelector(".s").textContent = s || "";
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
    var plan = q && q.plan && q.plan.plan;
    pbTile("pb-plan", !!plan, plan === "box" || plan === "cool" ? "warn" : "ok",
      PLAN_TEXT[plan] || String(plan).toUpperCase(), plan ? PLAN_WHY[q.plan.reason] || q.plan.reason : "");
    var hasFuel = b.fuel_laps !== null && b.fuel_laps !== undefined;
    pbTile("pb-fuel", hasFuel, b.fuel_laps >= b.fuel_need_laps ? "ok" : "crit",
      fmt(b.fuel_laps, 1) + " laps", "push lap needs " + fmt(b.fuel_need_laps, 1));
    var hasErs = b.ers_pct !== null && b.ers_pct !== undefined;
    pbTile("pb-ers", hasErs, b.ers_pct >= b.ers_need_pct ? "ok" : "warn",
      fmt(b.ers_pct, 0) + "%", "want " + fmt(b.ers_need_pct, 0) + "%");
    pbTile("pb-time", !!q, q && q.fresh_sets === 0 ? "warn" : "",
      q ? clock(q.session_time_left) : "", q ? q.fresh_sets + " fresh set" + (q.fresh_sets === 1 ? "" : "s") : "");
    var hasGap = !!q && q.margin_ms !== null && q.margin_ms !== undefined;
    var through = !!q && q.through;
    pbTile("pb-pole", !!q && (hasGap || !!q.best_lap_ms),
      through || (hasGap && q.margin_ms > 0) ? "ok" : hasGap ? "crit" : "",
      hasGap ? signed(-q.margin_ms) : q ? lapTime(q.best_lap_ms) : "",
      hasGap ? "best " + lapTime(q.best_lap_ms) + (through ? " · through" : "") : through ? "through" : "",
      hasGap ? (q.margin_kind === "pole" ? "VS P2" : "VS CUT") : "BEST");
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
    setText("cz-last", lapTime(c.last_hot_ms));
    var pg = c.pole ? c.pole.gap_ms : null;
    setText("cz-pole", signed(pg));
    setClass("cz-pole", "v" + (pg > 0 ? " crit" : pg < 0 ? " ok" : ""));
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
      mode.textContent = (c.recharging ? "✓ RECHARGING" : "⚠ SET RECHARGE") +
        " · need " + fmt(minPct, 0) + "%";
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
      behind.textContent = s === null ? "✓ NO HOT LAP BEHIND" :
        "⚠ HOT LAP " + fmt(s, 1) + " s BEHIND · MOVE OFF LINE";
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
      pole.textContent = c.pole
        ? "POLE " + (c.pole.driver || "") + " " + signed(-c.pole.gap_ms).replace("−", "-")
        : "POLE --";
    }
    var secs = el("c-sectors");
    if (secs) {
      var gaps = c.pole ? c.pole.sector_gaps_ms : [0, 0, 0];
      var mine = (c.pole && c.pole.sectors_ms) || [0, 0, 0];
      var top = gaps.indexOf(Math.max.apply(null, gaps));
      Array.prototype.forEach.call(secs.children, function (box, i) {
        var ms = gaps[i];
        box.querySelector(".t").textContent = mine[i] ? (mine[i] / 1000).toFixed(1) : "--";
        box.querySelector(".d").textContent = ms ? signed(ms) : "";
        box.className = "c-sec" + (i === top && ms > 0 ? " worst" : ms < 0 ? " gain" : "");
      });
    }
    setText("c-mis", "LAST LAP " + (c.last_hot_ms ? lapTime(c.last_hot_ms) : "--") + " · " +
      (c.mistakes || "clean"));
    setText("c-sub", (q ? clock(q.session_time_left) + " left · " + q.fresh_sets + " fresh · " : "") +
      "fuel " + fmt(c.fuel_laps, 1) + " laps");
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

  var PRI_WORDS = { 1: "⚠ URGENT", 2: "● ACTION", 3: "◦ INFO" };
  function audioLabel(c) {
    switch (c.audio) {
      case "started": return "▶ spoken";
      case "interrupted": return "✂ cut off";
      case "dropped": return "✗ skipped";
      default: return "… queued";
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
        pri.className = "pri"; pri.textContent = PRI_WORDS[cur.priority] || "INFO";
        meta.appendChild(pri);
        meta.appendChild(document.createTextNode(" · LAP " + cur.lap));
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
        b.textContent = (stale ? "LAST RADIO · LAP " : "BEFORE · LAP ") + prev.lap +
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
      e.className = "empty"; e.textContent = "no earlier radio yet";
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
        tag.className = "tag"; tag.textContent = c.audio === "dropped" ? "skipped" : "cut off";
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
      cancelledIds = {};
      serverAudio = !!(p && p.audio);
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
        fx("call");
      }
      phoneCall(p);
    } else if (m.type === "audio") {
      queueAudio(p);
    } else if (m.type === "cancel") {
      cancelAudio(p.id);
      if (soundOn && !serverAudio && spokenIds[p.id] && window.speechSynthesis) {
        window.speechSynthesis.cancel();
      }
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
      if (p.kind === "ack" || p.kind === "neg") fx(p.kind);
    } else if (m.type === "voice") {
      if (p.state === "listening") {
        voiceListening = true;
        voiceFlash = null;
        clearTimeout(voiceFlashTimer);
        if (soundOn && voiceBlip) {
          voiceBlip.currentTime = 0;
          voiceBlip.play().catch(function () {});
        }
        renderVoice({ listening: true, via: p.via, left_s: p.cap_s, warn: false });
      } else if (p.state === "closed") {
        voiceListening = false;
        var message = "";
        var kind = "";
        if (p.reason === "recognised") {
          message = "HEARD ▸ " + (p.item || p.text || "?");
          kind = "heard";
        } else if (p.reason === "miss" || p.reason === "low_confidence") {
          message = "MISSED ▸ " + (p.text || "?");
          kind = "missed";
        } else if (p.reason === "cap") {
          message = "NO REQUEST";
          kind = "missed";
        }
        fxListen(false);
        if (kind) fx(kind);
        if (message) {
          voiceFlash = { text: message, kind: kind, until: Date.now() + 3000 };
          clearTimeout(voiceFlashTimer);
          voiceFlashTimer = setTimeout(function () {
            voiceFlash = null;
            renderVoice(lastState && lastState.voice);
          }, 3000);
        } else {
          voiceFlash = null;
          clearTimeout(voiceFlashTimer);
        }
        renderVoice(lastState && lastState.voice);
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
      if (soundOn) sendCtl({ type: "audio", on: true });
    };
    ws.onmessage = function (ev) { onFrame(JSON.parse(ev.data)); };
    ws.onclose = function (ev) {
      lastFrameAt = null;
      if (ev.code === 4003) {
        location.replace("/pin?next=" + encodeURIComponent(location.pathname + location.search));
        return;
      }
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
  // P = page reset (back to the race layout), M = next mindset: same backend
  // state the UDP Actions 4/2 drive, so every client (/ and /radio) stays in sync.
  document.addEventListener("keydown", function (ev) {
    if (ev.repeat || !document.hasFocus()) return;
    if (ev.code === "KeyP") sendCtl({ type: "page" });
    else if (ev.code === "KeyM") sendCtl({ type: "mindset" });
  });
  // Arrow Up/Down = menu up/down (UDP Actions 2/3), Enter = confirm,
  // Escape = close (docs/12). Space still confirms like UDP Action 1.
  var MENU_KEYS = { ArrowUp: "up", ArrowDown: "down", Enter: "confirm", Escape: "close" };
  document.addEventListener("keydown", function (ev) {
    if (ev.repeat || !document.hasFocus()) return;
    if (ev.code === "KeyV") {
      if (ev.target && ev.target.closest && ev.target.closest("input, textarea, select, button")) return;
      ev.preventDefault();
      sendCtl({ type: "voice" });
      return;
    }
    var op = MENU_KEYS[ev.code];
    if (!op) return;
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

  // Phone (docs/15 §3): compact one-column page with ARM PHONE RADIO. ?phone=1 forces it.
  var phoneQuery = matchMedia("(max-width: 600px)");
  function setPhone() {
    if (document.body.classList.contains("dash")) {
      document.body.classList.toggle("phone", /[?&]phone=1/.test(location.search) || phoneQuery.matches);
    }
  }
  if (phoneQuery.addEventListener) phoneQuery.addEventListener("change", setPhone);
  setPhone();

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
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  }

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
