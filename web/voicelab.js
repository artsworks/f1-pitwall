(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var statusEl = $("status");
  var audio = null;
  var vocab = null;
  var selectedSlots = new Set();

  function status(msg, isErr) {
    statusEl.textContent = msg;
    statusEl.className = isErr ? "status err" : "status";
  }

  function play(b64) {
    if (audio) audio.pause();
    audio = new Audio("data:audio/wav;base64," + b64);
    audio.play();
  }

  function lines(text) {
    return text.split("\n").map(function (s) { return s.trim(); }).filter(Boolean);
  }

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
        return data;
      });
    });
  }

  function shuffle(arr) {
    var a = arr.slice();
    for (var i = a.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var t = a[i]; a[i] = a[j]; a[j] = t;
    }
    return a;
  }

  function gradeRow(comboId, label, name, wav, ms) {
    var row = document.createElement("div");
    row.className = "variant";
    var lbl = document.createElement("span");
    lbl.className = "lbl";
    var meta = document.createElement("span");
    meta.className = "meta";
    function refresh() {
      lbl.textContent = $("reveal").checked ? label + " (" + name + ")" : label;
      meta.textContent = $("reveal").checked && ms != null ? ms + " ms" : "";
    }
    refresh();
    row._refresh = refresh;
    var btn = document.createElement("button");
    btn.textContent = "play";
    btn.onclick = function () { play(wav); };
    var score = document.createElement("select");
    score.className = "score";
    for (var i = 1; i <= 5; i++) {
      var o = document.createElement("option");
      o.value = i; o.textContent = i;
      score.appendChild(o);
    }
    score.value = 3;
    var note = document.createElement("input");
    note.className = "note";
    note.type = "text";
    note.placeholder = "note";
    var save = document.createElement("button");
    save.textContent = "Save";
    var saved = document.createElement("span");
    save.onclick = function () {
      save.disabled = true;
      post("/api/voicelab/grade", {
        combo: comboId, variant: name, score: Number(score.value), note: note.value,
      }).then(function () {
        saved.className = "saved"; saved.textContent = "saved";
      }).catch(function (e) {
        status(e.message, true);
      }).finally(function () { save.disabled = false; });
    };
    [lbl, meta, btn, score, note, save, saved].forEach(function (el) { row.appendChild(el); });
    return row;
  }

  function comboCard(combo) {
    var card = document.createElement("div");
    card.className = "card";
    var id = document.createElement("div");
    id.className = "id";
    id.textContent = combo.id;
    card.appendChild(id);
    var names = ["stem", "slot", "spliced", "full"];
    var letters = "ABCD";
    shuffle(names).forEach(function (name, i) {
      var ms = name === "spliced" ? combo.ms.splice : combo.ms[name];
      card.appendChild(gradeRow(combo.id, "Variant " + letters[i], name, combo.wavs[name], ms));
    });
    return card;
  }

  function refreshStems() {
    var sel = $("stems");
    var keep = new Set(Array.from(sel.selectedOptions).map(function (o) { return o.value; }));
    var filter = $("stem-filter").value.toLowerCase();
    sel.innerHTML = "";
    vocab.stems.forEach(function (s) {
      if (filter && s.text.toLowerCase().indexOf(filter) < 0) return;
      var o = document.createElement("option");
      o.value = s.text;
      o.textContent = "P" + s.priority + "  " + s.text;
      o.selected = keep.has(s.text);
      sel.appendChild(o);
    });
  }

  function refreshSlotOptions() {
    var sel = $("slots");
    sel.innerHTML = "";
    var cat = $("slot-cat").value;
    (vocab.slots[cat] || []).forEach(function (s) {
      var o = document.createElement("option");
      o.value = s; o.textContent = s;
      o.selected = selectedSlots.has(s);
      sel.appendChild(o);
    });
    refreshChips();
  }

  function refreshChips() {
    var box = $("slot-chips");
    box.innerHTML = "";
    selectedSlots.forEach(function (s) {
      var chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = s + " ✕";
      chip.title = "remove";
      chip.onclick = function () {
        selectedSlots.delete(s);
        refreshSlotOptions();
      };
      box.appendChild(chip);
    });
  }

  function gatherStems() {
    var fromSel = Array.from($("stems").selectedOptions).map(function (o) { return o.value; });
    return fromSel.concat(lines($("custom-stems").value));
  }

  function gatherSlots() {
    return Array.from(selectedSlots).concat(lines($("custom-slots").value));
  }

  function opts(gapId, fadeId, urlId) {
    var body = {
      gap_ms: Number($(gapId).value) || 0,
      fade_ms: Number($(fadeId).value) || 0,
    };
    var url = $(urlId).value.trim();
    if (url) body.url = url;
    return body;
  }

  function renderCombos() {
    var stems = gatherStems();
    var slots = gatherSlots();
    if (!stems.length || !slots.length) { status("pick at least one stem and one slot", true); return; }
    var btn = $("render");
    btn.disabled = true;
    status("rendering " + stems.length + "×" + slots.length + " combos…");
    var body = Object.assign(opts("gap", "fade", "url"), { stems: stems, slots: slots });
    post("/api/voicelab/render", body).then(function (data) {
      status("done: " + data.combos.length + " combos from " + data.url);
      var results = $("results");
      results.innerHTML = "";
      data.combos.forEach(function (c) { results.appendChild(comboCard(c)); });
    }).catch(function (e) {
      status(e.message, true);
    }).finally(function () { btn.disabled = false; });
  }

  function chainCard(data) {
    var card = document.createElement("div");
    card.className = "card";
    var id = document.createElement("div");
    id.className = "id";
    id.textContent = data.id;
    card.appendChild(id);
    var segs = document.createElement("div");
    segs.className = "variant";
    var lbl = document.createElement("span");
    lbl.className = "lbl";
    lbl.textContent = "Segments";
    segs.appendChild(lbl);
    data.segments.forEach(function (s) {
      var b = document.createElement("button");
      b.textContent = s.text;
      b.onclick = function () { play(s.wav); };
      segs.appendChild(b);
    });
    card.appendChild(segs);
    var variants = [
      { name: "spliced", wav: data.spliced, ms: data.ms.splice },
      { name: "full", wav: data.full, ms: data.ms.full },
    ];
    var letters = "AB";
    shuffle(variants).forEach(function (v, i) {
      card.appendChild(gradeRow(data.id, "Variant " + letters[i], v.name, v.wav, v.ms));
    });
    return card;
  }

  function renderChain() {
    var segs = lines($("segments").value);
    if (!segs.length) { status("enter at least one segment", true); return; }
    var btn = $("render-chain");
    btn.disabled = true;
    status("rendering " + segs.length + " segments…");
    var body = Object.assign(opts("gap2", "fade2", "url2"), { segments: segs });
    post("/api/voicelab/render_multi", body).then(function (data) {
      status("done: chain from " + data.url);
      var results = $("results");
      results.innerHTML = "";
      results.appendChild(chainCard(data));
    }).catch(function (e) {
      status(e.message, true);
    }).finally(function () { btn.disabled = false; });
  }

  function setMode(chain) {
    $("stem-mode").hidden = chain;
    $("chain-mode").hidden = !chain;
    $("results").innerHTML = "";
    status("");
  }

  $("mode-stem").onclick = function () { setMode(false); };
  $("mode-chain").onclick = function () { setMode(true); };
  $("reveal").onchange = function () {
    document.querySelectorAll(".variant").forEach(function (row) {
      if (row._refresh) row._refresh();
    });
  };
  $("stem-filter").oninput = refreshStems;
  $("slot-cat").onchange = refreshSlotOptions;
  $("slots").onchange = function () {
    Array.from($("slots").options).forEach(function (o) {
      if (o.selected) selectedSlots.add(o.value); else selectedSlots.delete(o.value);
    });
    refreshChips();
  };
  $("render").onclick = renderCombos;
  $("render-chain").onclick = renderChain;

  fetch("/api/voicelab/vocab").then(function (r) { return r.json(); }).then(function (data) {
    vocab = data;
    var cat = $("slot-cat");
    Object.keys(data.slots).forEach(function (k) {
      var o = document.createElement("option");
      o.value = k; o.textContent = k;
      cat.appendChild(o);
    });
    refreshStems();
    refreshSlotOptions();
    var presets = $("presets");
    (data.chains.lap_time || []).forEach(function (chain, i) {
      var b = document.createElement("button");
      b.textContent = "lap " + (i + 1);
      b.onclick = function () { $("segments").value = chain.join("\n"); };
      presets.appendChild(b);
    });
    var atoms = $("atom-list");
    (data.slots.lap_time_atoms || []).forEach(function (a) {
      var b = document.createElement("button");
      b.textContent = a;
      b.onclick = function () {
        var cur = lines($("segments").value);
        cur.push(a);
        $("segments").value = cur.join("\n");
      };
      atoms.appendChild(b);
    });
  }).catch(function (e) { status("vocab failed: " + e.message, true); });

  fetch("/api/config").then(function (r) { return r.json(); }).then(function (cfg) {
    var url = cfg.speech && cfg.speech.omnivoice_url;
    if (url) { $("url").placeholder = url; $("url2").placeholder = url; }
  }).catch(function () {});
})();
