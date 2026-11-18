/* In-page QR check-in: pledge -> camera scan (jsQR, vendored) -> POST /checkin/<token> with CSRF.
 * The server stays authoritative (window, lab, pledge, location). This file only gathers the scan.
 */
(function () {
  "use strict";
  var tr = window.appTranslate || function(text){ return text; };
  var modal = document.getElementById("ck-modal");
  if (!modal) return;
  function $(s) { return modal.querySelector(s); }

  var stepPledge = $("#ck-step-pledge"), stepScan = $("#ck-step-scan");
  var box = $("#ck-pledge"), startBtn = $("#ck-start");
  var video = $("#ck-video"), canvas = document.createElement("canvas");
  var statusEl = $("#ck-status"), fallback = $("#ck-fallback"), fileInput = $("#ck-file");
  var resultEl = $("#ck-result"), retryBtn = $("#ck-retry"), doneBtn = $("#ck-done");
  var geoEl = $("#ck-geo");
  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  var TOKEN_RE = /\/checkin\/([A-Za-z0-9_-]{8,64})(?:[\/?#]|$)/;

  var state = { bookingId: "", geo: false, stream: null, timer: null, busy: false, success: false, pos: null, posPromise: null };

  function show(el, on) { el.hidden = !on; }
  function setStatus(text, kind) { statusEl.textContent = text; statusEl.className = "ck-status" + (kind ? " ck-" + kind : ""); }
  function showResult(text, kind) { resultEl.textContent = text; resultEl.className = "ck-result ck-" + kind; resultEl.hidden = !text; }

  /* ---------------------------------------------------------- location */
  function requestPosition() {
    state.pos = null;
    if (!navigator.geolocation) {
      state.posPromise = Promise.reject(new Error(tr("当前浏览器不支持定位，无法在需要位置验证的实验室签到。")));
      state.posPromise.catch(function () {});
      return state.posPromise;
    }
    geoEl.hidden = false; geoEl.textContent = tr("正在获取你的位置…");
    state.posPromise = new Promise(function (resolve, reject) {
      navigator.geolocation.getCurrentPosition(function (p) {
        state.pos = { lat: p.coords.latitude, lng: p.coords.longitude, accuracy: p.coords.accuracy };
        geoEl.textContent = tr("已获取位置（误差约 {meters} 米）。", {meters:Math.round(p.coords.accuracy)});
        resolve(state.pos);
      }, function (err) {
        var msg = err && err.code === 1 ? tr("位置权限被拒绝。本实验室要求位置验证，请在浏览器设置里允许获取位置后重试。")
          : err && err.code === 3 ? tr("获取位置超时，请到窗边或门口重试。") : tr("无法获取位置，请确认已开启手机定位后重试。");
        geoEl.textContent = msg;
        reject(new Error(msg));
      }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
    });
    state.posPromise.catch(function () {});
    return state.posPromise;
  }

  /* ---------------------------------------------------------- camera */
  function stopCamera() {
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
    if (state.stream) { state.stream.getTracks().forEach(function (t) { t.stop(); }); state.stream = null; }
    video.srcObject = null;
    modal.removeAttribute("data-scanning");
  }

  function startCamera() {
    showResult("", "info"); show(retryBtn, false);
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.jsQR) {
      setStatus(tr("当前浏览器无法打开相机（需要 HTTPS 或本机地址）。请点下方按钮拍摄二维码照片，或用手机自带相机扫码。"), "warn");
      show(video, false); show(fallback, true);
      return;
    }
    setStatus(tr("正在打开相机…"));
    navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false }).then(function (stream) {
      state.stream = stream;
      video.srcObject = stream;
      video.setAttribute("playsinline", "true");
      show(video, true); show(fallback, true);
      return video.play();
    }).then(function () {
      modal.setAttribute("data-scanning", "1");
      setStatus(tr("请把镜头对准实验室门口的签到二维码。"));
      tick();
    }).catch(function () {
      stopCamera();
      show(video, false); show(fallback, true);
      setStatus(tr("相机无法使用或权限被拒绝。请允许相机权限，或点下方按钮拍摄二维码照片；也可以用手机自带相机扫码。"), "warn");
    });
  }

  function tick() {
    if (!state.stream) return;
    if (!state.busy && video.readyState >= 2 && video.videoWidth) {
      var w = video.videoWidth, h = video.videoHeight, scale = Math.min(1, 640 / Math.max(w, h));
      canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale);
      var ctx = canvas.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      var img = ctx.getImageData(0, 0, canvas.width, canvas.height);
      var code = window.jsQR(img.data, img.width, img.height, { inversionAttempts: "dontInvert" });
      if (code && code.data) { onDecoded(code.data); }
    }
    state.timer = setTimeout(tick, 150);
  }

  /* ---------------------------------------------------------- decode result -> POST */
  function tokenFrom(text) {
    var m = TOKEN_RE.exec(String(text || ""));
    return m ? m[1] : null;
  }

  function onDecoded(text) {
    var token = tokenFrom(text);
    if (!token) { setStatus(tr("这不是实验室签到二维码，请对准门口张贴的签到二维码。"), "warn"); return; }
    stopCamera();
    submit(token, false);
  }

  function submit(token, retriedGeo) {
    state.busy = true;
    setStatus(tr("已识别二维码，正在签到…"));
    var needGeo = state.geo || retriedGeo;
    var geoStep = needGeo ? (state.pos ? Promise.resolve(state.pos) : (state.posPromise || requestPosition())) : Promise.resolve(null);
    geoStep.then(function (pos) {
      var body = new FormData();
      body.append("csrf_token", csrf);
      body.append("pledge", "1");
      if (state.bookingId) body.append("booking_id", state.bookingId);
      if (pos) { body.append("lat", pos.lat); body.append("lng", pos.lng); body.append("accuracy", pos.accuracy); }
      return fetch("/checkin/" + encodeURIComponent(token), {
        method: "POST", body: body, credentials: "same-origin",
        headers: { "X-Requested-With": "fetch", "Accept": "application/json" }
      }).then(function (resp) {
        return resp.json().catch(function () { return { ok: false, message: tr("服务器返回了无法识别的结果（{status}）。", {status:resp.status}) }; });
      }).then(function (data) {
        if (!data.ok && data.code === "geo" && !pos && !retriedGeo) {
          state.pos = null; state.posPromise = null;
          return submit(token, true);
        }
        finish(data, token);
      });
    }).catch(function (err) {
      state.busy = false;
      setStatus("");
      showResult((err && err.message) || tr("签到失败，请重试。"), "error");
      show(retryBtn, true);
      retryBtn.onclick = function () { state.pos = null; state.posPromise = null; if (state.geo) requestPosition(); showResult("", "info"); show(retryBtn, false); startCamera(); };
    });
  }

  function finish(data, token) {
    state.busy = false;
    setStatus("");
    if (data.ok) {
      state.success = true;
      showResult("✓ " + data.title + "：" + data.message, "success");
      show(retryBtn, false); show(fallback, false); show(video, false); show(doneBtn, true);
      return;
    }
    showResult((data.title ? data.title + "：" : "") + (data.message || tr("签到失败。")), "error");
    show(retryBtn, true);
    retryBtn.onclick = function () {
      if (data.code === "geo") { state.pos = null; state.posPromise = null; requestPosition(); }
      showResult("", "info"); show(retryBtn, false); startCamera();
    };
  }

  /* ---------------------------------------------------------- photo fallback (also needs the pledge: step 2 only) */
  fileInput.addEventListener("change", function () {
    var file = fileInput.files && fileInput.files[0];
    fileInput.value = "";
    if (!file) return;
    if (!window.jsQR) { setStatus(tr("二维码识别组件未能加载，请刷新页面。"), "warn"); return; }
    setStatus(tr("正在识别照片中的二维码…"));
    // data: URL (not blob:) because the Content-Security-Policy only allows img-src 'self' data:
    var reader = new FileReader(), image = new Image();
    image.onload = function () {
      var found = null;
      [1600, 1000, 640].forEach(function (max) {
        if (found) return;
        var scale = Math.min(1, max / Math.max(image.naturalWidth, image.naturalHeight));
        canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
        canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
        var ctx = canvas.getContext("2d", { willReadFrequently: true });
        ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
        var data = ctx.getImageData(0, 0, canvas.width, canvas.height);
        found = window.jsQR(data.data, data.width, data.height, { inversionAttempts: "attemptBoth" });
      });
      if (found && found.data) { stopCamera(); onDecoded(found.data); }
      else { setStatus(tr("没有在照片里识别到二维码，请靠近一些、保持清晰后重拍。"), "warn"); }
    };
    image.onerror = function () { setStatus(tr("这张图片无法读取，请重新拍摄。"), "warn"); };
    reader.onload = function () { image.src = reader.result; };
    reader.onerror = image.onerror;
    reader.readAsDataURL(file);
  });

  /* ---------------------------------------------------------- modal plumbing */
  function openModal(button) {
    state.bookingId = button.getAttribute("data-booking-id") || "";
    state.geo = button.getAttribute("data-geo") === "1";
    state.success = false; state.busy = false; state.pos = null; state.posPromise = null;
    box.checked = false; startBtn.disabled = true;
    show(stepPledge, true); show(stepScan, false);
    showResult("", "info"); show(doneBtn, false); show(retryBtn, false); geoEl.hidden = true;
    $("#ck-lab").textContent = button.getAttribute("data-lab-name") || "";
    modal.hidden = false;
    document.body.classList.add("ck-open");
    box.focus();
  }

  function closeModal() {
    stopCamera();
    modal.hidden = true;
    document.body.classList.remove("ck-open");
    if (state.success) window.location.reload();
  }

  box.addEventListener("change", function () { startBtn.disabled = !box.checked; });
  startBtn.addEventListener("click", function () {
    if (!box.checked) return;
    show(stepPledge, false); show(stepScan, true); show(fallback, true); show(video, false);
    if (state.geo) requestPosition();
    startCamera();
  });
  $("#ck-cancel").addEventListener("click", closeModal);
  $("#ck-close").addEventListener("click", closeModal);
  doneBtn.addEventListener("click", closeModal);
  modal.addEventListener("click", function (e) { if (e.target === modal && !state.busy) closeModal(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !modal.hidden && !state.busy) closeModal(); });
  Array.prototype.forEach.call(document.querySelectorAll(".ck-open-btn"), function (b) {
    b.addEventListener("click", function () { openModal(b); });
  });
  window.addEventListener("pagehide", stopCamera);
})();
