/* pitwall review-mode transport bar (docs/07). Enabled when the hello frame
   carries review: true — see app.js which adds body.review. */
(function () {
  var strip, detail, scrub, posEl, lapEl, playBtn;
  var timeline = null;   // {duration_us, laps, events, decisions}
  var status = null;     // {playing, position_us, lap, speed}
  var decisions = [];

  function el(id) { return document.getElementById(id); }
  function api(path, method, body) {
    return fetch("/api/review/" + path, {
      method: method || "GET",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    }).then(function (r) { return r.json(); });
  }

  function fmt(us) {
    var s = Math.floor(us / 1e6), m = Math.floor(s / 60);
    return m + ":" + ("0" + (s % 60)).slice(-2);
  }

  function tickColor(d) {
    if (d.outcome === "fired") {
      return d.priority === 1 ? "#e5484d" : d.priority === 2 ? "#ffb224" : "#46a758";
    }
    if (d.outcome === "ack" || d.outcome === "neg" || d.outcome === "bookmark") {
      return "#8b5cf6";
    }
    return "#5c6470"; // suppressed and everything else
  }

  function drawStrip() {
    if (!timeline || !strip) return;
    strip.innerHTML = "";
    var dur = timeline.duration_us || 1;
    decisions.forEach(function (d) {
      var t = (d.t || 0) * 1e6;
      var div = document.createElement("div");
      div.className = "tick" + (d.grade ? " graded" : "");
      div.style.left = (100 * t / dur) + "%";
      div.style.background = tickColor(d);
      div.title = d.rule_id + " — " + d.outcome;
      div.addEventListener("click", function () { showDetail(d); });
      strip.appendChild(div);
    });
  }

  function showDetail(d) {
    detail.hidden = false;
    var rows = "";
    var inputs = d.inputs || {};
    Object.keys(inputs).forEach(function (k) {
      var v = inputs[k];
      rows += "<tr><td>" + k + "</td><td>" +
        (typeof v === "number" ? +v.toFixed(3) : String(v)) + "</td></tr>";
    });
    detail.innerHTML =
      "<b>" + (d.rule_id || d.outcome) + "</b> — " + d.outcome +
      (d.suppressed_by ? " (" + d.suppressed_by + ")" : "") +
      "<div class='dtext'>" + (d.text || "") + "</div>" +
      "<table>" + rows + "</table>" +
      "<div class='grades'>" +
      ["good", "noise", "too_late", "wrong"].map(function (g) {
        return "<button data-g='" + g + "'" +
          (d.grade === g ? " class='on'" : "") + ">" + g + "</button>";
      }).join(" ") + "</div>";
    detail.querySelectorAll("button[data-g]").forEach(function (b) {
      b.addEventListener("click", function () {
        api("grade", "POST", {
          call_id: d.call_id, rule_id: d.rule_id, grade: b.dataset.g,
        }).then(function () {
          d.grade = b.dataset.g;
          showDetail(d);
          drawStrip();
        });
      });
    });
  }

  function refresh() {
    api("status").then(function (st) {
      status = st;
      if (playBtn) playBtn.textContent = st.playing ? "⏸" : "▶";
      if (posEl) posEl.textContent = fmt(st.position_us) + " / " + fmt(st.duration_us);
      if (scrub) scrub.value = String(Math.round(1000 * st.position_us / (st.duration_us || 1)));
      var lastLap = timeline && timeline.laps.length
        ? timeline.laps[timeline.laps.length - 1].lap : 0;
      if (lapEl) lapEl.textContent = "LAP " + st.lap + " / " + lastLap;
    });
  }

  function init() {
    strip = el("t-strip"); detail = el("t-detail");
    scrub = el("t-scrub"); posEl = el("t-pos"); lapEl = el("t-lap");
    playBtn = el("t-play");
    api("timeline").then(function (tl) {
      timeline = tl;
      decisions = tl.decisions || [];
      drawStrip();
      refresh();
      setInterval(refresh, 1000);
    });
    el("t-play").addEventListener("click", function () {
      api(status && status.playing ? "pause" : "play", "POST").then(function (st) { status = st; refresh(); });
    });
    el("t-prev").addEventListener("click", function () {
      var cur = status ? status.lap : 0;
      api("seek", "POST", { lap: Math.max(1, cur - 1) }).then(refresh);
    });
    el("t-next").addEventListener("click", function () {
      var cur = status ? status.lap : 0;
      api("seek", "POST", { lap: cur + 1 }).then(refresh);
    });
    scrub.addEventListener("change", function () {
      var us = Math.round((Number(scrub.value) / 1000) * timeline.duration_us);
      api("seek", "POST", { offset_us: us }).then(refresh);
    });
  }

  window.pitwallReviewInit = function () {
    var t = el("transport");
    if (t) t.hidden = false;
    init();
  };
})();
