/* Shared helpers for the AureoClean public pages. */
(function () {
  "use strict";
  var cfg = window.AUREOCLEAN || {};
  var ABC = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";

  function randomId(n) {
    var bytes = new Uint8Array(n), out = "";
    crypto.getRandomValues(bytes);
    for (var i = 0; i < n; i++) out += ABC[bytes[i] % ABC.length];
    return out;
  }

  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode: ignore */ } }
  };

  // One random ID per phone/computer browser, kept on the device. It is the
  // sign-up's document ID, so the database itself refuses a second sign-up
  // from the same device. (Clearing the browser's data creates a new ID.)
  var memoryId = null;
  function deviceId() {
    var id = store.get("aureoclean.device");
    if (!/^[A-Za-z0-9]{24}$/.test(id || "")) {
      id = memoryId || randomId(24);
      store.set("aureoclean.device", id);
      memoryId = id;
    }
    return id;
  }

  function normalizePhone(raw) {
    var d = String(raw || "").replace(/[^0-9]/g, "");
    if (d.indexOf("63") === 0) d = d.slice(2);
    if (d.indexOf("0") === 0) d = d.slice(1);
    return /^9[0-9]{9}$/.test(d) ? "+63" + d : null;
  }

  function prettyPhone(e164) { return e164.replace(/^\+63(\d{3})(\d{3})(\d{4})$/, "0$1 $2 $3"); }

  // Firestore value helpers
  var v = {
    str: function (s) { return { stringValue: String(s) }; },
    bool: function (b) { return { booleanValue: !!b }; },
    int: function (n) { return { integerValue: String(n | 0) }; },
    list: function (arr) { return { arrayValue: { values: arr.map(function (s) { return { stringValue: s }; }) } }; }
  };

  // Create a document (never overwrite), with createdAt set by Google's clock.
  function createDoc(collection, docId, fields) {
    var base = "projects/" + cfg.projectId + "/databases/(default)/documents";
    var url = "https://firestore.googleapis.com/v1/" + base + ":commit" +
              (cfg.apiKey ? "?key=" + encodeURIComponent(cfg.apiKey) : "");
    var body = { writes: [{
      update: { name: base + "/" + collection + "/" + docId, fields: fields },
      updateTransforms: [{ fieldPath: "createdAt", setToServerValue: "REQUEST_TIME" }],
      currentDocument: { exists: false }
    }] };
    return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(body), referrerPolicy: "no-referrer", credentials: "omit" })
      .then(function (res) {
        if (res.ok) return { ok: true };
        return res.text().then(function (t) { return { ok: false, status: res.status, text: t }; });
      }, function () { return { ok: false, status: 0, text: "offline" }; });
  }

  function reason(r) {
    try {
      var e = JSON.parse(r.text);
      e = (e[0] || e).error || {};
      return e.message ? " (Details: " + String(e.message).slice(0, 160) + ")" : "";
    } catch (x) { return ""; }
  }

  window.Aureo = { cfg: cfg, randomId: randomId, deviceId: deviceId, store: store, v: v,
                   createDoc: createDoc, reason: reason, normalizePhone: normalizePhone, prettyPhone: prettyPhone };
})();
