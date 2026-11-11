"""Source login browser: a small browser window (Edge WebView2 through pywebview) for course sites that need a
login. The user signs in, opens the materials page and presses "Tải tài liệu mới từ trang này"; the window then
hands the page's links and the site's login cookies back to the app (as a JSON file) and closes.

Runs as its own process (`LiveTranslator.exe --source-browser <url> <out.json> <profile folder>`), because the
browser needs the main thread and the app's window already has it. The profile folder keeps the login, so the
next time the window opens already signed in.
"""
import json
import os
import sys

# A small bar pinned to the bottom of every page: the button collects every link on the page (also in frames
# from the same site) and gives them to Python.
TOOLBAR_JS = r"""
(function () {
  if (window.__ltBar || !document.body) return;
  var bar = document.createElement('div');
  window.__ltBar = bar;
  bar.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;background:#1f2937;color:#fff;' +
    'font:13px Segoe UI,Arial,sans-serif;padding:10px 12px;border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.35);' +
    'max-width:360px;line-height:1.4';
  bar.innerHTML = '<div style="margin-bottom:8px">Live Translator: đăng nhập, mở trang chứa tài liệu ' +
    '(Materials), rồi bấm nút dưới.</div>' +
    '<button id="__ltScan" style="background:#16a34a;color:#fff;border:0;border-radius:6px;padding:7px 12px;' +
    'font:600 13px Segoe UI,Arial,sans-serif;cursor:pointer">📥 Tải tài liệu mới từ trang này</button>' +
    '<span id="__ltMsg" style="margin-left:8px;color:#d1fae5"></span>';
  document.body.appendChild(bar);
  function collect(doc, out) {
    var as = doc.querySelectorAll('a[href]');
    for (var i = 0; i < as.length; i++) {
      var a = as[i];
      var t = (a.innerText || a.getAttribute('title') || a.getAttribute('aria-label') || '').trim();
      out.push({href: a.href, text: t.slice(0, 200), download: a.getAttribute('download') || ''});
    }
    var fr = doc.querySelectorAll('iframe,frame');
    for (var j = 0; j < fr.length; j++) {
      try { if (fr[j].contentDocument) collect(fr[j].contentDocument, out); } catch (e) {}
    }
  }
  var sent = false;
  function scan() {
    if (sent) return;
    sent = true;
    var links = [];
    collect(document, links);
    document.getElementById('__ltMsg').textContent = 'Đang gửi ' + links.length + ' link...';
    window.pywebview.api.scan({url: location.href, title: document.title, links: links,
                               ua: navigator.userAgent});
  }
  document.getElementById('__ltScan').onclick = scan;

  // Automatic: on the saved materials page (already signed in), wait until the page has finished adding
  // links, count down 3 s and scan by itself. "Huỷ tự động" stops it (to go to another page first).
  var target = __TARGET__;
  function same(a, b) {
    try { var x = new URL(a), y = new URL(b);
          return x.origin === y.origin && x.pathname.replace(/\/+$/, '') === y.pathname.replace(/\/+$/, '') &&
                 x.search === y.search; } catch (e) { return false; }
  }
  if (target && same(location.href, target)) {
    var msg = document.getElementById('__ltMsg'), cancelled = false, last = -1, stable = 0, waited = 0;
    var stop = document.createElement('a');
    stop.textContent = 'Huỷ tự động';
    stop.href = 'javascript:void 0';
    stop.style.cssText = 'margin-left:8px;color:#fca5a5';
    stop.onclick = function () { cancelled = true; msg.textContent = 'Đã huỷ — bấm nút khi sẵn sàng.'; stop.remove(); };
    bar.appendChild(stop);
    var timer = setInterval(function () {
      if (cancelled) { clearInterval(timer); return; }
      var n = document.querySelectorAll('a[href]').length;
      stable = (n === last) ? stable + 1 : 0; last = n; waited++;
      if (stable < 3 && waited < 16) { msg.textContent = 'Đang chờ trang tải xong...'; return; }
      clearInterval(timer);
      var left = 3;
      msg.textContent = 'Tự tải sau ' + left + ' giây...';
      var cd = setInterval(function () {
        if (cancelled) { clearInterval(cd); return; }
        left--;
        if (left > 0) { msg.textContent = 'Tự tải sau ' + left + ' giây...'; return; }
        clearInterval(cd); stop.remove(); scan();
      }, 1000);
    }, 500);
  }
})();
"""


def trace(msg):
    """Debug notes when LT_SOURCE_TRACE names a file (tests)."""
    path = os.environ.get("LT_SOURCE_TRACE")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


class Api:
    def __init__(self, out):
        self.out = out
        self._window = None                 # private: pywebview walks public attributes (huge, slow)

    def scan(self, page):
        """Called from the page's button: save links + login cookies, then close the window."""
        cookies = []
        try:
            for c in self._window.get_cookies() or []:
                for name, m in c.items():
                    cookies.append({"name": name, "value": m.value, "domain": m["domain"] or "",
                                    "path": m["path"] or "/"})
        except Exception as e:  # noqa: BLE001
            page["cookie_error"] = str(e)
        page["cookies"] = cookies
        tmp = self.out + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(page, f, ensure_ascii=False)
        os.replace(tmp, self.out)
        if sys.platform == "darwin":
            # Mac (WebKit): this call comes from the page and the page waits for it to return; closing the
            # window from here waited on that same page, so the window stayed open ("Đang gửi ... link") and
            # the app kept waiting. Return first and close a moment later; if the browser process still has
            # not ended after a few seconds, end it (the result file is already written).
            import threading
            import time

            def close():
                time.sleep(0.3)
                try:
                    self._window.destroy()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(4.0)
                os._exit(0)
            threading.Thread(target=close, daemon=True).start()
            return True
        self._window.destroy()


def run(url, out, profile):
    try:
        import webview
    except Exception as e:  # noqa: BLE001
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"error": f"không mở được cửa sổ trình duyệt: {e}"}, f, ensure_ascii=False)
        return
    os.makedirs(profile, exist_ok=True)
    api = Api(out)
    w = webview.create_window("Source — đăng nhập, mở trang tài liệu rồi bấm 📥 Tải tài liệu mới", url,
                              js_api=api, width=1200, height=860)
    api._window = w

    js = TOOLBAR_JS.replace("__TARGET__", json.dumps(url))

    def inject():
        try:
            w.evaluate_js(js)
            trace("injected on " + str(w.get_current_url()))
        except Exception as e:  # noqa: BLE001
            trace(f"inject failed: {e}")
    w.events.loaded += inject
    try:
        webview.start(private_mode=False, storage_path=profile)
    except Exception as e:  # noqa: BLE001
        if not os.path.exists(out):
            with open(out, "w", encoding="utf-8") as f:
                json.dump({"error": f"không mở được cửa sổ trình duyệt: {e}"}, f, ensure_ascii=False)


if __name__ == "__main__":
    run(*sys.argv[1:4])
