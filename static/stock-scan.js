/* Stock on hand camera scanner. Chrome uses BarcodeDetector. Other browsers load /static/zxing.js. */
(function () {
  var openBtn = document.getElementById("stock-scan");
  var root = document.getElementById("stock-scanner");
  if (!openBtn || !root) return;

  var video = document.getElementById("stock-scan-video");
  var statusEl = document.getElementById("stock-scan-status");
  var resultEl = document.getElementById("stock-scan-results");
  var closeBtn = document.getElementById("stock-scan-close");
  var modeBox = document.getElementById("stock-scan-mode");
  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  var stream = null;
  var reader = null;
  var timer = null;
  var audioCtx = null;
  var running = false;
  var generation = 0;
  var engine = "";
  var detectorErrors = 0;
  var last = {text: "", at: 0};
  var MODE_KEY = "silkwoodStockScanMode";

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text || "";
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch];
    });
  }

  function stopTracks(media) {
    if (!media || !media.getTracks) return;
    media.getTracks().forEach(function (track) { track.stop(); });
  }

  function stop() {
    running = false;
    engine = "";
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    if (reader) {
      try { reader.reset(); } catch (err) {}
      reader = null;
    }
    stopTracks(stream);
    stream = null;
    if (video) {
      try { video.pause(); } catch (err) {}
      video.srcObject = null;
    }
  }

  function closeScanner() {
    stop();
    root.hidden = true;
    document.body.classList.remove("scanning");
    try { openBtn.focus(); } catch (err) {}
  }

  function unlockAudio() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      if (!audioCtx) audioCtx = new Ctx();
      if (audioCtx.state === "suspended") audioCtx.resume();
    } catch (err) {}
  }

  function beep() {
    unlockAudio();
    try {
      if (audioCtx && audioCtx.state === "running") {
        var osc = audioCtx.createOscillator();
        var gain = audioCtx.createGain();
        osc.type = "sine";
        osc.frequency.value = 880;
        gain.gain.setValueAtTime(0.06, audioCtx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.09);
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start();
        osc.stop(audioCtx.currentTime + 0.1);
      }
    } catch (err) {}
    try {
      if (navigator.vibrate) navigator.vibrate(80);
    } catch (err) {}
  }

  function permissionMessage(err) {
    var name = (err && err.name) || "";
    if (!window.isSecureContext) {
      return "The camera needs a secure page (https). Open the portal over https, then try Scan again.";
    }
    if (name === "NotAllowedError" || name === "PermissionDeniedError" || name === "SecurityError") {
      return "Camera permission was denied. Allow the camera for this site in the browser settings, then tap Scan again. You can still search by name or PLU.";
    }
    if (name === "NotFoundError" || name === "DevicesNotFoundError") {
      return "No camera was found on this device. You can still search by name or PLU.";
    }
    if (name === "NotReadableError" || name === "TrackStartError" || name === "AbortError") {
      return "The camera is busy in another app. Close that app and tap Scan again.";
    }
    return "The camera did not start. You can still search by name or PLU.";
  }

  function scanMode() {
    return !!(modeBox && modeBox.checked);
  }

  function goToProduct(code) {
    stop();
    root.hidden = true;
    document.body.classList.remove("scanning");
    location.assign("/portal/stock?pick=" + encodeURIComponent(code) + "#stock-count");
  }

  function goLink(barcode) {
    stop();
    root.hidden = true;
    document.body.classList.remove("scanning");
    location.assign("/portal/stock?link=" + encodeURIComponent(barcode) + "#stock-count");
  }

  function brisbaneNow() {
    var fmt = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Australia/Brisbane",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23"
    });
    var parts = {};
    fmt.formatToParts(new Date()).forEach(function (part) { parts[part.type] = part.value; });
    return {
      baseline_date: parts.year + "-" + parts.month + "-" + parts.day,
      baseline_time: parts.hour + ":" + parts.minute
    };
  }

  function countFields() {
    var form = document.getElementById("stock-count");
    var date = form && form.querySelector('input[name="baseline_date"]');
    var time = form && form.querySelector('input[name="baseline_time"]');
    var fallback = brisbaneNow();
    return {
      baseline_date: (date && date.value) || fallback.baseline_date,
      baseline_time: (time && time.value) || fallback.baseline_time
    };
  }

  function findRow(code) {
    var rows = resultEl.querySelectorAll(".scan-row");
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute("data-code") === code) return rows[i];
    }
    return null;
  }

  function trimRows() {
    while (resultEl.children.length > 6) resultEl.removeChild(resultEl.lastChild);
  }

  function saveCount(row, data) {
    var input = row.querySelector("input");
    var note = row.querySelector(".scan-note");
    var button = row.querySelector("button");
    var qty = (input && input.value || "").trim();
    if (!qty) {
      setStatus("Enter the count for " + data.name + ".");
      if (input) input.focus();
      return;
    }
    var fields = countFields();
    var body = new URLSearchParams({
      csrf: csrf,
      action: "set_count",
      code: data.code,
      qty: qty,
      baseline_date: fields.baseline_date,
      baseline_time: fields.baseline_time,
      note: "scanned"
    });
    if (button) button.disabled = true;
    fetch("/portal/stock", {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"},
      body: body.toString(),
      credentials: "same-origin"
    }).then(function (res) {
      if (res.ok && res.url.indexOf("ok=1") !== -1) {
        if (note) note.textContent = "Saved";
        setStatus("Saved " + data.name + ". Point at the next barcode.");
        return;
      }
      if (button) button.disabled = false;
      if (res.status === 403) setStatus("The form expired. Reload the page, then scan again.");
      else setStatus("Could not save that count. Check the number and the date, then try again.");
    }).catch(function () {
      if (button) button.disabled = false;
      setStatus("Could not save that count. Check the connection and try again.");
    });
  }

  function addRow(data) {
    var existing = findRow(data.code);
    if (existing) {
      var again = existing.querySelector("input");
      if (again && !again.disabled) again.focus();
      return;
    }
    var row = document.createElement("div");
    row.className = "scan-row";
    row.setAttribute("data-code", data.code);
    var hand = data.onHandText ? '<div class="mini">On hand ' + esc(data.onHandText) + "</div>" : "";
    row.innerHTML = '<div class="name">' + esc(data.name) + hand +
      '<div class="mini">' + esc(data.code) + "</div></div>" +
      '<label class="f">Count<input inputmode="decimal" min="0" step="any" enterkeyhint="done"></label>' +
      '<button class="btn" type="button">Save count</button>' +
      '<p class="mini scan-note"></p>';
    var input = row.querySelector("input");
    var button = row.querySelector("button");
    button.addEventListener("click", function () { saveCount(row, data); });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        saveCount(row, data);
      }
    });
    resultEl.insertBefore(row, resultEl.firstChild);
    trimRows();
    input.focus();
  }

  function showUnlinked(barcode) {
    var row = document.createElement("div");
    row.className = "scan-row";
    row.innerHTML = '<div class="name">Barcode not linked<div class="mini">' + esc(barcode) + "</div></div>" +
      '<button class="btn" type="button">Choose a product</button>';
    row.querySelector("button").addEventListener("click", function () { goLink(barcode); });
    resultEl.insertBefore(row, resultEl.firstChild);
    trimRows();
  }

  function lookup(text) {
    var now = Date.now();
    if (text === last.text && now - last.at < 2500) return;
    last = {text: text, at: now};
    beep();
    setStatus("Read " + text);
    fetch("/api/portal/stock/barcode?barcode=" + encodeURIComponent(text), {
      headers: {"Accept": "application/json"},
      credentials: "same-origin"
    }).then(function (res) {
      return res.json().then(function (data) { return {res: res, data: data || {}}; });
    }).then(function (out) {
      if (!running) return;
      var data = out.data;
      if (out.res.ok && data.ok && data.code) {
        if (scanMode()) {
          addRow(data);
          setStatus("Matched " + data.name + ". The camera stays open.");
        } else {
          goToProduct(data.code);
        }
        return;
      }
      var message = data.error || "Barcode not linked";
      if (scanMode()) {
        showUnlinked(text);
        setStatus(message + ". Choose a product to link " + text + ", or keep scanning.");
      } else {
        goLink(text);
      }
    }).catch(function () {
      if (running) setStatus("Could not check that barcode. Try again.");
    });
  }

  function loopDetector(detector, token) {
    if (!running || engine !== "detector" || token !== generation) return;
    if (!video || video.readyState < 2) {
      timer = setTimeout(function () { loopDetector(detector, token); }, 200);
      return;
    }
    detector.detect(video).then(function (codes) {
      if (!running || engine !== "detector" || token !== generation) return;
      if (codes && codes[0] && codes[0].rawValue) lookup(String(codes[0].rawValue));
      timer = setTimeout(function () { loopDetector(detector, token); }, 200);
    }).catch(function () {
      if (!running || engine !== "detector" || token !== generation) return;
      detectorErrors += 1;
      if (detectorErrors < 4) {
        timer = setTimeout(function () { loopDetector(detector, token); }, 200);
        return;
      }
      engine = "zxing";
      if (timer) clearTimeout(timer);
      timer = null;
      startZxing(token);
    });
  }

  function startDetector(token) {
    var formats = ["ean_13", "ean_8", "upc_a", "upc_e", "code_128"];
    var ready = Promise.resolve(formats);
    if (window.BarcodeDetector.getSupportedFormats) {
      ready = BarcodeDetector.getSupportedFormats().then(function (supported) {
        var chosen = formats.filter(function (fmt) { return supported.indexOf(fmt) !== -1; });
        return chosen.length ? chosen : null;
      }).catch(function () { return formats; });
    }
    return ready.then(function (chosen) {
      if (!running || token !== generation) return;
      if (!chosen) {
        engine = "zxing";
        return startZxing(token);
      }
      var detector = new BarcodeDetector({formats: chosen});
      engine = "detector";
      loopDetector(detector, token);
    });
  }

  function loadZxing() {
    if (window.ZXing && window.ZXing.BrowserMultiFormatReader) return Promise.resolve();
    return new Promise(function (resolve, reject) {
      var script = document.createElement("script");
      script.src = "/static/zxing.js";
      script.onload = function () { resolve(); };
      script.onerror = function () { reject(new Error("zxing")); };
      document.head.appendChild(script);
    });
  }

  function startZxing(token) {
    if (reader) return Promise.resolve();
    setStatus("Starting the scanner…");
    return loadZxing().then(function () {
      if (!running || token !== generation || !stream) return;
      var ZX = window.ZXing;
      var hints = new Map();
      hints.set(ZX.DecodeHintType.POSSIBLE_FORMATS, [
        ZX.BarcodeFormat.EAN_13,
        ZX.BarcodeFormat.EAN_8,
        ZX.BarcodeFormat.UPC_A,
        ZX.BarcodeFormat.UPC_E,
        ZX.BarcodeFormat.CODE_128
      ]);
      reader = new ZX.BrowserMultiFormatReader(hints, 300);
      engine = "zxing";
      try { video.pause(); } catch (err) {}
      return reader.decodeFromStream(stream, video, function (result) {
        if (!running || engine !== "zxing" || !result) return;
        var text = result.getText ? result.getText() : (result.text || "");
        if (text) lookup(String(text));
      });
    });
  }

  function attachStream(got, token) {
    if (!running || token !== generation) {
      stopTracks(got);
      return;
    }
    stream = got;
    video.srcObject = got;
    video.muted = true;
    video.setAttribute("playsinline", "true");
    video.setAttribute("autoplay", "true");
    if ("BarcodeDetector" in window) {
      var played = video.play();
      if (played && played.catch) played.catch(function () {});
      return startDetector(token);
    }
    return startZxing(token);
  }

  function openCamera(token) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus(window.isSecureContext
        ? "No camera was found on this device. You can still search by name or PLU."
        : "The camera needs a secure page (https). Open the portal over https, then try Scan again.");
      return;
    }
    var constraints = {audio: false, video: {facingMode: {ideal: "environment"}}};
    navigator.mediaDevices.getUserMedia(constraints).catch(function (err) {
      if (err && (err.name === "OverconstrainedError" || err.name === "ConstraintNotSatisfiedError")) {
        return navigator.mediaDevices.getUserMedia({audio: false, video: true});
      }
      throw err;
    }).then(function (got) {
      return attachStream(got, token);
    }).then(function () {
      if (!running || token !== generation || !statusEl) return;
      if (/Opening|Starting/.test(statusEl.textContent || "")) setStatus("Point the rear camera at a barcode.");
    }).catch(function (err) {
      if (token !== generation) return;
      stop();
      setStatus(permissionMessage(err));
    });
  }

  function openScanner() {
    var token = ++generation;
    running = true;
    engine = "";
    detectorErrors = 0;
    last = {text: "", at: 0};
    resultEl.innerHTML = "";
    root.hidden = false;
    document.body.classList.add("scanning");
    setStatus("Opening the camera…");
    try { closeBtn.focus(); } catch (err) {}
    unlockAudio();
    openCamera(token);
  }

  try {
    if (modeBox && window.sessionStorage && sessionStorage.getItem(MODE_KEY) === "1") modeBox.checked = true;
  } catch (err) {}
  if (modeBox) {
    modeBox.addEventListener("change", function () {
      try {
        if (window.sessionStorage) sessionStorage.setItem(MODE_KEY, modeBox.checked ? "1" : "0");
      } catch (err) {}
    });
  }
  openBtn.addEventListener("click", openScanner);
  if (closeBtn) closeBtn.addEventListener("click", closeScanner);
  var backdrop = root.querySelector("[data-scan-backdrop]");
  if (backdrop) backdrop.addEventListener("click", closeScanner);
  document.addEventListener("keydown", function (event) {
    if (!root.hidden && event.key === "Escape") closeScanner();
  });
  window.addEventListener("pagehide", stop);

  var picked = document.querySelector('#stock-count input[name="code"]');
  if (picked && picked.value && !document.getElementById("barcode-link")) {
    var qty = document.querySelector('#stock-count input[name="qty"]');
    if (qty && !qty.value) qty.focus();
  }
})();
