(function () {
  var clock = document.getElementById("clock");
  if (clock) {
    var fmt = new Intl.DateTimeFormat("en-AU", {
      timeZone: "Australia/Brisbane",
      weekday: "short",
      day: "numeric",
      month: "short",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit"
    });
    var tick = function () { clock.textContent = fmt.format(new Date()); };
    tick();
    setInterval(tick, 30000);
  }
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) event.preventDefault();
    });
  });

  var contractForm = document.getElementById("contract-form");
  var contractName = document.getElementById("contract-name");
  var contractRate = document.getElementById("contract-rate");
  if (contractForm && contractName && contractRate) {
    var savedRates = {};
    try { savedRates = JSON.parse(contractForm.getAttribute("data-rates") || "{}"); } catch (e) { savedRates = {}; }
    function contractKey(value) {
      return String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
    }
    function fillContractRate() {
      var saved = savedRates[contractKey(contractName.value)];
      if (!saved) return;
      if (contractRate.value && contractRate.getAttribute("data-filled") !== "1") return;
      contractRate.value = saved;
      contractRate.setAttribute("data-filled", "1");
    }
    contractName.addEventListener("change", fillContractRate);
    contractName.addEventListener("blur", fillContractRate);
    contractRate.addEventListener("input", function () {
      contractRate.setAttribute("data-filled", contractRate.value ? "0" : "1");
    });
  }

  var toggle = document.getElementById("nav-toggle");
  var backdrop = document.getElementById("nav-backdrop");
  function setNav(open) {
    document.body.classList.toggle("nav-open", open);
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (toggle) toggle.addEventListener("click", function () { setNav(!document.body.classList.contains("nav-open")); });
  if (backdrop) backdrop.addEventListener("click", function () { setNav(false); });

  document.querySelectorAll("[data-print]").forEach(function (btn) {
    btn.addEventListener("click", function () { window.print(); });
  });

  document.querySelectorAll("[data-supplier-filter]").forEach(function (box) {
    var search = box.querySelector("input[type=search]");
    if (!search) return;
    search.addEventListener("input", function () {
      var q = search.value.trim().toLowerCase();
      box.querySelectorAll("[data-supplier]").forEach(function (row) {
        var name = (row.getAttribute("data-supplier") || "").toLowerCase();
        row.hidden = Boolean(q) && name.indexOf(q) === -1;
      });
    });
  });

  var panel = document.getElementById("help-panel");
  var openBtn = document.getElementById("help-open");
  var closeBtn = document.getElementById("help-close");
  var log = document.getElementById("help-log");
  var form = document.getElementById("help-form");
  var clearBtn = document.getElementById("help-clear");
  if (!panel || !form || !log) return;
  var token = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  var timer = null;

  function esc(value) {
    return String(value || "").replace(/[&<>"']/g, function (ch) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch];
    });
  }
  function plain(value) {
    return esc(value).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/\n/g, "<br>");
  }
  function draw(messages, assistant) {
    if (!messages.length) {
      log.innerHTML = '<p class="empty">Ask a question about this page. Replies appear here.</p>';
      return;
    }
    log.innerHTML = messages.map(function (row) {
      var you = '<article class="bubble you"><b>You</b><p>' + plain(row.message) + "</p></article>";
      var waiting = assistant === "offline" ? "Assistant offline — your message is saved" : "Thinking…";
      var reply = row.reply
        ? '<article class="bubble bot"><b>Assistant</b><p>' + plain(row.reply) + "</p></article>"
        : '<article class="bubble bot"><b>Assistant</b><p>' + waiting + "</p></article>";
      return you + reply;
    }).join("");
    log.scrollTop = log.scrollHeight;
  }
  function load() {
    fetch("/api/portal/chat", {headers: {"Accept": "application/json"}}).then(function (res) {
      if (!res.ok) return;
      return res.json();
    }).then(function (data) {
      if (data && data.messages) draw(data.messages, data.assistant);
    }).catch(function () {});
  }
  function setOpen(open) {
    panel.hidden = !open;
    if (openBtn) openBtn.setAttribute("aria-expanded", open ? "true" : "false");
    if (timer) clearInterval(timer);
    timer = null;
    if (open) {
      load();
      timer = setInterval(load, 5000);
    }
  }
  if (openBtn) openBtn.addEventListener("click", function () { setOpen(panel.hidden); });
  if (closeBtn) closeBtn.addEventListener("click", function () { setOpen(false); });
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var field = form.querySelector("textarea");
    var message = (field.value || "").trim();
    if (!message) return;
    var context = {};
    new URLSearchParams(location.search).forEach(function (value, key) { context[key] = value; });
    fetch("/api/portal/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": token, "Accept": "application/json"},
      body: JSON.stringify({message: message, page: location.pathname, context: context})
    }).then(function (res) { return res.json(); }).then(function (data) {
      field.value = "";
      if (data && data.status === "offline") {
        var note = document.getElementById("help-note");
        if (note) note.textContent = "Assistant offline — your message is saved";
      }
      load();
    }).catch(function () {});
  });
  if (clearBtn) clearBtn.addEventListener("click", function () {
    fetch("/api/portal/chat/clear", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": token},
      body: "{}"
    }).then(function () { load(); });
  });
})();
