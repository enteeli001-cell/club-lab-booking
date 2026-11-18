/* 社团实验室预约管理系统 — shared front-end behaviour.
 * Served as a versioned, immutable static file (cached once per release instead of being
 * inlined into every page). Each module only runs when its elements are present.
 * Server-side validation, CSRF and permissions stay authoritative; this file only improves UX.
 */
(function () {
  "use strict";

  function $(selector, root) { return (root || document).querySelector(selector); }
  function $all(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }
  function pad(n) { return String(n).padStart(2, "0"); }
  function toMinutes(value) {
    if (!value) return null;
    var parts = value.split(":");
    var h = parseInt(parts[0], 10), m = parseInt(parts[1] || "0", 10);
    return isNaN(h) || isNaN(m) ? null : h * 60 + m;
  }
  function fromMinutes(total) { return pad(Math.floor(total / 60)) + ":" + pad(total % 60); }

  /* ---------------------------------------------------------------- device id cookie */
  (function deviceCookie() {
    var key = "lab_device_id";
    var m = document.cookie.match(/(?:^|; )lab_device_id=([^;]*)/);
    if (m && m[1]) return;
    var bytes = new Uint8Array(16);
    (window.crypto || window.msCrypto).getRandomValues(bytes);
    var id = Array.prototype.map.call(bytes, function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
    var cookie = key + "=" + id + ";path=/;max-age=31536000;SameSite=Lax";
    if (window.location.protocol === "https:") cookie += ";Secure";
    document.cookie = cookie;
  })();

  /* ---------------------------------------------------------------- navigation */
  (function navigation() {
    var toggle = document.getElementById("nav-toggle");
    var nav = document.getElementById("site-nav");
    $all(".nav a").forEach(function (link) {
      if (link.pathname === location.pathname) {
        link.classList.add("active");
        link.setAttribute("aria-current", "page");
      }
    });
    if (!toggle || !nav) return;
    var mobile = window.matchMedia("(max-width: 900px)");
    var label = toggle.querySelector(".nav-toggle-label");
    function setOpen(open) {
      nav.classList.toggle("is-open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      if (label) label.textContent = open ? tr("关闭菜单") : tr("菜单");
      else toggle.textContent = open ? tr("关闭菜单") : tr("菜单");
    }
    toggle.addEventListener("click", function () { setOpen(!nav.classList.contains("is-open")); });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && mobile.matches && nav.classList.contains("is-open")) {
        setOpen(false);
        toggle.focus();
      }
    });
    document.addEventListener("click", function (event) {
      if (!mobile.matches || !nav.classList.contains("is-open")) return;
      if (nav.contains(event.target) || toggle.contains(event.target)) return;
      setOpen(false);
    });
    function reset() { setOpen(false); }
    if (mobile.addEventListener) mobile.addEventListener("change", reset);
    else mobile.addListener(reset);
  })();

  /* ---------------------------------------------------------------- tables -> cards on phones */
  $all("table:not(.schedule)").forEach(function (table) {
    var headers = $all("thead th", table).map(function (th) { return th.textContent.trim(); });
    table.classList.add("responsive-table");
    $all("tbody tr", table).forEach(function (row) {
      Array.prototype.forEach.call(row.cells, function (cell, index) {
        if (cell.colSpan > 1) cell.classList.add("empty-cell");
        else cell.dataset.label = headers[index] || "";
      });
    });
  });
  $all("table").forEach(function (table) {
    if (table.closest(".table-scroll")) return;
    var wrap = document.createElement("div");
    wrap.className = "table-scroll";
    table.parentNode.insertBefore(wrap, table);
    wrap.appendChild(table);
  });

  /* ---------------------------------------------------------------- accessible dialogs */
  $all(".modal").forEach(function (modal) {
    var dialog = $(".modal-card", modal);
    if (!dialog) return;
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.tabIndex = -1;
    var heading = $("h2", dialog);
    if (heading && heading.id) dialog.setAttribute("aria-labelledby", heading.id);
    var previous = null;
    document.addEventListener("click", function (event) {
      if (modal.hidden) previous = event.target.closest("button, a, input") || document.activeElement;
    }, true);
    new MutationObserver(function () {
      document.body.classList.toggle("has-modal", !!$(".modal:not([hidden])"));
      if (!modal.hidden) {
        if (!previous) previous = document.activeElement;
        ($("textarea, input:not([type=hidden]), button", dialog) || dialog).focus();
      } else if (previous && previous.isConnected) previous.focus();
    }).observe(modal, { attributes: true, attributeFilter: ["hidden"] });
    modal.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { modal.hidden = true; event.preventDefault(); }
      if (event.key !== "Tab") return;
      var items = $all("a[href], button, input:not([type=hidden]), textarea, select", dialog)
        .filter(function (el) { return !el.disabled && el.getClientRects().length; });
      var first = items[0], last = items[items.length - 1];
      if (!first) { event.preventDefault(); dialog.focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    modal.addEventListener("click", function (event) { if (event.target === modal) modal.hidden = true; });
  });

  /* Interface messages follow the site-wide language cookie. */
  function tr(text, values) {
    var map = window.APP_I18N;
    var result = (map && map[text]) || text;
    return result.replace(/\{(\w+)\}/g, function(match, key){
      return values && Object.prototype.hasOwnProperty.call(values, key) ? String(values[key]) : match;
    });
  }
  window.appTranslate = tr;

  /* ---------------------------------------------------------------- password show/hide + Caps Lock */
  (function passwordFields() {
    var seq = 0;
    function bind(btn, input) {
      if (!btn || !input || btn.dataset.bound === "1") return;
      btn.dataset.bound = "1";
      if (!input.id) { seq += 1; input.id = "pw-input-" + seq; }
      btn.setAttribute("aria-controls", input.id);
      btn.addEventListener("click", function () {
        var show = input.type === "password";
        input.type = show ? "text" : "password";
        btn.textContent = tr(show ? "隐藏" : "显示");
        btn.setAttribute("aria-label", tr(show ? "隐藏密码" : "显示密码"));
        btn.setAttribute("aria-pressed", show ? "true" : "false");
      });
    }
    $all('input[type="password"]').forEach(function (input) {
      var wrap = input.closest(".pw-toggle-wrap");
      if (wrap) {
        bind($(".pw-toggle", wrap), input);
      } else {
        wrap = document.createElement("div");
        wrap.className = "pw-toggle-wrap";
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "pw-toggle";
        btn.setAttribute("aria-pressed", "false");
        btn.setAttribute("aria-label", tr("显示密码"));
        btn.textContent = tr("显示");
        wrap.appendChild(btn);
        bind(btn, input);
      }
      var caps = document.createElement("p");
      caps.className = "caps-hint";
      caps.hidden = true;
      caps.setAttribute("aria-live", "polite");
      caps.textContent = tr("大写锁定已开启");
      wrap.insertAdjacentElement("afterend", caps);
      function check(event) {
        if (event.getModifierState) caps.hidden = !event.getModifierState("CapsLock");
      }
      input.addEventListener("keydown", check);
      input.addEventListener("keyup", check);
      input.addEventListener("blur", function () { caps.hidden = true; });
    });
  })();

  // Confirmation messages are text, including any names, rather than executable JS.
  $all("form[data-confirm]").forEach(function(form){
    form.addEventListener("submit", function(event){
      if (!window.confirm(form.getAttribute("data-confirm"))) event.preventDefault();
    });
  });

  /* ---------------------------------------------------------------- double-submit guard */
  // Opt-in with data-busy=tr("提交中…"). Runs after native validation and after inline confirm().
  $all("form[data-busy]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (event.defaultPrevented) return;
      if (form.dataset.submitting === "1") { event.preventDefault(); return; }
      form.dataset.submitting = "1";
      var button = event.submitter || $("button[type=submit], button:not([type])", form);
      if (button) {
        button.dataset.label = button.textContent;
        button.textContent = form.getAttribute("data-busy") || tr("提交中…");
        button.classList.add("is-busy");
        button.setAttribute("aria-disabled", "true");
      }
    });
  });
  window.addEventListener("pageshow", function () {
    // Back/forward cache restores the old DOM: re-enable guarded buttons.
    $all("form[data-busy]").forEach(function (form) {
      form.dataset.submitting = "";
      $all(".is-busy", form).forEach(function (button) {
        button.textContent = button.dataset.label || button.textContent;
        button.classList.remove("is-busy");
        button.removeAttribute("aria-disabled");
      });
    });
  });

  /* ---------------------------------------------------------------- required-field hints */
  // Custom "未填写 / 未选择" prompts instead of the browser bubble. Driven by the HTML `required`
  // attribute (plus format attributes such as pattern/type/min); every form gets novalidate.
  // Hidden and disabled controls are skipped, so conditional fields (e.g. 其他同行人员) only
  // count while they are visible. A submit button with `formnovalidate` bypasses the check.
  (function requiredHints() {
    var uid = 0;
    var CHOICE = { radio: 1, checkbox: 1 };

    function controls(form) {
      return Array.prototype.slice.call(form.elements).filter(function (el) {
        if (!el.name && !el.id) return false;
        var type = (el.type || "").toLowerCase();
        return el.tagName !== "FIELDSET" && el.tagName !== "BUTTON" && el.tagName !== "OBJECT" && el.tagName !== "OUTPUT" &&
          ["hidden", "submit", "button", "reset", "image"].indexOf(type) < 0;
      });
    }
    function isHidden(el) {
      if (el.disabled || el.closest("[hidden]")) return true;
      return !(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    }
    function cleanLabel(node) {
      var clone = node.cloneNode(true);
      Array.prototype.forEach.call(clone.querySelectorAll(".req, .tz-badge, small, .hint, input, select, textarea, button"), function (n) { n.remove(); });
      var text = (clone.textContent || "").replace(/\s+/g, " ").replace(/[（(].*$/, "").replace(/[：:*＊]\s*$/, "").trim();
      return text;
    }
    function groupOf(el) {
      if (!CHOICE[el.type] || !el.name) return [el];
      return Array.prototype.slice.call(el.form.querySelectorAll('input[type="' + el.type + '"]')).filter(function (x) { return x.name === el.name; });
    }
    function labelNode(el) {
      var label = null;
      if (CHOICE[el.type]) {
        var set = el.closest("fieldset");
        var legend = set && set.querySelector("legend");
        if (legend && (groupOf(el).length > 1 || el.type === "radio")) return legend;
        return el.closest("label");
      }
      if (el.id) label = el.form.querySelector('label[for="' + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id) + '"]');
      if (!label) label = el.closest("label");
      if (!label) { var field = el.closest(".field"); if (field) label = field.querySelector("label"); }
      return label;
    }
    function labelText(el) {
      var node = labelNode(el);
      var text = node ? cleanLabel(node) : "";
      return text || el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.name || tr("此项");
    }
    function isEmpty(el) {
      var type = (el.type || "").toLowerCase();
      if (type === "radio") return !groupOf(el).some(function (x) { return x.checked; });
      if (type === "checkbox") return !el.checked;
      if (type === "file") return !el.files || el.files.length === 0;
      if (el.tagName === "SELECT") {
        return el.multiple ? el.selectedOptions.length === 0 : !el.value;
      }
      return !(el.value || "").trim();
    }
    function verb(el) {
      var type = (el.type || "").toLowerCase();
      var pick = el.tagName === "SELECT" || CHOICE[type] || type === "file" || type === "date" || type === "time" || type === "datetime-local";
      return pick ? tr("请选择 ") : tr("请填写 ");
    }
    function formatMessage(el) {
      var v = el.validity;
      var name = labelText(el);
      if (v.rangeUnderflow || v.rangeOverflow || v.stepMismatch) return name + tr("超出允许范围，请检查");
      if (v.tooLong || v.tooShort) return name + tr("长度不符合要求");
      return tr("请填写正确的 ") + name;
    }
    function problem(el) {
      if (el.willValidate === false && !el.required) return null;
      if (el.required && isEmpty(el)) return { text: verb(el) + labelText(el), missing: true };
      if (el.type !== "file" && (el.value || "") !== "" && el.willValidate && !el.checkValidity()) return { text: formatMessage(el), missing: false };
      return null;
    }
    function holderOf(el) {
      if (CHOICE[el.type]) return el.closest("fieldset") || el.closest(".checkbox-list") || el.closest(".field") || el.closest("label") || el.parentElement;
      return el.closest(".field") || el.parentElement;
    }

    function syncStars(form) {
      var live = {};
      controls(form).forEach(function (el) {
        if (!el.required || el.disabled) return;
        var node = labelNode(el);
        if (!node) return;
        if (!el._rid) el._rid = String(++uid);
        live[el._rid] = true;
        var star = node.querySelector('.req[data-for="' + el._rid + '"]') || node.querySelector(".req");
        if (!star) {
          star = document.createElement("span");
          star.className = "req";
          star.setAttribute("aria-hidden", "true");
          star.textContent = "*";
          node.appendChild(star);
        }
        star.setAttribute("data-for", el._rid);
      });
      Array.prototype.forEach.call(form.querySelectorAll(".req[data-for]"), function (star) {
        if (!live[star.getAttribute("data-for")]) {
          // Another required control may still share this label; keep the star for it.
          var shared = false;
          var label = star.parentNode;
          controls(form).forEach(function (el) { if (el.required && !el.disabled && labelNode(el) === label) shared = true; });
          if (!shared) star.remove();
        }
      });
    }

    function setup(form) {
      form.noValidate = true;
      var state = { items: [], summary: null };
      form._reqState = state;

      function summaryText() {
        var miss = state.items.filter(function (i) { return i.missing; }).length;
        var bad = state.items.length - miss;
        if (miss && bad) return tr("还有 {missing} 项未填写，另有 {invalid} 项格式不正确", {missing:miss, invalid:bad});
        if (miss) return tr("还有 {count} 项未填写", {count:miss});
        return tr("有 {count} 项格式不正确，请检查后再提交", {count:bad});
      }
      function renderSummary() {
        if (!state.items.length) {
          if (state.summary) { state.summary.remove(); state.summary = null; }
          return;
        }
        if (!state.summary) {
          state.summary = document.createElement("div");
          state.summary.className = "form-summary";
          state.summary.setAttribute("role", "alert");
          state.summary.setAttribute("tabindex", "-1");
          form.insertBefore(state.summary, form.firstChild);
        }
        state.summary.textContent = summaryText();
      }
      function clearItem(item) {
        item.group.forEach(function (x) { x.classList.remove("input-error"); x.removeAttribute("aria-invalid"); });
        item.holder.classList.remove("has-error");
        if (item.msg) item.msg.remove();
      }
      function show(el, p) {
        var holder = holderOf(el);
        var group = groupOf(el);
        var msg = document.createElement("div");
        msg.className = "field-msg";
        msg.setAttribute("role", "alert");
        msg.textContent = p.text;
        holder.appendChild(msg);
        holder.classList.add("has-error");
        group.forEach(function (x) { x.classList.add("input-error"); x.setAttribute("aria-invalid", "true"); });
        return { el: el, group: group, holder: holder, msg: msg, missing: p.missing };
      }
      function revalidate() {
        // Fields the user fixed (or that became hidden) lose their red frame immediately.
        var keep = [];
        state.items.forEach(function (item) {
          var p = isHidden(item.el) ? null : problem(item.el);
          if (p) {
            item.msg.textContent = p.text;
            item.missing = p.missing;
            keep.push(item);
          } else {
            clearItem(item);
          }
        });
        state.items = keep;
        renderSummary();
      }
      function validate() {
        state.items.forEach(clearItem);
        state.items = [];
        var seen = {};
        controls(form).forEach(function (el) {
          if (isHidden(el)) return;
          if (CHOICE[el.type] && el.name) {
            var key = el.type + ":" + el.name;
            if (seen[key]) return;
            seen[key] = true;
          }
          var p = problem(el);
          if (p) state.items.push(show(el, p));
        });
        renderSummary();
        return state.items;
      }

      form.addEventListener("submit", function (event) {
        if (event.submitter && event.submitter.hasAttribute("formnovalidate")) return;
        var items = validate();
        if (!items.length) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        var first = items[0].el;
        var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
        first.scrollIntoView({ block: "center", behavior: reduce ? "auto" : "smooth" });
        try { first.focus({ preventScroll: true }); } catch (e) { first.focus(); }
      }, true);
      ["input", "change"].forEach(function (name) {
        form.addEventListener(name, function () { if (state.items.length) revalidate(); });
      });

      syncStars(form);
      if (window.MutationObserver) {
        var queued = false;
        new MutationObserver(function () {
          if (queued) return;
          queued = true;
          requestAnimationFrame(function () {
            queued = false;
            syncStars(form);
            if (state.items.length) revalidate();
          });
        }).observe(form, { childList: true, subtree: true, attributes: true, attributeFilter: ["required", "disabled", "hidden"] });
      }
    }

    $all("form").forEach(setup);
  })();

  /* ---------------------------------------------------------------- captain login: masked name */
  (function captainLookup() {
    var input = document.querySelector("input[data-name-lookup]");
    var box = document.getElementById("captain-greeting");
    if (!input || !box) return;
    var endpoint = input.getAttribute("data-name-lookup");
    var cache = {};
    var timer = null;
    var controller = null;
    var lastShown = null;

    function render(state, data) {
      box.className = "captain-greeting";
      box.textContent = "";
      if (state === "idle") { box.hidden = true; return; }
      box.hidden = false;
      if (state === "loading") {
        box.classList.add("loading");
        box.textContent = tr("正在查询…");
        return;
      }
      if (state === "found") {
        box.classList.add("ok");
        var hello = document.createElement("div");
        hello.className = "hello";
        hello.textContent = data.greeting || "";
        var hint = document.createElement("div");
        hint.className = "self-hint";
        hint.textContent = data.hint || "";
        box.appendChild(hello);
        box.appendChild(hint);
        return;
      }
      box.classList.add("err");
      box.textContent = (data && data.error) || tr("未找到该队长账号");
    }

    function lookup() {
      var sid = (input.value || "").trim();
      if (timer) { clearTimeout(timer); timer = null; }
      if (!sid) { lastShown = null; render("idle"); return; }
      if (sid === lastShown) return;
      if (cache[sid]) { lastShown = sid; render(cache[sid].found ? "found" : "missing", cache[sid]); return; }
      if (controller) controller.abort();
      controller = window.AbortController ? new AbortController() : null;
      render("loading");
      fetch(endpoint + "?student_id=" + encodeURIComponent(sid), {
        headers: { "Accept": "application/json" },
        credentials: "same-origin",
        signal: controller ? controller.signal : undefined
      })
        .then(function (resp) {
          return resp.json().then(function (data) { return { status: resp.status, data: data || {} }; });
        })
        .then(function (result) {
          if (sid !== (input.value || "").trim()) return;
          lastShown = sid;
          if (result.status === 200) cache[sid] = result.data;  // never cache 429 / errors
          render(result.data.found ? "found" : "missing", result.data);
        })
        .catch(function (error) {
          if (error && error.name === "AbortError") return;
          if (sid !== (input.value || "").trim()) return;
          lastShown = null;
          render("missing", { error: tr("姓名查询暂时不可用，请稍后重试。") });
        });
    }

    input.addEventListener("input", function () {
      if (timer) clearTimeout(timer);
      if (!(input.value || "").trim()) { lookup(); return; }
      timer = setTimeout(lookup, 450);
    });
    input.addEventListener("blur", lookup);
    input.addEventListener("keydown", function (event) {
      // Enter in the student-ID box moves to the password instead of submitting half a form.
      var password = document.getElementById("password");
      if (event.key === "Enter" && password && !password.value) {
        event.preventDefault();
        lookup();
        password.focus();
      }
    });
    if ((input.value || "").trim()) lookup();
  })();

  /* ---------------------------------------------------------------- password rules (live + on submit) */
  (function passwordRules() {
    var form = document.getElementById("password-form");
    var list = document.getElementById("pw-rules");
    if (!form || !list) return;
    var current = document.getElementById("current_password");
    var neu = document.getElementById("new_password");
    var confirmInput = document.getElementById("confirm_password");
    var summary = document.getElementById("pw-rules-summary");
    var studentId = neu.getAttribute("data-student-id") || "";
    var minLength = parseInt(neu.getAttribute("data-min-length") || "6", 10);
    var soft = neu.getAttribute("data-soft") === "1";

    function evaluate() {
      var value = neu.value || "";
      return {
        min_length: value.length >= minLength,
        has_upper: /[A-Z]/.test(value),
        has_lower: /[a-z]/.test(value),
        has_special: /[^A-Za-z0-9]/.test(value),
        not_student_id: !studentId || value !== studentId,
        not_old_password: !current.value || value !== current.value,
        confirmed: value.length > 0 && value === (confirmInput.value || "")
      };
    }
    function refresh() {
      var results = evaluate();
      var done = 0, total = 0;
      $all("li[data-rule]", list).forEach(function (li) {
        var ok = !!results[li.getAttribute("data-rule")];
        total += 1; if (ok) done += 1;
        li.className = ok ? "ok" : "bad";
        var mark = $(".mark", li);
        if (mark) mark.textContent = ok ? "✓" : "○";
      });
      if (summary) summary.textContent = tr("已满足 {done} / {total} 项", {done:done,total:total});
      return done === total;
    }
    [current, neu, confirmInput].forEach(function (el) {
      ["input", "blur", "change"].forEach(function (evt) { el.addEventListener(evt, refresh); });
    });
    refresh();
    form.addEventListener("submit", function (event) {
      if (soft || refresh()) return;
      event.preventDefault();
      if (summary) summary.textContent = tr("还有规则未满足，请按上方提示调整后再保存。");
      list.closest(".pw-rules-box").classList.add("attention");
      neu.focus();
    }, true);
  })();

  /* ---------------------------------------------------------------- booking form */
  (function bookingForm() {
    var form = document.getElementById("booking-form");
    if (!form) return;
    var lab = document.getElementById("lab_id");
    var dateInput = document.getElementById("date");
    var start = document.getElementById("start_time");
    var end = document.getElementById("end_time");
    var hint = document.getElementById("duration-hint");
    var scheduleLink = document.getElementById("schedule-link");
    var maxMinutes = parseInt(form.getAttribute("data-max-minutes") || "60", 10);
    var lastDuration = null;

    if (dateInput && !dateInput.getAttribute("data-locked-min")) {
      // "Today" is the platform's date (Beijing time, rendered by the server), not the visitor's device date.
      var todayMeta = document.querySelector('meta[name="app-today"]');
      var iso = todayMeta && todayMeta.content;
      if (iso && (!dateInput.min || dateInput.min > iso)) dateInput.min = iso;
    }
    function selectedLab() { return lab && lab.selectedIndex >= 0 ? lab.options[lab.selectedIndex] : null; }
    function openHours() {
      var opt = selectedLab();
      return {
        start: toMinutes((opt && opt.getAttribute("data-open-start")) || "00:00"),
        end: toMinutes((opt && opt.getAttribute("data-open-end")) || "23:59")
      };
    }
    function syncHours() {
      var opt = selectedLab();
      if (!opt || !opt.getAttribute("data-open-start")) return;
      [start, end].forEach(function (el) {
        el.min = opt.getAttribute("data-open-start");
        el.max = opt.getAttribute("data-open-end");
      });
    }
    function syncScheduleLink() {
      if (!scheduleLink) return;
      var params = new URLSearchParams();
      if (lab && lab.value) params.set("lab_id", lab.value);
      if (dateInput && dateInput.value) params.set("date", dateInput.value);
      scheduleLink.href = scheduleLink.getAttribute("data-base") + (params.toString() ? "?" + params.toString() : "");
    }
    function setHint(text, bad) {
      if (!hint) return;
      hint.textContent = text;
      hint.classList.toggle("bad-hint", !!bad);
    }
    function checkDuration() {
      var a = toMinutes(start.value), b = toMinutes(end.value);
      if (a == null || b == null) return;
      var hours = openHours();
      if (b <= a) return setHint(tr("结束时间必须晚于开始时间。"), true);
      var mins = b - a;
      if (mins > maxMinutes) return setHint(tr("单次时长不得超过 1 小时（当前 {minutes} 分钟）。", {minutes:mins}), true);
      if (a < hours.start || b > hours.end) return setHint(tr("所选时段超出该实验室开放时间（{start}–{end}）。", {start:fromMinutes(hours.start),end:fromMinutes(hours.end)}), true);
      lastDuration = mins;
      setHint(tr("单次时长 {minutes} 分钟，不得超过 1 小时，且须在实验室开放时间内。与他人已通过预约重叠不超过 30 分钟不算冲突。", {minutes:mins}), false);
    }
    function setDuration(minutes) {
      var a = toMinutes(start.value);
      if (a == null) return;
      var limit = openHours().end;
      end.value = fromMinutes(Math.min(a + minutes, limit, 23 * 60 + 59));
      checkDuration();
    }
    $all("[data-duration]", form).forEach(function (chip) {
      chip.addEventListener("click", function () { setDuration(parseInt(chip.getAttribute("data-duration"), 10)); });
    });
    start.addEventListener("change", function () {
      // Keep the previously chosen length when the start moves (commonest edit on phones).
      if (lastDuration) setDuration(lastDuration); else checkDuration();
    });
    end.addEventListener("change", checkDuration);
    if (lab) lab.addEventListener("change", function () { syncHours(); checkDuration(); syncScheduleLink(); });
    if (dateInput) dateInput.addEventListener("change", syncScheduleLink);
    syncHours();
    checkDuration();
    syncScheduleLink();

    var includeOther = document.getElementById("include_other");
    var otherBox = document.getElementById("other-box");
    var otherRows = document.getElementById("other-rows");
    var addOther = document.getElementById("add-other");
    var template = document.getElementById("other-row-template");
    function syncOther() {
      if (!otherBox || !includeOther) return;
      otherBox.hidden = !includeOther.checked;
      $all("input", otherBox).forEach(function (input) {
        input.disabled = !includeOther.checked;
        input.required = includeOther.checked;
      });
    }
    function updateCount() {
      var counter = document.getElementById("companion-count");
      if (!counter) return;
      var members = $all('input[name="companion_member"]:checked', form).length;
      var others = includeOther && includeOther.checked ? $all('#other-rows .other-row', form).length : 0;
      counter.textContent = tr("共 {count} 人（含队长）", {count:1+members+others});
    }
    if (includeOther) includeOther.addEventListener("change", function () { syncOther(); updateCount(); });
    $all('input[name="companion_member"]', form).forEach(function (box) { box.addEventListener("change", updateCount); });
    if (addOther && otherRows && template) addOther.addEventListener("click", function () {
      otherRows.appendChild(template.content.cloneNode(true));
      syncOther();
      updateCount();
      var inputs = $all(".other-row:last-child input", otherRows);
      if (inputs[0]) inputs[0].focus();
    });
    if (otherRows) otherRows.addEventListener("click", function (event) {
      var remove = event.target.closest(".js-remove-row");
      if (!remove) return;
      var rows = $all(".other-row", otherRows);
      var row = remove.closest(".other-row");
      if (rows.length > 1) row.remove();
      else $all("input", row).forEach(function (input) { input.value = ""; });
      updateCount();
    });
    syncOther();
    updateCount();
  })();

  /* ---------------------------------------------------------------- tool borrowing form */
  (function toolForm() {
    var rows = document.getElementById("tool-rows");
    if (!rows) return;
    var add = document.getElementById("add-tool");
    var template = document.getElementById("tool-row-template");
    var borrowAt = document.getElementById("borrow_at");
    var expected = document.getElementById("expected_return_at");
    function bindRow(row) {
      var select = $(".js-tool-choice", row);
      var wrap = $(".js-other-wrap", row);
      var name = wrap ? $("input", wrap) : null;
      function sync() {
        var isOther = select && select.value === "other";
        if (wrap) wrap.hidden = !isOther;
        // Never disable: the server pairs tool_catalog_id / tool_name / tool_quantity by index.
        if (name) name.required = !!isOther;
      }
      if (select) select.addEventListener("change", sync);
      sync();
    }
    $all(".tool-row", rows).forEach(bindRow);
    if (add && template) add.addEventListener("click", function () {
      rows.appendChild(template.content.cloneNode(true));
      var row = rows.lastElementChild;
      bindRow(row);
      var select = $("select", row);
      if (select) select.focus();
    });
    rows.addEventListener("click", function (event) {
      var remove = event.target.closest(".js-remove-row");
      if (!remove) return;
      var all = $all(".tool-row", rows);
      if (all.length > 1) remove.closest(".tool-row").remove();
    });
    function syncReturnMin() {
      if (borrowAt && expected && borrowAt.value) expected.min = borrowAt.value;
    }
    function addHours(hours) {
      if (!borrowAt || !borrowAt.value || !expected) return;
      var base = new Date(borrowAt.value);
      if (isNaN(base.getTime())) return;
      base.setMinutes(base.getMinutes() + hours * 60);
      expected.value = base.getFullYear() + "-" + pad(base.getMonth() + 1) + "-" + pad(base.getDate()) + "T" + pad(base.getHours()) + ":" + pad(base.getMinutes());
    }
    $all("[data-return-after]").forEach(function (chip) {
      chip.addEventListener("click", function () { addHours(parseFloat(chip.getAttribute("data-return-after"))); });
    });
    if (borrowAt) borrowAt.addEventListener("change", syncReturnMin);
    syncReturnMin();
  })();

  /* ---------------------------------------------------------------- photo preview (camera upload) */
  $all('input[type="file"][data-preview]').forEach(function (input) {
    var target = document.getElementById(input.getAttribute("data-preview"));
    if (!target || !window.FileReader) return;
    input.addEventListener("change", function () {
      target.textContent = "";
      var file = input.files && input.files[0];
      if (!file) { target.hidden = true; return; }
      var info = document.createElement("span");
      info.className = "hint";
      info.textContent = file.name + " · " + (file.size / 1024 / 1024).toFixed(1) + " MB" + (file.size > 5 * 1024 * 1024 ? tr("（超过 5MB，请重新拍摄或压缩）") : "");
      if (file.size > 5 * 1024 * 1024) info.classList.add("bad-hint");
      target.hidden = false;
      if (/^image\//.test(file.type) && file.size <= 5 * 1024 * 1024) {
        var reader = new FileReader();
        reader.onload = function () {
          var img = document.createElement("img");
          img.alt = tr("照片预览");
          img.src = reader.result;  // data: URL (allowed by the CSP img-src)
          target.insertBefore(img, target.firstChild);
        };
        reader.readAsDataURL(file);
      }
      target.appendChild(info);
    });
  });

  /* ---------------------------------------------------------------- locked booking: contact dialog */
  (function lockedCancel() {
    var modal = document.getElementById("lock-modal");
    if (!modal) return;
    var notice = document.getElementById("lock-notice");
    var contacts = document.getElementById("lock-contacts");
    var closeBtn = document.getElementById("lock-close");
    $all(".js-locked-cancel").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (notice) notice.textContent = btn.getAttribute("data-notice") || "";
        var people = btn.getAttribute("data-contacts");
        if (contacts) contacts.textContent = people ? tr("请联系：") + people : "";
        modal.hidden = false;
      });
    });
    if (closeBtn) closeBtn.addEventListener("click", function () { modal.hidden = true; });
    if (new URLSearchParams(window.location.search).get("show_lock") === "1") {
      var first = $(".js-locked-cancel");
      if (first) first.click();
    }
  })();

  /* ---------------------------------------------------------------- auto refresh */
  // Reloads the page every AUTO_REFRESH_SECONDS (meta tag) so lists stay current. It never discards
  // work: it waits while a form is dirty/focused, a dialog or the scanner is open, a submit is running
  // or the tab is hidden, and retries on the next tick.
  (function autoRefresh() {
    var meta = $('meta[name="auto-refresh-seconds"]');
    var seconds = meta ? parseInt(meta.getAttribute("content"), 10) || 0 : 0;
    var key = "autorefresh-scroll:" + location.pathname + location.search;
    try {
      var saved = sessionStorage.getItem(key);
      if (saved !== null) {
        sessionStorage.removeItem(key);
        var y = parseInt(saved, 10) || 0;
        var restore = function () { window.scrollTo(0, y); };
        restore();
        window.addEventListener("load", restore);
      }
    } catch (e) { /* storage unavailable */ }
    if (seconds <= 0) return;

    var methodMeta = $('meta[name="request-method"]');
    var isPostResult = methodMeta && !/^(GET|HEAD)$/i.test(methodMeta.getAttribute("content") || "GET");
    var loadedAt = Date.now();
    var submitting = false;
    document.addEventListener("submit", function (event) { if (!event.defaultPrevented) submitting = true; });
    window.addEventListener("pageshow", function () { submitting = false; loadedAt = Date.now(); });

    function fieldDirty(el) {
      var type = (el.type || "").toLowerCase();
      if (el.tagName === "SELECT") {
        var options = $all("option", el);
        if (el.multiple) return options.some(function (o) { return o.selected !== o.defaultSelected; });
        // A single select with no `selected` attribute shows its first option: that is the default, not an edit.
        var def = 0;
        for (var i = 0; i < options.length; i++) { if (options[i].defaultSelected) { def = i; break; } }
        return el.selectedIndex !== def;
      }
      if (type === "hidden" || type === "submit" || type === "button" || type === "reset" || type === "image") return false;
      if (type === "checkbox" || type === "radio") return el.checked !== el.defaultChecked;
      if (type === "file") return !!(el.files && el.files.length);
      return el.value !== el.defaultValue;
    }
    function blockedReason() {
      if (document.hidden) return "hidden";
      if ($(".noshow-overlay, .modal:not([hidden]), .ck-modal:not([hidden]), dialog[open], [role=dialog]:not([hidden]):not(.modal), [role=alertdialog]")) return "dialog";
      if ($("[data-scanning]")) return "scanner";
      if (submitting || $("form[data-submitting='1'], .is-busy")) return "submitting";
      var active = document.activeElement;
      if (active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName) && !/^(button|submit|reset|image)$/i.test(active.type || "")) return "focused";
      if (active && active.isContentEditable) return "focused";
      if ($all("input, textarea, select").some(fieldDirty)) return "dirty";
      return "";
    }
    function refreshNow() {
      try { sessionStorage.setItem(key, String(Math.round(window.pageYOffset || document.documentElement.scrollTop || 0))); } catch (e) { /* ignore */ }
      // A POST result is re-fetched with GET (never re-submits the form).
      if (isPostResult) window.location.replace(window.location.href);
      else window.location.reload();
    }
    function attempt() {
      if (Date.now() - loadedAt < seconds * 1000 - 500) return;
      if (blockedReason()) return;
      refreshNow();
    }
    window.__autoRefresh = { seconds: seconds, blockedReason: blockedReason, attempt: attempt };
    window.setInterval(attempt, seconds * 1000);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && Date.now() - loadedAt >= seconds * 1000) attempt();
    });
  })();
})();
