/* Exact material quantities: Python Fraction on the server, BigInt rationals here. */
(function () {
  "use strict";
  function tr(text) { return window.appTranslate ? window.appTranslate(text) : text; }
  function gcd(a, b) { while (b) { var t = a % b; a = b; b = t; } return a; }
  function rational(n, d) {
    if (d <= 0n || n < 0n) return null;
    var g = gcd(n, d); return { n: n / g, d: d / g };
  }
  function parse(raw, trusted) {
    var value = String(raw).trim(), m, n, d;
    if (!value || (!trusted && value.length > 64)) return null;
    if (trusted && /^[0-9]+(?:\/[0-9]+)?$/.test(value)) {
      var stored = value.split("/"); return rational(BigInt(stored[0]), BigInt(stored[1] || "1"));
    }
    if ((m = value.match(/^([0-9]+)[ \t]+([0-9]+)[ \t]*\/[ \t]*([0-9]+)$/))) {
      var whole = BigInt(m[1]); n = BigInt(m[2]); d = BigInt(m[3]);
      if (n <= 0n || n >= d || d > 1000000000n) return null;
      n += whole * d;
    } else if ((m = value.match(/^([0-9]+)[ \t]*\/[ \t]*([0-9]+)$/))) {
      n = BigInt(m[1]); d = BigInt(m[2]); if (d > 1000000000n) return null;
    } else if (/^(?:[0-9]+(?:\.[0-9]{1,9})?|\.[0-9]{1,9})$/.test(value)) {
      var parts = value.split("."); var decimals = parts[1] || "";
      n = BigInt((parts[0] || "0") + decimals); d = 10n ** BigInt(decimals.length);
    } else { return null; }
    return rational(n, d);
  }
  function greater(a, b) { return a.n * b.d > b.n * a.d; }
  function quantityText(q) { return q.d === 1n ? String(q.n) : q.n + "/" + q.d; }
  function fmt(cents) { return String(cents / 100n) + "." + String(cents % 100n).padStart(2, "0"); }
  var zero = { n: 0n, d: 1n };
  function validate(input) {
    if (input.disabled || input.readOnly) { input.setCustomValidity(""); return zero; }
    var raw = input.value.trim();
    if (!raw) {
      input.setCustomValidity("");
      var emptyRow = input.closest(".mat-pick-row");
      var emptyHint = emptyRow && emptyRow.querySelector(".js-qty-error");
      if (emptyHint) { emptyHint.textContent = ""; emptyHint.hidden = true; }
      return zero;
    }
    var trusted = input.dataset.exactCurrent === "1" && raw === input.defaultValue;
    var q = parse(raw, trusted), error = "";
    if (!q || (!q.n && (input.dataset.allowZero !== "1" || (input.dataset.purchaseQty === "1" && raw !== "0")))) {
      error = tr("数量格式无效，请填写大于 0 的小数或分数，如 0.5、1/3、1 1/2。");
    } else {
      var maximum = parse(input.dataset.max || "1000000", true);
      if (greater(q, maximum)) error = tr("数量超过允许上限。");
    }
    input.setCustomValidity(error);
    var row = input.closest(".mat-pick-row");
    if (row) {
      var hint = row.querySelector(".js-qty-error");
      if (hint) { hint.textContent = error; hint.hidden = !error; }
    }
    return error ? null : q;
  }
  document.querySelectorAll(".quantity-input").forEach(function (input) {
    validate(input);
    input.addEventListener("input", function () { validate(input); });
    input.addEventListener("change", function () { validate(input); });
  });

  var pick = document.getElementById("mat-pick");
  if (pick) {
    var totalEl = document.getElementById("mat-total");
    var recalc = function () {
      var total = 0n, invalid = false;
      pick.querySelectorAll(".mat-pick-row").forEach(function (row) {
        var input = row.querySelector(".quantity-input");
        var price = BigInt(row.dataset.price || "0");
        var qty = validate(input);
        var sub = qty ? (2n * qty.n * price + qty.d) / (2n * qty.d) : 0n;
        total += sub; invalid = invalid || !qty;
        var subEl = row.querySelector(".js-sub");
        if (subEl) subEl.textContent = qty ? fmt(sub) : "—";
        row.classList.toggle("is-picked", Boolean(qty && qty.n > 0n));
      });
      if (totalEl) totalEl.textContent = invalid ? "—" : fmt(total);
    };
    pick.addEventListener("input", recalc);
    pick.addEventListener("change", recalc);
    pick.addEventListener("click", function (event) {
      var btn = event.target.closest(".qty-btn");
      if (!btn || btn.disabled) return;
      var input = btn.parentNode.querySelector(".quantity-input");
      if (!input || input.disabled) return;
      var qty = validate(input);
      if (!qty) { input.reportValidity(); return; }
      var next = qty.n + BigInt(btn.dataset.step) * qty.d;
      qty = next < 0n ? zero : rational(next, qty.d);
      var maximum = parse(input.dataset.max, true);
      if (greater(qty, maximum)) qty = maximum;
      input.value = quantityText(qty);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    recalc();
  }

  var timer = document.getElementById("code-timer");
  if (timer) {
    var left = parseInt(timer.getAttribute("data-seconds"), 10) || 0;
    var leftEl = timer.querySelector(".js-left");
    if (leftEl && left > 0) {
      var tick = setInterval(function () {
        left -= 1;
        if (left <= 0) {
          clearInterval(tick);
          timer.innerHTML = "";
          var b = document.createElement("b");
          b.className = "bad-hint";
          b.textContent = (window.appTranslate ? window.appTranslate("验证码已过期，请重新发送。") : "验证码已过期，请重新发送。");
          timer.appendChild(b);
          return;
        }
        leftEl.textContent = Math.floor(left / 60) + ":" + ("0" + (left % 60)).slice(-2);
      }, 1000);
    }
  }

  var resend = document.getElementById("resend-btn");
  if (resend) {
    var wait = parseInt(resend.getAttribute("data-wait"), 10) || 0;
    var waitEl = resend.querySelector(".js-wait");
    if (wait > 0 && waitEl) {
      var t2 = setInterval(function () {
        wait -= 1;
        if (wait <= 0) {
          clearInterval(t2);
          resend.textContent = resend.textContent.replace(/（\d+s）$/, "").replace(/\(\d+s\)$/, "");
          resend.disabled = false;
          return;
        }
        waitEl.textContent = wait;
      }, 1000);
    }
  }
})();
