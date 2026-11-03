(function () {
  "use strict";

  var MONTHS_FULL = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  var WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

  var statusEl = document.getElementById("status");
  var board = document.getElementById("board");
  var tape = document.getElementById("tape");
  var form = document.getElementById("guess");
  var yearHint = document.getElementById("year-hint");
  var yearInput = document.getElementById("year");
  var yearList = document.getElementById("year-list");
  var yearNote = document.getElementById("year-note");
  var lockBtn = document.getElementById("lock");
  var pickers = document.getElementById("pickers");
  var result = document.getElementById("result");
  var resultWhen = document.getElementById("result-when");
  var resultDay = document.getElementById("result-day");
  var resultDistance = document.getElementById("result-distance");
  var againBtn = document.getElementById("again");
  var sortButtons = Array.prototype.slice.call(document.querySelectorAll(".cols button"));

  var state = {
    ids: [],
    deck: [],
    rows: [],
    iso: "",
    sort: "cap",
    dir: "desc",
    year: null,
    yearMin: 2006,
    yearMax: 2026,
    activeYear: null,
    locked: false,
    busy: false
  };

  function asset(path) {
    var url = new URL(window.location.href);
    var dir = url.pathname;
    if (dir.endsWith("/")) {
      /* directory URL */
    } else if (/\.html?$/i.test(dir)) {
      dir = dir.slice(0, dir.lastIndexOf("/") + 1);
    } else {
      dir += "/";
    }
    return dir + path.replace(/^\//, "");
  }

  function shuffle(list) {
    var copy = list.slice();
    for (var i = copy.length - 1; i > 0; i -= 1) {
      var j = Math.floor(Math.random() * (i + 1));
      var tmp = copy[i];
      copy[i] = copy[j];
      copy[j] = tmp;
    }
    return copy;
  }

  function parseISODate(iso) {
    var parts = iso.split("-");
    if (parts.length !== 3) return null;
    var y = Number(parts[0]);
    var m = Number(parts[1]);
    var d = Number(parts[2]);
    if (!y || !m || !d) return null;
    return new Date(y, m - 1, d);
  }

  function formatPrice(n) {
    return n.toLocaleString("en-US", {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
  }

  function formatCap(millions) {
    var text;
    if (millions >= 1e6) text = (millions / 1e6).toFixed(2) + "T";
    else if (millions >= 100000) text = (millions / 1000).toFixed(0) + "B";
    else if (millions >= 10000) text = (millions / 1000).toFixed(1) + "B";
    else if (millions >= 1000) text = (millions / 1000).toFixed(2) + "B";
    else text = Math.round(millions) + "M";
    return "$" + text;
  }

  function sortedRows() {
    var rows = state.rows.slice();
    var sign = state.dir === "asc" ? 1 : -1;
    rows.sort(function (a, b) {
      var cmp = 0;
      if (state.sort === "name") cmp = a.name.localeCompare(b.name);
      else if (state.sort === "price") cmp = a.price - b.price;
      else cmp = a.cap - b.cap;
      if (cmp) return cmp * sign;
      return a.name.localeCompare(b.name);
    });
    return rows;
  }

  function syncSortHeaders() {
    sortButtons.forEach(function (button) {
      var key = button.dataset.sort;
      var active = key === state.sort;
      var ascending = state.dir === "asc";
      button.setAttribute("aria-pressed", String(active));
      button.setAttribute("aria-sort", active ? (ascending ? "ascending" : "descending") : "none");
      var dir = button.querySelector(".dir");
      if (!dir) {
        dir = document.createElement("span");
        dir.className = "dir";
        dir.setAttribute("aria-hidden", "true");
        button.appendChild(document.createTextNode(" "));
        button.appendChild(dir);
      }
      dir.textContent = active ? (ascending ? "↑" : "↓") : "";
      var meaning = {
        name: ascending ? "Name, A to Z" : "Name, Z to A",
        cap: ascending ? "Market cap, low to high" : "Market cap, high to low",
        price: ascending ? "Share price, low to high" : "Share price, high to low"
      };
      var idle = { name: "name", cap: "market cap", price: "share price" };
      button.setAttribute("aria-label", active ? meaning[key] + ". Activate to reverse." : "Sort by " + idle[key]);
    });
  }

  function renderTape() {
    var rows = sortedRows();
    var fragment = document.createDocumentFragment();
    rows.forEach(function (row) {
      var li = document.createElement("li");
      var name = document.createElement("span");
      var cap = document.createElement("span");
      var price = document.createElement("span");
      name.className = "name";
      cap.className = "cap";
      price.className = "price";
      name.textContent = row.name;
      cap.textContent = formatCap(row.cap);
      price.textContent = formatPrice(row.price);
      li.appendChild(name);
      li.appendChild(cap);
      li.appendChild(price);
      fragment.appendChild(li);
    });
    tape.replaceChildren(fragment);
    syncSortHeaders();
  }

  function asRow(raw) {
    if (!Array.isArray(raw) || raw.length < 3) return null;
    var name = typeof raw[0] === "string" ? raw[0] : "";
    var price = Number(raw[1]);
    var cap = Number(raw[2]);
    if (!name || !Number.isFinite(price) || !Number.isFinite(cap)) return null;
    return { name: name, price: price, cap: cap };
  }

  function syncLock() {
    lockBtn.disabled = state.locked || !state.year;
  }

  function inRange(year) {
    return year >= state.yearMin && year <= state.yearMax;
  }

  function parseYear(text) {
    if (!/^\d{4}$/.test(text)) return null;
    var year = Number(text);
    return inRange(year) ? year : null;
  }

  function matchingYears(query) {
    var years = [];
    var y;
    for (y = state.yearMin; y <= state.yearMax; y += 1) {
      if (!query || String(y).indexOf(query) === 0) years.push(y);
    }
    return years;
  }

  function closeList() {
    yearList.hidden = true;
    yearList.replaceChildren();
    yearInput.setAttribute("aria-expanded", "false");
    yearInput.removeAttribute("aria-activedescendant");
    state.activeYear = null;
  }

  function renderList() {
    if (state.locked || document.activeElement !== yearInput) {
      closeList();
      return;
    }
    var query = yearInput.value;
    var years = matchingYears(query);
    if (!years.length) {
      closeList();
      return;
    }
    if (state.activeYear === null || years.indexOf(state.activeYear) === -1) {
      state.activeYear = parseYear(query) || years[0];
    }
    var fragment = document.createDocumentFragment();
    years.forEach(function (year) {
      var option = document.createElement("button");
      option.type = "button";
      option.id = "year-opt-" + year;
      option.dataset.year = String(year);
      option.setAttribute("role", "option");
      option.textContent = String(year);
      option.setAttribute("aria-selected", String(year === state.activeYear));
      fragment.appendChild(option);
    });
    yearList.replaceChildren(fragment);
    yearList.hidden = false;
    yearInput.setAttribute("aria-expanded", "true");
    yearInput.setAttribute("aria-activedescendant", "year-opt-" + state.activeYear);
    var active = document.getElementById("year-opt-" + state.activeYear);
    if (!active) return;
    var listRect = yearList.getBoundingClientRect();
    var itemRect = active.getBoundingClientRect();
    if (itemRect.top < listRect.top) yearList.scrollTop -= listRect.top - itemRect.top;
    else if (itemRect.bottom > listRect.bottom) yearList.scrollTop += itemRect.bottom - listRect.bottom;
  }

  function applyInput() {
    var digits = yearInput.value.replace(/\D/g, "").slice(0, 4);
    if (yearInput.value !== digits) yearInput.value = digits;
    var year = parseYear(digits);
    state.year = year;
    var rejected = digits.length === 4 && year === null;
    yearInput.setAttribute("aria-invalid", rejected ? "true" : "false");
    yearNote.hidden = !rejected;
    syncLock();
    if (year !== null) {
      closeList();
      return;
    }
    renderList();
  }

  function chooseYear(year) {
    if (!inRange(year) || state.locked) return;
    yearInput.value = String(year);
    state.year = year;
    yearInput.setAttribute("aria-invalid", "false");
    yearNote.hidden = true;
    closeList();
    syncLock();
  }

  function submitGuess(year) {
    if (year == null) year = parseYear(yearInput.value);
    if (!inRange(year) || state.locked || !state.iso) return;
    state.locked = true;
    yearInput.value = String(year);
    state.year = year;
    yearInput.setAttribute("aria-invalid", "false");
    yearNote.hidden = true;
    closeList();
    syncLock();
    if (document.activeElement === yearInput) yearInput.blur();
    reveal();
  }

  function moveActive(step) {
    var years = matchingYears(yearInput.value);
    if (!years.length) return;
    var index = years.indexOf(state.activeYear);
    if (index < 0) index = 0;
    else index = (index + step + years.length) % years.length;
    state.activeYear = years[index];
    renderList();
  }

  function offLine(abs) {
    if (abs === 0) return "Exact year";
    if (abs === 1) return "You were 1 year off";
    return "You were " + abs + " years off";
  }

  function reveal() {
    var actual = parseISODate(state.iso);
    if (!actual) return;
    var actualYear = actual.getFullYear();
    var actualMonth = actual.getMonth();
    var abs = Math.abs(state.year - actualYear);
    state.locked = true;
    resultWhen.textContent = String(actualYear);
    resultDay.textContent = WEEKDAYS[actual.getDay()] + ", " + MONTHS_FULL[actualMonth] + " " + actual.getDate();
    resultDistance.textContent = offLine(abs);
    pickers.hidden = true;
    result.hidden = false;
    syncLock();
  }

  function resetGuess() {
    state.year = null;
    state.activeYear = null;
    state.locked = false;
    yearInput.value = "";
    yearInput.setAttribute("aria-invalid", "false");
    yearNote.hidden = true;
    closeList();
    pickers.hidden = false;
    result.hidden = true;
    resultWhen.textContent = "";
    resultDay.textContent = "";
    resultDistance.textContent = "";
    syncLock();
  }

  function showStatus(message) {
    statusEl.hidden = !message;
    statusEl.textContent = message || "";
  }

  function asSessionId(id) {
    return typeof id === "string" && /^[a-f0-9]{16}$/.test(id) ? id : null;
  }

  async function loadSnapshot(id) {
    if (!asSessionId(id)) throw new Error("Bad session id");
    var response = await fetch(asset("data/snapshots/" + id + ".json"), { cache: "no-cache" });
    if (!response.ok) throw new Error("Session " + response.status);
    var payload = await response.json();
    if (!payload || typeof payload.d !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(payload.d) || !parseISODate(payload.d) || !Array.isArray(payload.rows)) {
      throw new Error("Session was unreadable");
    }
    var rows = [];
    payload.rows.forEach(function (raw) {
      var row = asRow(raw);
      if (row) rows.push(row);
    });
    if (!rows.length) throw new Error("Session was empty");
    return { iso: payload.d, rows: rows };
  }

  async function deal() {
    if (state.busy) return;
    state.busy = true;
    board.setAttribute("aria-busy", "true");
    showStatus("Pulling a session…");
    tape.replaceChildren();
    resetGuess();
    state.sort = "cap";
    state.dir = "desc";
    syncSortHeaders();
    var loaded = null;
    var attempts = 0;
    while (!loaded && attempts < 4) {
      attempts += 1;
      if (!state.deck.length) state.deck = shuffle(state.ids);
      var id = state.deck.pop();
      try {
        loaded = await loadSnapshot(id);
      } catch (err) {
        loaded = null;
      }
    }
    state.busy = false;
    board.removeAttribute("aria-busy");
    if (!loaded) {
      showStatus("Couldn't load a session. Serve this folder over HTTP and refresh. Opening the file directly blocks the data.");
      return;
    }
    state.iso = loaded.iso;
    state.rows = loaded.rows;
    showStatus("");
    renderTape();
    board.scrollTop = 0;
  }

  yearInput.addEventListener("input", applyInput);

  yearInput.addEventListener("focus", function () {
    state.activeYear = state.year;
    renderList();
  });

  var suppressBlurSubmit = false;
  var suppressBlurTimer = 0;

  function holdBlurSubmit() {
    suppressBlurSubmit = true;
    clearTimeout(suppressBlurTimer);
    suppressBlurTimer = setTimeout(function () {
      suppressBlurSubmit = false;
    }, 300);
  }

  yearInput.addEventListener("blur", function (event) {
    closeList();
    if (state.locked || suppressBlurSubmit) return;
    var next = event.relatedTarget;
    if (next && yearList.contains(next)) return;
    if (parseYear(yearInput.value) === null) return;
    submitGuess(parseYear(yearInput.value));
  });

  yearInput.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (yearList.hidden) renderList();
      else moveActive(event.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (event.key === "Escape") {
      closeList();
      return;
    }
    if (event.key !== "Enter") return;
    if (parseYear(yearInput.value) === null) event.preventDefault();
  });

  var armedSuggestion = null;

  yearList.addEventListener("mousedown", function (event) {
    if (!event.target.closest("button")) return;
    event.preventDefault();
    holdBlurSubmit();
  });

  yearList.addEventListener("pointerdown", function (event) {
    var option = event.target.closest("button");
    if (!option) return;
    event.preventDefault();
    holdBlurSubmit();
    armedSuggestion = {
      year: Number(option.dataset.year),
      x: event.clientX,
      y: event.clientY,
      id: event.pointerId
    };
    if (event.isTrusted) {
      try { yearList.setPointerCapture(event.pointerId); } catch (err) { /* not a capturable pointer */ }
    }
  });

  yearList.addEventListener("pointerup", function (event) {
    if (!armedSuggestion || event.pointerId !== armedSuggestion.id) return;
    var year = armedSuggestion.year;
    var dragged = Math.abs(event.clientX - armedSuggestion.x) > 12 || Math.abs(event.clientY - armedSuggestion.y) > 12;
    armedSuggestion = null;
    if (dragged) return;
    chooseYear(year);
  });

  yearList.addEventListener("pointercancel", function (event) {
    if (armedSuggestion && event.pointerId === armedSuggestion.id) armedSuggestion = null;
  });

  sortButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      var key = button.dataset.sort;
      if (state.sort === key) state.dir = state.dir === "desc" ? "asc" : "desc";
      else {
        state.sort = key;
        state.dir = key === "name" ? "asc" : "desc";
      }
      if (state.rows.length) {
        renderTape();
        board.scrollTop = 0;
      } else syncSortHeaders();
    });
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    submitGuess(parseYear(yearInput.value));
  });

  function syncVisibleFrame() {
    var viewport = window.visualViewport;
    var inset = 0;
    if (viewport) inset = Math.max(0, window.innerHeight - viewport.offsetTop - viewport.height);
    document.documentElement.style.setProperty("--keyboard-inset", inset + "px");
  }

  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", syncVisibleFrame);
    window.visualViewport.addEventListener("scroll", syncVisibleFrame);
    syncVisibleFrame();
  }

  againBtn.addEventListener("click", function () {
    deal();
  });

  async function init() {
    var response = await fetch(asset("data/manifest.json"), { cache: "no-cache" });
    if (!response.ok) throw new Error("Manifest " + response.status);
    var manifest = await response.json();
    var ids = Array.isArray(manifest.snapshots) ? manifest.snapshots : [];
    state.ids = ids.map(asSessionId).filter(Boolean);
    if (!state.ids.length) throw new Error("No sessions");
    var yearMin = Number(manifest.yearMin);
    var yearMax = Number(manifest.yearMax);
    if (!Number.isInteger(yearMin) || !Number.isInteger(yearMax) || yearMin < 1900 || yearMax > 2100 || yearMax < yearMin || yearMax - yearMin > 80) {
      throw new Error("Bad year range");
    }
    state.yearMin = yearMin;
    state.yearMax = yearMax;
    var span = state.yearMin + "–" + state.yearMax;
    yearHint.textContent = span;
    yearNote.textContent = span + " only";
    await deal();
  }

  init().catch(function () {
    showStatus("Couldn't load a session. From this folder run python3 -m http.server 8081, then open the local address. Opening the file directly won't load the data.");
  });
})();
