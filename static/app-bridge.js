// Talks to the Campfire page hosting this frame. Only messages from the configured host origin
// are trusted; everything is posted to that origin only.
(function () {
  const script = document.currentScript;
  let hostOrigin = (script && script.dataset.hostOrigin) || "http://localhost:3000";
  const pending = new Map();

  function newId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return String(Date.now()) + Math.random().toString(16).slice(2);
  }

  function post(type, payload, requestId) {
    const message = { source: "campfire-app-bridge", version: 1, type: type };
    if (requestId) message.requestId = requestId;
    if (payload !== undefined) message.payload = payload;
    window.parent.postMessage(message, hostOrigin);
  }

  window.addEventListener("message", function (event) {
    if (event.origin !== hostOrigin || event.source !== window.parent) return;
    const msg = event.data;
    if (!msg || msg.source !== "campfire-host" || msg.version !== 1) return;
    const waiter = pending.get(msg.requestId);
    if (msg.type === "context" && waiter) {
      pending.delete(msg.requestId);
      clearTimeout(waiter.timer);
      waiter.resolve(msg.payload);
    }
  });

  window.CampfireBridge = {
    configure: function (options) {
      if (options && options.hostOrigin) hostOrigin = options.hostOrigin;
    },
    ready: function (timeoutMs) {
      return new Promise(function (resolve, reject) {
        const id = newId();
        const timer = setTimeout(function () {
          pending.delete(id);
          reject(new Error("Campfire did not answer; open this from an invoice in Campfire."));
        }, timeoutMs || 5000);
        pending.set(id, { resolve: resolve, timer: timer });
        post("ready", undefined, id);
      });
    },
    toast: function (level, message) { post("toast", { level: level, message: message }); },
    refresh: function () { post("refresh"); },
    resize: function (height) { post("resize", { height: Math.ceil(height || document.body.scrollHeight) }); },
    close: function () { post("close"); },
    openUrl: function (url) { post("open_url", { url: url }); },
  };
})();
