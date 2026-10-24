/* Swappable transport between the SPA and the Python plugin.
 *
 *   HttpTransport — fetch() for requests + Server-Sent Events for pushes (system browser).
 *   QtTransport   — QWebChannel object "bridge" (embedded QWebEngineView, when available).
 *
 * Both expose the same interface:
 *   request(method, path, body) -> Promise<json>
 *   upload(file, name, onProgress) -> Promise<json>
 *   connect(lastEventId, onEvent, onStatus)    // onStatus(true|false)
 */
(function () {
  "use strict";

  class HttpTransport {
    constructor() {
      this.kind = "http";
      this.source = null;
    }

    async request(method, path, body) {
      const options = { method: method, headers: {} };
      if (body !== undefined) {
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
      }
      const response = await fetch(path, options);
      let data = null;
      try { data = await response.json(); } catch (e) { data = null; }
      if (!response.ok) {
        const message = (data && data.error) || ("HTTP " + response.status);
        throw new Error(message);
      }
      return data;
    }

    upload(file, name, onProgress) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/upload?name=" + encodeURIComponent(name));
        xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
        xhr.onload = () => {
          let data = null;
          try { data = JSON.parse(xhr.responseText); } catch (e) { /* ignore */ }
          if (xhr.status >= 200 && xhr.status < 300) resolve(data);
          else reject(new Error((data && data.error) || ("HTTP " + xhr.status)));
        };
        xhr.onerror = () => reject(new Error("Сеть недоступна"));
        xhr.send(file);
      });
    }

    connect(lastEventId, onEvent, onStatus) {
      if (this.source) this.source.close();
      const source = new EventSource("/api/events?last=" + (lastEventId || 0));
      this.source = source;
      source.onopen = () => onStatus(true);
      source.onmessage = (message) => {
        try { onEvent(JSON.parse(message.data)); } catch (e) { console.error(e); }
      };
      source.onerror = () => {
        source.close();
        onStatus(false);
      };
    }
  }

  class QtTransport {
    constructor(bridge) {
      this.kind = "qt";
      this.bridge = bridge;
      this.pending = new Map();
      this.nextId = 1;
      bridge.response.connect((text) => {
        const reply = JSON.parse(text);
        const waiter = this.pending.get(reply.id);
        if (!waiter) return;
        this.pending.delete(reply.id);
        let data = null;
        try { data = reply.body ? JSON.parse(reply.body) : null; } catch (e) { data = null; }
        if (reply.status >= 400) waiter.reject(new Error((data && data.error) || ("HTTP " + reply.status)));
        else waiter.resolve(data);
      });
    }

    request(method, path, body, bodyBase64) {
      return new Promise((resolve, reject) => {
        const id = this.nextId++;
        this.pending.set(id, { resolve, reject });
        this.bridge.request(JSON.stringify({ id, method, path, body, bodyBase64 }));
      });
    }

    async upload(file, name, onProgress) {
      const buffer = await file.arrayBuffer();
      let binary = "";
      const bytes = new Uint8Array(buffer);
      for (let i = 0; i < bytes.length; i += 0x8000) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      }
      if (onProgress) onProgress(1);
      return this.request("POST", "/api/upload?name=" + encodeURIComponent(name), undefined, btoa(binary));
    }

    connect(lastEventId, onEvent, onStatus) {
      if (!this.connected) {
        this.bridge.event.connect((text) => {
          try { onEvent(JSON.parse(text)); } catch (e) { console.error(e); }
        });
        this.connected = true;
      }
      onStatus(true);
    }
  }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.onload = resolve;
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }

  async function createTransport() {
    const wantsQt = new URLSearchParams(location.search).get("transport") === "qt";
    if (wantsQt && window.qt && window.qt.webChannelTransport) {
      try {
        if (!window.QWebChannel) await loadScript("qrc:///qtwebchannel/qwebchannel.js");
        const bridge = await new Promise((resolve) => {
          new window.QWebChannel(window.qt.webChannelTransport, (channel) => resolve(channel.objects.bridge));
        });
        return new QtTransport(bridge);
      } catch (e) {
        console.warn("QWebChannel is not available, falling back to HTTP", e);
      }
    }
    return new HttpTransport();
  }

  window.Transport = { create: createTransport };
})();
