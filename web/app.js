// pitwall dashboard client — vanilla, no deps. Layout/lifecycle: docs/15-dashboard-design.md.
(function () {
  "use strict";
  var STALE_MS = 1000, CONNECTING_MS = 3000, MAX_CALLS = 12;
  var IDLE_S = { 1: 30, 2: 20, 3: 20 };
  var COMPOUNDS = { 16: "SOFT", 17: "MEDIUM", 18: "HARD", 7: "INTER", 8: "WET" };
  var SC_WORDS = { 1: "SAFETY CAR", 2: "VSC", 3: "FORMATION" };
  var MENU_HEADS = {
    tyres: "TYRES GONE?", pit: "PIT NOW?", fight: "FIGHT",
    rain: "RAIN COMING?", push: "PUSH OR SAVE?", budget: "RADIO CALLS",
    understeer: "UNDERSTEER", oversteer: "OVERSTEER",
    mindset: "MINDSET", silent: "RADIO SILENT"
  };

  var ws = null, lastSeq = null, backoff = 500, mismatched = false;
  var SILENT = "data:audio/wav;base64," +
    "UklGRkQDAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YSADAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
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
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" +
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==";
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
    setClass("phase", "sess" + (p.phase === "out_lap" ? " warn" : ""));
    setText("session", (p.session_label || p.session_kind || "--").toUpperCase() + " · " +
      String(p.track || "--").toUpperCase());
    var total = p.total_laps && p.session_kind === "race" ? p.total_laps : null, left = lapsLeft(p, s);
    setText("lap", "LAP " + (p.lap_num || "--") + (total ? "/" + total : ""));
    setText("lap-left", left !== null ? "· " + left + " LEFT" : "");
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
      mind.textContent = String(p.mindset || "--").toUpperCase();
      mind.className = "pill" + (p.mindset === "aggressive" ? " aggr" : "");
    }
    var drv = el("drv");
    if (drv) {
      if (!p.driver) {
        drv.hidden = true;
      } else {
        drv.hidden = false;
        var neutral = p.driver.neutral !== undefined && p.driver.neutral !== null ?
          p.driver.neutral : 0.5;
        drv.className = "chip drv " + (p.driver.rating >= neutral ? "ok" : "low");
        drv.title = "driver rating " + p.driver.rating.toFixed(2) + " · confidence " +
          Math.round(p.driver.confidence * 100) + "% · " + p.driver.source;
        var srcIcon = p.driver.source === "global" ? "#ico-globe" : "#ico-track";
        drv.innerHTML = '<svg class="ico"><use href="#ico-wheel"/></svg>' +
          Math.round(p.driver.rating * 100) +
          '<svg class="ico src"><use href="' + srcIcon + '"/></svg>';
      }
    }
    renderLimits(p);
    var v = el("verbosity");
    if (v) {
      v.innerHTML = "";
      if (p.volume) {
        var volumeChip = span("", "v-chip vol");
        var volumeIcon = p.volume.label === "low" ? "ico-mute" :
          p.volume.label === "max" ? "ico-vol" : "ico-quiet";
        volumeChip.title = "volume " + p.volume.pct + "%";
        volumeChip.innerHTML = '<svg class="ico"><use href="#' + volumeIcon + '"/></svg>' +
          esc(String(p.volume.label).toUpperCase());
        v.appendChild(volumeChip);
      }
      if (p.verbosity && p.verbosity !== "normal") {
        var vc = span("", "v-chip");
        vc.title = "verbosity: " + p.verbosity;
        vc.innerHTML = '<svg class="ico"><use href="#ico-vol"/></svg>' +
          esc(String(p.verbosity).slice(0, 3).toUpperCase());
        v.appendChild(vc);
      }
      if (p.quiet) {
        var qc = span("", "v-chip quiet");
        qc.title = "quiet mode";
        qc.innerHTML = '<svg class="ico"><use href="#ico-quiet"/></svg>' +
          (p.quiet_left_s ? clock(p.quiet_left_s) : "");
        v.appendChild(qc);
      }
      if (p.silent) {
        var scChip = span("", "v-chip silent");
        scChip.title = "radio silent";
        scChip.innerHTML = '<svg class="ico"><use href="#ico-mute"/></svg>';
        v.appendChild(scChip);
      }
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

    var q = p.quali || null, cool = q ? q.cool : null;
    document.body.classList.toggle("quali", !!q && p.session_kind === "qualifying");
    document.body.classList.toggle("flying", p.phase === "flying");
    renderCool(cool, q);
    renderPitBoard(p.pit_board, q, p.phase, p.track_info || {});
    if (q) renderQZone(q, p);
    renderTyres(p, s);
    renderSetupStrip(p);
    renderFuel(p, s);
    renderBatt(p, s);
    renderPace(p);
    renderWeather(t, p);
    renderDamage(p.damage);
    renderCtx(p, s, t);

    setText("ers", "ERS " + fmt(p.ers_pct, 0) + "%");
    setText("sc", "SC " + sc);
    if (p.latency) {
      setText("latency", "queue " + p.latency.queue_count + " · oldest " +
        fmt(p.latency.queue_oldest_s, 1) + " s · TTS " + fmt(p.latency.tts_p99_ms, 0) +
        " ms · screen lag " + fmt(p.latency.packet_to_ws_p99_ms, 0) + " ms");
    }
  }

  // Laps left in a race: the strategy's laps_remaining, else total minus the current lap.
  function lapsLeft(p, s) {
    if (p.session_kind !== "race") return null;
    if (s && typeof s.laps_remaining === "number") return fmt(s.laps_remaining, 0);
    return p.total_laps && p.lap_num ? String(Math.max(0, p.total_laps - p.lap_num)) : null;
  }

  // Limits: a card in the TRACK group and a status chip (race and qualifying). Phone shows the chip only.
  function renderLimits(p) {
    var card = el("limits-card"), chip = el("limits"), inf = p.race && p.race.infringements;
    var shown = !!inf && (p.session_kind === "race" || p.session_kind === "qualifying");
    if (card) card.hidden = !shown;
    if (chip) chip.hidden = !shown;
    if (!shown) return;
    var cuts = Number(inf.corner_cuts) || 0;
    var cutWarns = Number(inf.cut_warns) || 0;
    var limitWarns = Number(inf.limit_warns) || 0;
    var penalty = Number(inf.penalty_s) || 0;
    var warnAt = Number(inf.warn_at);
    var crit = penalty > 0 || (warnAt > 0 && cuts > 0 && cuts % warnAt === 0);
    var warn = warnAt > 0 && cuts % warnAt === warnAt - 1;
    var notice = !crit && !warn && cuts > 0;
    var level = crit ? "crit" : warn ? "warn" : notice ? "notice" : "";
    var splitComplete = cutWarns + limitWarns >= cuts;
    if (chip) {
      chip.className = "chip limits" + (level ? " " + level : "");
      chip.title = splitComplete ? cutWarns + " corner cuts · " + limitWarns + " track limits" : cuts + " warnings";
      chip.textContent = (crit || warn ? "⚠ " : "") +
        (splitComplete ? "C" + cutWarns + " T" + limitWarns : "W" + cuts);
      if (penalty > 0) chip.appendChild(span("+" + penalty + "s", "pen"));
    }
    if (!card) return;
    card.className = "card limitcard " + level;
    setText("lim-cut", String(cutWarns));
    setText("lim-limit", String(limitWarns));
    setText("lim-warn", String(cuts));
    el("lim-split").hidden = !splitComplete;
    el("lim-total").hidden = splitComplete;
    var filled = warnAt > 0 ? (cuts > 0 && cuts % warnAt === 0 ? warnAt : cuts % warnAt) : 0;
    setText("lim-dots", warnAt > 0 ? "●".repeat(filled) + "○".repeat(warnAt - filled) : "");
    setText("lim-pen", penalty > 0 ? "+" + penalty + "s" : "");
  }

  // Tyres: trend arrow from the fast EMA (inner) vs tyre_inner_ema_slow, wear %,
  // window meter on a 40..120 °C scale with the band from tyre_window_c, ⚠ tag above 120 °C,
  // HOT WEAR rate, LOCKED border.
  var T_LO = 40, T_HI = 120, TREND_C = 0.5;
  function renderTyres(p, s) {
    var comp = COMPOUNDS[p.tyre_visual] || (p.tyre_visual ? "C" + p.tyre_visual : "--");
    var compEl = el("compound");
    if (compEl) {
      compEl.textContent = comp.charAt(0);
      compEl.title = comp;
      compEl.className = "cmp " + (/^[MHSIW]$/.test(comp.charAt(0)) ? comp.charAt(0) : "");
    }
    setText("tyre-age", p.tyre_age_laps !== undefined && p.tyre_age_laps !== null ?
      p.tyre_age_laps + "L OLD" : "--");
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
        pace.appendChild(document.createTextNode("~" + fmt(lop, 0) + "L " + (cls === "ok" ? "▬ " : "▼ ")));
        pace.appendChild(span(cls === "ok" ? "PLENTY" : cls === "warn" ? "SHRINKING" : "NEAR CLIFF"));
      }
    }
    if (!p.tyres) return;
    var win = p.tyre_window_c, tspan = T_HI - T_LO;
    var bandL = win ? Math.max(0, Math.min(1, (win[0] - T_LO) / tspan)) : null;
    var bandR = win ? Math.max(0, Math.min(1, (win[1] - T_LO) / tspan)) : null;
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
      c.querySelector(".st-word").textContent = String(t.status || "").toUpperCase();
      var d = t.inner !== null && t.tyre_inner_ema_slow !== null && t.tyre_inner_ema_slow !== undefined ?
        t.inner - t.tyre_inner_ema_slow : 0;
      var tr = c.querySelector(".trend");
      tr.className = "trend " + (d > TREND_C ? "up" : d < -TREND_C ? "down" : "flat");
      tr.textContent = d > TREND_C ? "▲" : d < -TREND_C ? "▼" : "▬";
      tr.title = d > TREND_C ? "heating" : d < -TREND_C ? "cooling" : "steady";
      var f = t.inner === null ? 0 : Math.max(0, Math.min(1, (t.inner - T_LO) / (T_HI - T_LO)));
      var meter = c.querySelector(".meter");
      meter.querySelector("i").style.setProperty("--f", f.toFixed(3));
      if (win) {
        c.style.setProperty("--band-left", (bandL * 100).toFixed(1) + "%");
        c.style.setProperty("--band-width", ((bandR - bandL) * 100).toFixed(1) + "%");
      } else {
        c.style.removeProperty("--band-left");
        c.style.removeProperty("--band-width");
      }
      c.querySelector(".lo").textContent = win ? fmt(win[0], 0) + "°" : "";
      c.querySelector(".hi").textContent = win ? fmt(win[1], 0) + "°" : "";
      var over = t.inner !== null && t.inner > T_HI;
      meter.classList.toggle("over", over);
      c.querySelector(".wear").className = "wear" + (t.wear >= 70 ? " crit" : t.wear >= 50 ? " warn" : "");
      c.querySelector(".wear b").textContent = fmt(t.wear, 0) + "%";
      c.querySelector(".hw").textContent = st.wear_hot_corner === k ?
        "HOT WEAR" + (st.wear_hot_rate ? " +" + fmt(st.wear_hot_rate, 1) + "%/lap" : "") : "";
      var brk = c.querySelector(".brk");
      if (brk) {
        var bv = p.brakes ? p.brakes[k] : null;
        brk.textContent = bv !== null && bv !== undefined ? "BRK " + fmt(bv, 0) + "°" : "";
        brk.className = "brk" + (bv >= 1000 ? " crit" : bv >= 850 ? " warn" : bv !== null && bv < 200 && p.phase === "on_track" ? " cold" : "");
      }
      var tags = c.querySelector(".tags");
      tags.innerHTML = "";
      if (t.lockup) tags.appendChild(span("LOCKED", "tag lock"));
      if (over) tags.appendChild(span("⚠ >" + T_HI + "°", "tag crit"));
      if (k === worst) {
        if (st.overheat) tags.appendChild(span("OVERHEAT", "tag crit"));
        if (st.blister_max_pct >= 5) tags.appendChild(span("BLISTER " + fmt(st.blister_max_pct, 0) + "%", "tag warn"));
        if (st.graining) tags.appendChild(span("GRAINING", "tag warn"));
      }
    });
  }

  // BB / DIFF: live values from the setup packet and advice from the pit board or track payload.
  function renderSetupStrip(p) {
    var vals = p.setup ? p.setup.values || {} : {};
    var advice = p.pit_board ? (p.pit_board.setup_advice || []) : (p.setup_live || []);
    function one(id, field, sug) {
      var v = vals[field], rec = null;
      advice.forEach(function (r) { if (r.fields.indexOf(field) >= 0) rec = r; });
      setText(id, v === undefined || v === null ? "--" : fmt(v, 0) + "%");
      var suited = rec && v !== undefined && Number(v) === Number(rec.to);
      setClass(id, "v" + (suited ? " suited" : ""));
      if (!sug) return;
      if (!rec || suited) { setText(sug, ""); setClass(sug, "sug none"); return; }
      setText(sug, (rec.to > v ? "▲ " : "▼ ") +
        (rec.tier === "experiment" ? "TRY " : "") + fmt(rec.to, 0));
      setClass(sug, "sug" + (rec.tier === "experiment" ? " try" : ""));
    }
    one("su-bb", "brake_bias", "su-bb-sug");
    one("su-on", "on_throttle", "su-on-sug");
    one("su-off", "off_throttle", null);
  }

  // Fuel at the flag: signed laps spare (+) or short (-).
  function renderFuel(p, s) {
    var fdl = s ? s.fuel_delta_laps : null, game = fdl === null || fdl === undefined;
    var d = game ? p.fuel_remaining_laps : fdl;
    setText("fuel", d === null || d === undefined ? "--" : (d >= 0 ? "+" : "−") + fmt(Math.abs(d), 1));
    setClass("fuel", "hero" + (d === null || d === undefined ? "" : d < 0 ? " crit" : d < 0.5 ? " warn" : " ok"));
  }

  var BATT_WORDS = { on_budget: "ON BUDGET", over: "OVER · SAVE", under: "UNDER · CHARGING", attack_ok: "SPEND · ATTACK OK" };
  var BATT_ICONS = { on_budget: "M13 2 4 14h7l-1 8 9-12h-7z", attack_ok: "M13 2 4 14h7l-1 8 9-12h-7z",
    over: "M12 3c1 4 5 5 5 10a5 5 0 0 1-10 0c0-3 2-4 2-7 2 1 3 2 3 4",
    under: "M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0zM12 18v4" };
  function renderBatt(p, s) {
    var e = s && s.energy ? s.energy : null, mode = e ? e.mode : null;
    setClass("batt", "card carcard battcard" + (mode ? " " + mode : ""));
    setText("batt-pct", fmt(p.ers_pct, 0) + "%");
    var m = el("batt-mode");
    if (m && m.dataset.mode !== String(mode)) {
      m.dataset.mode = String(mode);
      m.innerHTML = mode && BATT_ICONS[mode] ?
        '<svg class="ico" viewBox="0 0 24 24"><path d="' + BATT_ICONS[mode] + '"/></svg> ' : "";
      m.appendChild(document.createTextNode(BATT_WORDS[mode] || "no budget yet"));
    }
    var sub = el("batt-sub");
    if (sub) {
      var floor = !!e && e.laps_to_floor !== null && e.laps_to_floor !== undefined;
      sub.hidden = !floor;
      if (floor) sub.textContent = "FLOOR " + fmt(e.laps_to_floor, 0) + "L";
    }
  }

  // PACE: the newest lap against the one before, with the sector deltas.
  function renderPace(p) {
    var r = p.race || {}, laps = Array.isArray(r.recent_laps) ? r.recent_laps : [];
    var newer = laps.length ? laps[laps.length - 1] : null;
    var older = laps.length > 1 ? laps[laps.length - 2] : null;
    setText("pace-last", newer ? lapTime(newer.time_ms) : "--");
    setText("pace-vs", newer ? "L" + newer.lap + (older ? " VS L" + older.lap : "") : "no lap yet");
    var lapDelta = older && newer ? newer.time_ms - older.time_ms : null;
    setText("pace-delta", lapDelta === null ? "--" : signed(lapDelta));
    setClass("pace-delta", "big" + (lapDelta < 0 ? " ok" : lapDelta > 0 ? " warn" : ""));
    for (var sector = 0; sector < 3; sector++) {
      var before = older && older.sectors_ms ? older.sectors_ms[sector] : null;
      var after = newer && newer.sectors_ms ? newer.sectors_ms[sector] : null;
      var delta = typeof before === "number" && before !== 0 &&
        typeof after === "number" && after !== 0 ? after - before : null;
      var id = "pace-s" + (sector + 1);
      setText(id, delta === null ? "--" : signed(delta));
      setClass(id, delta < 0 ? "ok" : delta > 0 ? "warn" : "");
    }
  }

  // Weather: icon + rain chance per forecast slot (now / 10 / 30 min). The crossover
  // line names the compound and the lap it is expected on (lap and pace known).
  var WEATHER_WORDS = { 0: "CLEAR", 1: "LIGHT CLOUD", 2: "OVERCAST", 3: "LIGHT RAIN", 4: "HEAVY RAIN", 5: "STORM" };
  var CROSS = { to_inter: ["INTER", "inter", "warn"], to_wet: ["WET", "wet", "cold"], to_dry: ["SLICK", "medium", "warn"] };
  var CROSS_PLURAL = { to_inter: "INTERS", to_wet: "WETS", to_dry: "SLICKS" };
  var SLOT_MIN = [0, 10, 30];
  function crossChip(c) { return c === "to_dry" ? span("SLICK", "tagw") : compChip(CROSS[c][0]); }
  function crossoverLap(cm, p) {
    if (cm === null || cm === undefined || !p || !p.lap_num) return null;
    var baseMs = (p.race && p.race.base_pace_ms > 0 ? p.race.base_pace_ms : 90000);
    var lapsOut = Math.max(0, Math.round((cm * 60000) / baseMs));
    var target = p.lap_num + lapsOut;
    if (p.total_laps && target > p.total_laps) return null;
    return target;
  }
  function renderWeather(t, p) {
    var wf = t.weather_forecast || [t.weather, -1, -1], r = t.rain_chance_pct || [null, null, null];
    var c = CROSS[t.weather_crossover] ? t.weather_crossover : null, cm = t.weather_crossover_min;
    var cLap = c ? crossoverLap(cm, p) : null;
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
      n.querySelector(".k").textContent = i === 0 ? "NOW" : SLOT_MIN[i] + " MIN";
      n.querySelector("use").setAttribute("href", "#wx-" + (!known || w < 3 ? 2 : w < 4 ? 3 : 4));
      n.querySelector(".pct").textContent = known && pct !== null && pct !== undefined ? pct + "%" : "--";
    }
    var x = el("cross");
    if (!x) return;
    x.innerHTML = "";
    if (!c) { x.className = "cross"; x.textContent = "✓ no crossover"; return; }
    x.className = "cross " + CROSS[c][2];
    x.appendChild(crossChip(c));
    if (cLap) x.appendChild(span("~L" + cLap, "lap"));
    x.appendChild(span(CROSS_PLURAL[c] + (cm !== null && cm !== undefined ? " IN " + fmt(cm, 0) + " MIN" : " SOON"), "tagw"));
  }

  // Damage: front wings and floor as numbers; other parts and faults in one compact line.
  // Amber from 5 %, red from 25 %.
  var DMG_MORE = [["rear_wing", "RW"], ["diffuser", "DIFF"], ["sidepod", "SIDEPOD"], ["gearbox", "GEARBOX"], ["engine", "ENGINE"]];
  function dmgClass(v) { return v >= 25 ? "crit" : v >= 5 ? "warn" : "ok"; }
  function dmgNum(id, v) {
    var n = el(id);
    if (!n) return;
    n.textContent = v === null || v === undefined ? "--" : fmt(v, 0);
    n.className = "mid " + (v === null || v === undefined ? "" : dmgClass(v));
  }
  function renderDamage(d) {
    if (!d) return;
    dmgNum("dm-fwl", d.front_left_wing);
    dmgNum("dm-fwr", d.front_right_wing);
    dmgNum("dm-floor", d.floor);
    var more = el("dm-more");
    if (!more) return;
    more.innerHTML = "";
    var parts = [];
    DMG_MORE.forEach(function (pair) {
      var val = d[pair[0]];
      if (val > 0) parts.push(span(pair[1] + " " + fmt(val, 0) + "%", "n " + dmgClass(val)));
    });
    if (d.drs_fault) parts.push(span("DRS FAULT", "n crit"));
    if (d.ers_fault) parts.push(span("ERS FAULT", "n crit"));
    if (!parts.length) more.appendChild(span("✓ none", ""));
    parts.forEach(function (x, i) {
      if (i) more.appendChild(document.createTextNode("  "));
      more.appendChild(x);
    });
  }

  // Small DOM helpers for the contextual cards.
  var SVG_NS = "http://www.w3.org/2000/svg";
  function icon(name, cls) {
    var n = document.createElementNS(SVG_NS, "svg");
    n.setAttribute("class", cls || "i");
    var u = document.createElementNS(SVG_NS, "use");
    u.setAttribute("href", "#" + name);
    n.appendChild(u);
    return n;
  }
  function node(cls, items) {
    var n = span("", cls, "div");
    (items || []).forEach(function (x) {
      if (x) n.appendChild(typeof x === "string" ? document.createTextNode(x) : x);
    });
    return n;
  }
  function setCard(n, cls, items) {
    n.className = cls;
    n.innerHTML = "";
    (items || []).forEach(function (x) {
      if (x) n.appendChild(typeof x === "string" ? document.createTextNode(x) : x);
    });
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (ch) { return "&#" + ch.charCodeAt(0) + ";"; }); }
  function secSigned(v) { return (v >= 0 ? "+" : "−") + fmt(Math.abs(v), 1); }

  // Contextual column: safety, box, reply, info, then RACE (or the quali zone).
  function renderCtx(p, s, t) {
    var sc = p.safety_car || 0, scLive = sc === 1 || sc === 2, c = CROSS[t.weather_crossover] ? t.weather_crossover : null;
    var cm = t.weather_crossover_min;
    renderSafety(p, s, t, sc, c, cm);
    var ap = activePlan(s), lapNow = p.lap_num || 0, win = ap && ap.window;
    var inRacePit = !!(p.pit_board && !p.pit_board.show_release && p.phase !== "garage");
    var boxNow = !!(s && ((win && lapNow >= win[0] && lapNow <= win[1]) ||
      (s.plans || []).some(function (x) { return x.kind === "reactive" && scLive; })));
    renderBox(p, s, ap, inRacePit, boxNow);
    renderReplyCard();
    renderBattle(s, p.position);
    renderPlans(s, scLive);
    renderInfo(s, t);
  }

  // SAFETY: red flag, SC / VSC, rain crossover, blue flag (v2 priority order).
  function renderSafety(p, s, t, sc, c, cm) {
    var box = el("c-safety");
    if (!box) return;
    var rain = !!c && cm !== null && cm !== undefined && cm <= 5;
    box.hidden = !(p.red_flag || sc || rain || t.blue_flag);
    if (box.hidden) return;
    if (p.red_flag) {
      setCard(box, "card act stop", [icon("tgt", "i l"), span("RED FLAG", "huge"), span("BOX · FREE TYRES", "mid")]);
    } else if (sc) {
      setCard(box, "card act sc", [
        icon("warn", "i l"),
        span(sc === 1 ? "SC" : sc === 2 ? "VSC" : "FORMATION", "huge"),
        t.sc_laps ? span("LAP " + t.sc_laps, "tagw") : null,
        span("", "sp"),
        s && s.pit_loss_s ? span("PIT LOSS", "tagw") : null,
        s && s.pit_loss_s ? span(fmt(s.pit_loss_s, 0) + "s", "big") : null
      ]);
    } else if (rain) {
      var pct = t.weather_crossover_pct;
      setCard(box, "card act cold", [
        icon("rain2", "i l"), crossChip(c), span(fmt(cm, 0) + " MIN", "huge"),
        span("", "sp"),
        pct !== null && pct !== undefined ? span(fmt(pct, 0) + "%", "big") : null
      ]);
    } else {
      setCard(box, "card act cold", [icon("tgt", "i l"), span("BLUE", "huge"), span("LET LEADER BY", "mid")]);
    }
  }

  // Exit reading after a stop: the rival name and gap, or clean.
  function exitNode(ex) {
    if (!ex) return null;
    var gap = ex.rival && ex.rival.gap_s !== null && ex.rival.gap_s !== undefined ? " " + secSigned(ex.rival.gap_s) + " s" : "";
    if (ex.rival && ex.rival.name) {
      return span((ex.clean ? "✓ clear of " : "⚠ traffic ") + ex.rival.name + gap, ex.clean ? "ok" : "warn", "b");
    }
    if (ex.clean === true) return span("✓ clean exit", "ok", "b");
    return null;
  }
  function ucOcNodes(s) {
    var out = [];
    if (s.undercut_s) out.push(icon("uc", "i s"), span("UC " + secSigned(s.undercut_s) + " s"));
    if (s.overcut_s) out.push(icon("oc", "i s"), span("OC " + secSigned(s.overcut_s) + " s"));
    return out;
  }

  // BOX: the stop window, the fitted compound, pit loss and the exit. A wrong tyre fitted
  // shows only after the stop (fit_mismatch). The mock-up FIT chip is the planned tyre.
  function renderBox(p, s, ap, inRacePit, boxNow) {
    var box = el("c-box");
    if (!box) return;
    var fit = s && s.fit_mismatch, next = s && s.plan_next_compound;
    var show = (boxNow || inRacePit || !!fit) && !p.red_flag;
    box.hidden = !show;
    if (!show) return;
    var win = ap && ap.window;
    var head = fit ? [span("FITTED "), compChip(fit.fitted), span(" NOT "), compChip(fit.expected)] :
      [span(inRacePit ? "IN PIT LANE" : "BOX NOW" + (ap ? " · PLAN " + ap.id : ""), "huge")];
    var line = fit ? "Plan " + fit.plan + " expected " + String(fit.expected).toLowerCase() + " · new plan from next lap" :
      (win ? "WINDOW L" + win[0] + "–" + win[1] : "");
    var lossed = s && s.pit_loss_s ? [icon("ico-lap", "i s"), span(fmt(s.pit_loss_s, 0) + " s", "mid")] : null;
    setCard(box, "card act " + (fit ? "stop" : "box"), [
      icon("box", "i l"),
      node("col", [node("row", head), span(line, "tagw")]),
      span("", "sp"),
      !fit && next ? compChip(next) : null,
      lossed ? node("row", lossed) : null,
      s ? exitNode(s.pit_exit) : null
    ]);
  }

  function renderReplyCard() {
    var replyCall = null;
    for (var i = calls.length - 1; i >= 0; i--) {
      var rc = calls[i];
      if (rc.audio !== "dropped" && (rc.reply || (rc.tags && rc.tags.indexOf("menu_answer") !== -1))) {
        replyCall = rc;
        break;
      }
    }
    var showReply = !!(replyCall && (ageS(replyCall) || 0) < 15);
    var replyKey = showReply && String(replyCall.rule_id || "").indexOf("menu:") === 0 ?
      replyCall.rule_id.slice(5) : "";
    var replyHead = showReply ? (replyCall.reply && replyCall.reply.head) ||
      MENU_HEADS[replyKey] || (replyKey ? replyKey.replace(/_/g, " ").toUpperCase() : "REPLY") : "";
    var replyLine = showReply ? (replyCall.reply && replyCall.reply.line) || replyCall.text || "" : "";
    var n = el("c-reply");
    if (!n) return;
    n.hidden = !showReply;
    if (showReply) setCard(n, "card act reply", [icon("reply", "i"), node("col", [span(replyHead, "mid"), span(replyLine, "reply-line")])]);
  }

  function renderInfo(s, t) {
    var info = el("c-info"), msg = null;
    if (s && s.ahead && s.ahead.pitted) msg = "◦ INFO · " + String(s.ahead.name || "car ahead") + " pitted";
    else if (s && s.behind && s.behind.pitted) msg = "◦ INFO · " + String(s.behind.name || "car behind") + " pitted";
    else if (t.penalty_s) msg = "◦ INFO · penalty +" + t.penalty_s + " s" + (t.unserved ? " · " + t.unserved + " unserved" : "");
    if (!info) return;
    info.hidden = !msg;
    if (msg) setCard(info, "card info", [icon("pen", "i s"), msg]);
  }

  // Plan C (kind "reactive") is sent every tick; show it only under SC/VSC or once it is the active plan.
  // Under live SC/VSC plan C comes first with an amber border. The active plan keeps its thick border.
  function renderPlans(s, scLive) {
    var box = el("plans");
    if (!box) return;
    box.innerHTML = "";
    var all = s ? s.plans || [] : [];
    var react = all.filter(function (pl) { return pl.kind === "reactive" && (pl.active || scLive); });
    var normal = all.filter(function (pl) { return pl.kind !== "reactive"; });
    var plans = react.concat(normal);
    var focusReactive = scLive && react.length > 0;
    if (!plans.length) {
      box.appendChild(node("card dim1", [s ? s.stint_plan || "no stop planned" : "--"]));
      return;
    }
    var ap = activePlan(s);
    plans.forEach(function (pl) {
      if (pl.kind === "reactive") {
        var saving = typeof pl.delta_s === "number" && pl.delta_s < 0;
        box.appendChild(node("card plan" + (focusReactive ? " focus" : ""), [
          node("row", [
            icon("box", "i s warn"), span(pl.id, "mid"), span("BOX NOW", "warn"),
            pl.compounds && pl.compounds.length ? compChip(pl.compounds[0]) : null,
            span("", "sp"),
            saving ? span("SAVES", "tagw") : span("cheap stop", "tagw"),
            saving ? span(fmt(-pl.delta_s, 1) + " s", "big ok") : null
          ])
        ]));
        return;
      }
      var seq = node("seq", []);
      (pl.compounds || []).forEach(function (cp, i) {
        if (i) seq.appendChild(span("›", "arr"));
        seq.appendChild(compChip(cp));
      });
      if (pl === ap) {
        var target = s.plan_target_lap;
        var w = pl.window;
        box.appendChild(node("card plan active", [
          node("row", [
            icon("plan", "i"), span(pl.id, "mid"), seq, span("", "sp"),
            span(s.on_plan === false ? "✗ OFF PLAN " + fmt(s.plan_off_s, 1) + " s" : "✓ ON PLAN",
              "onp" + (s.on_plan === false ? " off" : ""))
          ]),
          target || w ? node("row sub", [
            icon("tgt", "i s warn"),
            target ? span("BOX", "tagw") : null,
            target ? span("L" + target, "big") : null,
            span("", "sp"),
            w ? span("WINDOW", "tagw") : null,
            w ? span(w[0] + "–" + w[1], "mid") : null
          ]) : null,
          s.pit_exit || s.undercut_s || s.overcut_s ? node("row sub alt", [
            icon("exit", "i s"), exitNode(s.pit_exit), span("", "sp")
          ].concat(ucOcNodes(s))) : null
        ]));
        return;
      }
      var stops = pl.stops === 1 ? "1 STOP" : pl.stops + " STOPS";
      box.appendChild(node("card plan dim1 row", [
        icon("plan", "i s"), span(pl.id, "mid"), seq, span("", "sp"),
        span(stops, "tagw"),
        pl.delta_s === null || pl.delta_s === undefined ? null : span(secSigned(pl.delta_s), "mid")
      ]));
    });
    var hasExit = !!(s && s.pit_exit && (s.pit_exit.rival || s.pit_exit.clean !== undefined));
    var hasUc = !!(s && (s.undercut_s || s.overcut_s));
    if (!ap && (hasExit || hasUc)) {
      box.appendChild(node("card dim1 row", [icon("exit", "i s"), exitNode(s.pit_exit), span("", "sp")].concat(ucOcNodes(s))));
    }
  }

  function activePlan(s) {
    var ps = s && s.plans ? s.plans : [];
    for (var i = 0; i < ps.length; i++) if (ps[i].active) return ps[i];
    return null;
  }

  function span(txt, cls, tag) {
    var n = document.createElement(tag || "span");
    n.textContent = txt;
    if (cls) n.className = cls;
    return n;
  }
  // Compound circle: M, H, S, I, W. Other names (C-numbered slicks) show their label.
  function compChip(c) {
    var w = String(c || "?"), letter = /^c\d/i.test(w) ? w.toUpperCase() : w.charAt(0).toUpperCase();
    return span(letter, "cmp " + (/^[MHSIW]$/.test(letter) ? letter : ""));
  }

  // Battle (docs/18): the active rival is bright with a thick border, the other car dims.
  // Defending or under threat makes BEHIND the bright card. A result (passed, held, lost)
  // fills the bright card for as long as the backend keeps battle.result (about 20 s).
  var MODE_WORDS = { free_air: "FREE AIR", catching: "CATCHING", attacking: "ATTACKING",
    defending: "DEFENDING", under_threat: "UNDER THREAT", managing: "MANAGING" };
  var MODE_CLS = { catching: "catching", attacking: "attack", defending: "threat", under_threat: "threat" };
  var MODE_ICON = { catching: "attack", attacking: "attack", defending: "defend", under_threat: "defend" };
  var railCache = {}, resultRival = { result: "", name: "" };
  function syncChildren(parent, kids) {
    for (var i = 0; i < kids.length; i++) {
      var cur = parent.childNodes[i], next = kids[i];
      if (cur === next) continue;
      if (!cur) parent.appendChild(next);
      else if (kids.indexOf(cur, i + 1) >= 0) parent.insertBefore(next, cur);
      else parent.replaceChild(next, cur);
    }
    while (parent.childNodes.length > kids.length) {
      parent.removeChild(parent.lastChild);
    }
  }
  function gapStr(g, side) { return g === null || g === undefined ? "--" : (side === "ahead" ? "+" : "−") + fmt(Math.abs(g), 1); }
  function rail(g, side, train, key) {
    var r = key ? railCache[key] : null;
    if (!r) {
      r = span("", "", "div");
      r.appendChild(span("", "rz", "div"));
      r.appendChild(span("", "rc", "div"));
      if (key) railCache[key] = r;
    }
    r.className = "rail " + side + (train ? " train" : "");
    r.lastChild.style.setProperty("--g", Math.max(0, Math.min(1, g / 3)).toFixed(3));
    return r;
  }
  function carRow(label, r, gap, ot) {
    return node("rv", [
      node("name", [
        span(label, "pos"), span(String(r.name || "--").toUpperCase(), "nm"),
        r.compound ? compChip(r.compound) : null,
        r.tyre_age_known && typeof r.tyre_age === "number" ? span(r.tyre_age + "L", "age") : null,
        ot ? span("OV", "tag crit") : null
      ]),
      span(gap, "gap")
    ]);
  }
  function paceRow(b, side, r) {
    var closing = side === "ahead" ? b.closing_ahead_s : b.closing_behind_s;
    var prob = side === "ahead" ? b.pass_prob : b.hold_prob;
    var ld = r && typeof r.last_lap_delta_s === "number" ? r.last_lap_delta_s : null;
    if (!closing && (prob === null || prob === undefined) && ld === null) return null;
    var items = [];
    if (closing) items.push(icon("close", "i s"), span(fmt(closing, 1) + " / LAP", side === "ahead" ? "ok" : "crit"));
    if (ld !== null) items.push(span("LAST", "tagw"), span((ld < 0 ? "−" : "+") + fmt(Math.abs(ld), 1), ld < 0 ? "ok" : "warn"));
    if (prob !== null && prob !== undefined) {
      var ring = span("", "ring", "span");
      ring.style.setProperty("--p", (100 * prob).toFixed(0) + "%");
      items.push(node("prob", [ring, span(fmt(100 * prob, 0) + "%"), span(side === "ahead" ? "PASS" : "HOLD", "tagw")]));
    }
    return node("pace", items);
  }
  function renderBattle(s, pos) {
    var grp = el("race-grp"), train = el("train"), b = s && s.battle;
    if (!grp) return;
    grp.hidden = !(b && b.mode);
    if (!b || !b.mode) return;
    var mode = b.mode, result = b.result || "";
    var resultSide = result === "lost" ? "ahead" : result === "passed" || result === "held" ? "behind" :
      result === "swap_ahead" ? "ahead" : result === "swap_behind" ? "behind" : null;
    var active = MODE_CLS[mode] ? (mode === "defending" || mode === "under_threat" ? "behind" : "ahead") : null;
    var bright = resultSide || active || (result === "scrap" ? "ahead" : null);
    var trains = [];
    [["ahead", b.ahead2, s.ahead], ["behind", b.behind2, s.behind]].forEach(function (x) {
      var far = x[1], near = x[2];
      if (!far || !near || far.gap_s === null || near.gap_s === null || Math.abs(far.gap_s - near.gap_s) > 1) return;
      trains.push(node("card far row", [
        icon(x[0] === "ahead" ? "up" : "down", "i s"),
        carRow(far.pos ? "P" + far.pos : "", far, gapStr(far.gap_s, x[0]), false),
        span("IN TRAIN", "tagw")
      ]));
    });
    syncChildren(train, trains);
    train.hidden = !trains.length;
    var nowName = resultSide && s[resultSide] ? String(s[resultSide].name || "--").toUpperCase() : "";
    if (resultRival.result !== result) resultRival = { result: result, name: nowName };
    if (!resultRival.name) resultRival.name = nowName;
    var rivalName = resultRival.name;
    var swaps = b.swaps ? b.swaps + (b.swaps === 1 ? " SWAP" : " SWAPS") : "";
    [["ahead", el("card-ahead")], ["behind", el("card-behind")]].forEach(function (x) {
      var sd = x[0], n = x[1], r = s[sd];
      n.hidden = !r;
      if (!r) return;
      var isBright = sd === bright;
      var modeNode = null, filled = "";
      if (isBright && (result === "passed" || result === "held" || result === "lost")) {
        var word = result === "passed" ? "PASSED " : result === "held" ? "HELD " : "LOST TO ";
        modeNode = node("mode-row", [icon(result === "lost" ? "down" : "defend", "i s"), span(word + rivalName)]);
        filled = result === "lost" ? " filled-crit" : " filled-ok";
      } else if (isBright && (result === "swap_ahead" || result === "swap_behind")) {
        modeNode = node("mode-row", [span("TRADING PLACES"), span("· " + rivalName + " " + swaps, "sub")]);
      } else if (isBright && result === "scrap") {
        modeNode = node("mode-row", [span("SCRAP"), span(swaps, "sub")]);
      } else if (isBright) {
        var laps = mode === "catching" && b.catch_laps ? "IN RANGE IN " + fmt(b.catch_laps, 0) + " LAPS" :
          mode === "under_threat" && b.threat_laps ? "IN RANGE IN " + fmt(b.threat_laps, 0) + " LAPS" :
          b.mode_laps ? b.mode_laps + (b.mode_laps === 1 ? " LAP" : " LAPS") : "";
        var uc = s.undercut_s && s.undercut_s > 0 ? "UNDERCUT " + secSigned(s.undercut_s) + " s" :
          s.overcut_s && s.overcut_s > 0 ? "OVERCUT " + secSigned(s.overcut_s) + " s" : "";
        modeNode = node("mode-row " + (MODE_CLS[mode] || ""), [
          icon(MODE_ICON[mode] || "attack", "i s"), span(MODE_WORDS[mode] || mode.toUpperCase()),
          laps ? span(laps, "sub") : null, uc ? span(uc, "uc-pill") : null
        ]);
      } else if (sd === "ahead" && (mode === "free_air" || mode === "managing")) {
        modeNode = node("mode-row", [span(MODE_WORDS[mode])]);
      }
      var g = gapStr(r.gap_s, sd);
      var kids = [modeNode, carRow(r.pos ? "P" + r.pos : "", r, g, r.overtake && sd === "behind")];
      if (r.gap_s !== null && r.gap_s !== undefined && r.gap_s <= 3) kids.push(rail(r.gap_s, sd, false, sd));
      kids.push(paceRow(b, sd, r));
      n.className = "card bcard " + sd + (isBright ? " bright" : " dim") + filled +
        (isBright && MODE_CLS[mode] ? " " + MODE_CLS[mode] : "");
      syncChildren(n, kids.filter(function (k) { return k; }));
    });
  }

  // Qualifying zone: phase strip, cut-off, pole and teammate sectors, this run.
  function qRow(id, gaps, worst, lapGap) {
    var row = el(id);
    if (!row) return;
    var tds = row.querySelectorAll("td");
    for (var i = 0; i < 3; i++) {
      var v = gaps ? gaps[i] : 0;
      tds[i].textContent = v ? signed(v) : "--";
      tds[i].className = (v < 0 ? "ok" : v > 0 ? "crit" : "") + (i === worst && v > 0 ? " worst" : "");
    }
    if (tds[3]) {
      tds[3].textContent = lapGap !== null && lapGap !== undefined && lapGap !== 0 ? signed(lapGap) : "--";
      tds[3].className = lapGap < 0 ? "ok" : lapGap > 0 ? "crit" : "";
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
    setClass("q-margin", "big" + (has && !through ? " crit" : has ? " ok" : ""));
    var cutLine = el("q-cut-line");
    if (cutLine) {
      cutLine.className = "qcut-grid";
      cutLine.innerHTML = "";
      [
        [q.margin_kind === "pole" ? "TARGET" : "PRED CUT", q.margin_kind === "pole" ? "vs P2" : lapTime(q.cutoff_ms)],
        ["YOUR BEST", lapTime(q.best_lap_ms)],
        ["TIME LEFT", clock(q.session_time_left)],
        ["FRESH SETS", (q.fresh_sets !== null && q.fresh_sets !== undefined ? q.fresh_sets : "--") +
          (q.fresh_sets === 1 ? " set" : " sets")]
      ].forEach(function (pair) {
        var cell = span("", "qcut-cell", "div");
        cell.appendChild(span(pair[0], "k"));
        cell.appendChild(span(pair[1], "v"));
        cutLine.appendChild(cell);
      });
    }
    var pole = q.pole || (q.cool && q.cool.pole) || null;
    setText("q-pole-who", pole && pole.driver ? pole.driver : "--");
    var gaps = pole ? pole.sector_gaps_ms : null;
    qRow("q-gaps", gaps, pole && pole.worst_sector ? pole.worst_sector - 1 : -1, pole ? pole.gap_ms : null);
    var mate = q.teammate || null;
    setText("q-mate-who", mate && mate.driver ? mate.driver : "--");
    qRow("q-mate-gaps", mate ? mate.sector_gaps_ms : null, mate && mate.worst_sector ? mate.worst_sector - 1 : -1, mate ? mate.gap_ms : null);
    var head = "--", line = "no run plan yet", lap = q.lap, tone = "proj", delta = "";
    var runCard = el("q-run");
    if (q.release) {
      var r = q.release;
      head = r.time_for_out_lap === false ? "✗ STAY IN" : r.clean ? "✓ GO NOW" : r.wait_s !== null ? "WAIT " + fmt(r.wait_s, 0) + " s" : "⚠ TRAFFIC";
      tone = r.time_for_out_lap === false ? "stop" : r.clean ? "go" : r.wait_s !== null ? "wait" : "stop";
      line = r.cars_on_track + " on track" + (r.gap_ahead_s !== null ? " · " + fmt(r.gap_ahead_s, 0) + " s clear ahead" : "");
    } else if (lap) {
      head = lap.abort ? "✗ ABORT" : "PROJ " + lapTime(lap.projected_ms);
      tone = lap.abort ? "stop" : "proj";
      delta = lap.delta_ms === null ? "" : signed(lap.delta_ms);
      line = (lap.abort ? "PROJ " + lapTime(lap.projected_ms) : "") + (delta ? "" : (lap.abort ? " · " : "") + "no cut-off yet");
    } else if (q.plan) {
      head = "PLAN " + String(q.plan.plan).toUpperCase();
      line = q.plan.reason || "";
    }
    if (runCard) runCard.className = "card act " + tone;
    setText("q-run-head", head);
    setText("q-run-delta", delta);
    setText("q-run-tag", delta ? "TO CUT" : "");
    setText("q-run-line", line);
  }
  var SPEAK_ICON = '<svg class="ico"><use href="#ico-speak"/></svg>';
  function renderVoice(v) {
    var pill = el("voice"), tag = el("drvmenu-voice");
    var listening = voiceListening && v && v.listening;
    var flash = voiceFlash && voiceFlash.until > Date.now() ? voiceFlash : null;
    if (pill) {
      if (listening) {
        pill.hidden = false;
        pill.className = "voice" + (v.warn ? " warn" : "");
        pill.innerHTML = SPEAK_ICON;
        if (v.left_s !== null && v.left_s !== undefined) {
          pill.appendChild(document.createTextNode(Math.ceil(v.left_s) + "s"));
        }
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

  function renderCorner(k, t, win) {
    var node = el("pb-" + k);
    if (!node) return;
    var move = node.querySelector(".pb-move"), psi = node.querySelector(".pb-psi"),
      temp = node.querySelector(".pb-temp");
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
    var avg = t.avg_c;
    temp.textContent = avg !== null && avg !== undefined ? Math.round(avg) + "°" : "--";
    temp.className = "pb-temp" + (avg === null || avg === undefined || !win ? "" :
      avg > win[1] ? " hot" : avg < win[0] ? " cold" : " ok");
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
  function changeRow(list, done, name, from, to, why, tier) {
    var li = document.createElement("li");
    li.className = (done ? "done" : "todo") + (tier === "experiment" ? " try" : "");
    var parts = [span(done ? "☑" : "☐", "box")];
    parts.push(tier === "experiment" ? span("TRY", "tag try") : span("", "tag spacer"));
    parts.push(span(name, "nm"),
      span(done ? to + " ✓" : from + " → " + to, "val"), span(why || "", "why"));
    parts.forEach(function (n) { li.appendChild(n); });
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
      if (rec.tier === "experiment") return rec.reason;
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
          fmt(rec.from, 1), fmt(rec.to, 1), why(rec), rec.tier);
        return;
      }
      var f = setupFormat(rec.fields[0]);
      var name = rec.fields.map(function (field) {
        var ff = setupFormat(field);
        return ff ? ff[1] : field.replace(/_/g, " ");
      }).join(" + ");
      changeRow(change, matchesTarget(rec, setup[rec.fields[0]]), name,
        setupValue(f, rec.from), setupValue(f, rec.to), why(rec), rec.tier);
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
          var c = span("", "c" + (rec && !matchesTarget(rec, v) ?
            (rec.tier === "experiment" ? " try" : " todo") : "") +
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

  function renderPitBoard(b, q, phase, t) {
    var box = el("pitboard");
    if (!box) return;
    box.hidden = !b;
    document.body.classList.toggle("pit", !!b);
    document.body.classList.toggle("race-pit", !!(b && !b.show_release && phase !== "garage"));
    if (!b) return;
    box.querySelector(".pb-head").hidden = !b.show_release;
    var rel = releaseState(q, phase);
    setText("pb-release", rel.text);
    setClass("pb-release", "pb-release " + rel.cls);
    setText("pb-release-sub", rel.sub || (b.has_advice ? "" : "no run data yet"));
    var win = b.tyre_window_c;
    setText("pb-press-note", b.has_advice || b.tyres.fl.avg_c !== null ? "· avg on flying laps" +
      (win ? " · target " + fmt(win[0], 0) + "–" + fmt(win[1], 0) + "°" : "") : "· no flying laps this run");
    CORNERS.forEach(function (k) { renderCorner(k, b.tyres[k], win); });
    var plan = q && q.plan && q.plan.plan;
    pbTile("pb-plan", !!plan, plan === "box" || plan === "cool" ? "warn" : "ok",
      PLAN_TEXT[plan] || String(plan).toUpperCase(), plan ? PLAN_WHY[q.plan.reason] || q.plan.reason : "");
    var hasFuel = b.fuel_laps !== null && b.fuel_laps !== undefined;
    pbTile("pb-fuel", true, !hasFuel ? "" : b.fuel_laps >= b.fuel_need_laps ? "ok" : "crit",
      hasFuel ? fmt(b.fuel_laps, 1) + " laps" : "--", "push lap needs " + fmt(b.fuel_need_laps, 1));
    var hasErs = b.ers_pct !== null && b.ers_pct !== undefined;
    pbTile("pb-ers", true, !hasErs ? "" : b.ers_pct >= b.ers_need_pct ? "ok" : "warn",
      hasErs ? fmt(b.ers_pct, 0) + "%" : "--", "want " + fmt(b.ers_need_pct, 0) + "%");
    pbTile("pb-time", !!q, q && q.fresh_sets === 0 ? "warn" : "",
      q ? clock(q.session_time_left) : "", q ? q.fresh_sets + " fresh set" + (q.fresh_sets === 1 ? "" : "s") : "");
    var hasGap = !!q && q.margin_ms !== null && q.margin_ms !== undefined;
    var through = !!q && q.through;
    pbTile("pb-pole", !!q && (hasGap || !!q.best_lap_ms),
      through || (hasGap && q.margin_ms > 0) ? "ok" : hasGap ? "crit" : "",
      hasGap ? signed(-q.margin_ms) : q ? lapTime(q.best_lap_ms) : "",
      hasGap ? "best " + lapTime(q.best_lap_ms) + (through ? " · through" : "") : through ? "through" : "",
      hasGap ? (q.margin_kind === "pole" ? "VS P2" : "VS CUT") : "BEST");
    renderPbWeather(t);
    renderPbSectors(b.sectors);
    renderSetup(b);
  }

  // Garage weather: one row per forecast slot, then the crossover line.
  function renderPbWeather(t) {
    var list = el("pb-wx");
    if (!list) return;
    list.innerHTML = "";
    var wf = t.weather_forecast || [t.weather, -1, -1], r = t.rain_chance_pct || [null, null, null];
    for (var i = 0; i < 3; i++) {
      var w = wf[i], pct = r[i], known = w !== null && w !== undefined && w >= 0;
      var li = document.createElement("li");
      li.className = !known ? "" : w >= 4 ? "storm" : w >= 3 || pct >= 40 ? "wet" : "dry";
      li.appendChild(span(i === 0 ? "NOW" : "+" + SLOT_MIN[i] + " min", "k"));
      li.appendChild(span(known ? WEATHER_WORDS[w] || "--" : "--", "sky"));
      li.appendChild(span(known && pct !== null && pct !== undefined ? "rain " + pct + "%" : "--", "pct"));
      list.appendChild(li);
    }
    var x = el("pb-cross"), c = CROSS[t.weather_crossover] ? t.weather_crossover : null;
    var cm = t.weather_crossover_min;
    if (!x) return;
    x.innerHTML = "";
    if (!c) { x.className = "cross"; x.textContent = "✓ no tyre change due"; return; }
    x.className = "cross " + CROSS[c][2];
    x.appendChild(crossChip(c));
    x.appendChild(document.createTextNode(cm !== null && cm !== undefined ? " in ~" + fmt(cm, 0) + " min" : " soon"));
  }

  // Garage sectors: your best lap, then the gap per sector to the leader and teammate.
  function renderPbSectors(sec) {
    sec = sec || {};
    function secTime(ms) { return ms ? (ms / 1000).toFixed(3) : "--"; }
    function secGap(ms) {
      if (ms === null || ms === undefined) return "--";
      return (ms > 0 ? "+" : ms < 0 ? "−" : "±") + (Math.abs(ms) / 1000).toFixed(3);
    }
    function fill(id, cells) {
      var row = el(id);
      if (!row) return;
      var tds = row.querySelectorAll("td");
      cells.forEach(function (c, i) { tds[i].textContent = c[0]; tds[i].className = c[1] || ""; });
    }
    function gapRow(id, who) {
      var gaps = who ? who.gaps_ms || [] : [];
      var worst = gaps.reduce(function (w, g, i) { return g > 0 && (w < 0 || g > gaps[w]) ? i : w; }, -1);
      var cells = [0, 1, 2].map(function (i) {
        var g = gaps[i];
        return [secGap(g), g === null || g === undefined ? "" :
          (g > 0 ? "warn" : g < 0 ? "ok" : "") + (i === worst ? " worst" : "")];
      });
      var lap = who ? who.gap_ms : null;
      cells.push([secGap(lap), lap > 0 ? "warn" : lap < 0 ? "ok" : ""]);
      fill(id, cells);
    }
    function benchName(id, who) {
      var n = el(id);
      if (!n) return;
      n.textContent = (who && who.driver) || "";
      n.title = n.textContent;
      n.hidden = !n.textContent;
    }
    benchName("pb-sec-lead-who", sec.leader);
    benchName("pb-sec-mate-who", sec.teammate);
    var mine = sec.mine_ms || [null, null, null];
    fill("pb-sec-me", [[secTime(mine[0])], [secTime(mine[1])], [secTime(mine[2])], [lapTime(sec.lap_ms)]]);
    if (sec.player_leads) fill("pb-sec-lead", [["you lead", "ok"], [""], [""], ["±0.000", "ok"]]);
    else gapRow("pb-sec-lead", sec.leader);
    gapRow("pb-sec-mate", sec.teammate);
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
    var minPct = c.ers_min_pct, ready = c.ers_pct >= minPct;
    var ersTile = el("c-ers-tile");
    if (ersTile) ersTile.className = "card col " + (ready ? "ok" : "warn");
    setText("c-ers", fmt(c.ers_pct, 0) + "%");
    var bar = el("c-ers-bar");
    if (bar) bar.style.setProperty("--f", Math.max(0, Math.min(100, c.ers_pct)) + "%");
    var mark = el("c-ers-min");
    if (mark) mark.style.setProperty("--m", minPct + "%");
    var mode = el("c-mode");
    if (mode) {
      mode.textContent = (c.recharging ? "✓ RECHARGING" : "⚠ SET RECHARGE") +
        " · need " + fmt(minPct, 0) + "%";
      mode.className = "sub" + (c.recharging ? "" : " warn");
    }
    var need = c.fuel_need_laps, fuelOk = c.fuel_laps >= need;
    var fuelTile = el("c-fuel-tile");
    if (fuelTile) fuelTile.className = "card col " + (fuelOk ? "ok" : "warn");
    setText("c-fuel", fmt(c.fuel_laps, 1) + "L");
    var fbar = el("c-fuel-bar");
    if (fbar) fbar.style.setProperty("--f", (100 * Math.max(0, Math.min(1, (c.fuel_laps || 0) / (2 * need)))).toFixed(1) + "%");
    setText("c-fuel-need", "push needs " + fmt(need, 1) + " laps");
    var hot = c.dist_to_hot_m;
    var hotEl = el("c-hot");
    if (hotEl) {
      hotEl.textContent = hot === null ? "--" : hot >= 1000 ? fmt(hot / 1000, 1) : fmt(hot, 0);
      if (hot !== null) hotEl.appendChild(span(hot >= 1000 ? "KM" : "M", "tagw"));
    }
    setText("c-plan", c.extend ? "+1 COOL LAP" : c.plan_reason ?
      "cooling: " + (PLAN_WORDS[c.plan_reason] || c.plan_reason) : "--");
    var behind = el("c-behind");
    if (behind) {
      var s = c.car_behind_s;
      if (s === null) {
        setCard(behind, "card clear", ["✓ NO HOT LAP BEHIND"]);
      } else {
        setCard(behind, "card act " + (s <= 3 ? "stop flash" : "box"), [
          icon("warn", "i l"), span("HOT LAP " + fmt(s, 1) + " s BEHIND", "big"), span("MOVE OFF LINE", "mid")
        ]);
      }
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
    setText("c-mis", "RUN CHECK · " + (c.mistakes || "clean lap"));
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
      prevEl = el("previous"), evid = el("call-evidence"), life = el("call-life");
    if (banner && text && meta) {
      meta.innerHTML = "";
      if (mismatched) {
        banner.className = "banner mismatch";
        text.textContent = "PROTOCOL MISMATCH — RELOAD";
        if (life) life.className = "life";
      } else if (stale && lastState !== null) {
        banner.className = "banner stalewarn";
        text.textContent = "TELEMETRY STALE · ADVICE PAUSED";
        if (life) life.className = "life";
      } else if (cur) {
        if (cur.id !== shownCurrentId) {
          hold("hit", HIT_MS);
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
        if (life) life.className = "life";
      }
      if (evid) {
        var evTxt = cur && !stale && !mismatched ? (cur.evidence || cur.why || "") : "";
        evid.hidden = !evTxt;
        if (evTxt) {
          evid.innerHTML = "";
          var eb = document.createElement("b");
          eb.textContent = "WHY ";
          evid.appendChild(eb);
          evid.appendChild(document.createTextNode(evTxt));
        }
      }
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

  // Dim battle card shrinks to one line when the right column overflows (docs/15 §3).
  function fitRight() {
    var col = el("ctx");
    if (!col) return;
    document.body.classList.remove("squeeze");
    if (col.scrollHeight > col.clientHeight + 1) document.body.classList.add("squeeze");
  }

  function render() {
    var stale = renderFreshness();
    renderRadio(stale);
    renderReplyCard();
    fitRight();
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
      if (p.calls) {
        calls = p.calls.map(function (c) {
          return Object.assign({ audio: "dispatched" }, c);
        });
      }
      renderState(p);
    } else if (m.type === "call") {
      if (!calls.some(function (c) { return c.id === p.id; })) {
        calls.push(Object.assign({ t: m.t, seq: m.seq, audio: "dispatched" }, p));
        if (calls.length > MAX_CALLS) calls = calls.slice(-MAX_CALLS);
        fx("call");
      }
      renderReplyCard();
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
