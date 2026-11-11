"""Source login browser: a small browser window (Edge WebView2 through pywebview) for course sites that need a
login. The user signs in, opens the materials (clicking a "Materials" tab if the site has one) and presses
"Tải tài liệu mới từ trang này"; the window hands the page's links and the site's login cookies back to the app
(as a JSON file) and closes.

Remembered for next time, so later runs need no clicks at all:
  - the login: the profile folder keeps cookies, and the site's session cookies (which a browser normally drops
    when it closes, e.g. Salesforce sites like exec.mit.edu) are re-saved as 30-day cookies;
  - the page and the clicks made on it before pressing the button (e.g. the "Materials" tab): replayed, then
    the page is scanned by itself.

Runs as its own process (`LiveTranslator.exe --source-browser <url> <out.json> <profile folder> [clicks json]`),
because the browser needs the main thread and the app's window already has it.
"""
import json
import os
import sys
import threading
import time

# A small bar pinned to the bottom of every page. It never scans a login page. Links are taken from the open
# tab's panel (e.g. "Materials") when there is one, else from the page without its header/footer/menus; also
# inside components (shadow DOM, used by Salesforce/Lightning sites) and frames from the same site.
TOOLBAR_JS = r"""
(function () {
  if (window.__ltBar || !document.body) return;
  var bar = document.createElement('div');
  window.__ltBar = bar;
  bar.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:2147483647;background:#1f2937;color:#fff;' +
    'font:13px Segoe UI,Arial,sans-serif;padding:10px 12px;border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.35);' +
    'max-width:400px;line-height:1.4';
  bar.innerHTML = '<div id="__ltHint" style="margin-bottom:8px">Live Translator: đăng nhập, bấm tab ' +
    '<b>Materials</b>, rồi bấm nút dưới. Lần sau app tự làm.</div>' +
    '<button id="__ltScan" style="background:#16a34a;color:#fff;border:0;border-radius:6px;padding:7px 12px;' +
    'font:600 13px Segoe UI,Arial,sans-serif;cursor:pointer">📥 Tải tài liệu mới từ trang này</button>' +
    '<div id="__ltMsg" style="margin-top:6px;color:#d1fae5"></div>';
  document.body.appendChild(bar);
  var msg = document.getElementById('__ltMsg');

  // every element of the page, also inside shadow roots and same-site frames
  function all(root, out) {
    var els = root.querySelectorAll('*');
    for (var i = 0; i < els.length; i++) {
      var e = els[i];
      out.push(e);
      if (e.shadowRoot) all(e.shadowRoot, out);
      if (e.tagName === 'IFRAME' || e.tagName === 'FRAME') {
        try { if (e.contentDocument) all(e.contentDocument, out); } catch (x) {}
      }
    }
    return out;
  }
  function mine(e) { return e === bar || bar.contains(e); }
  function visible(e) {
    if (!e.getClientRects || !e.getClientRects().length) return false;
    try { var s = getComputedStyle(e); return s.visibility !== 'hidden' && s.display !== 'none'; } catch (x) { return true; }
  }
  function label(e) {
    return (e.innerText || e.textContent || e.getAttribute('title') || e.getAttribute('aria-label') ||
            e.getAttribute('data-label') || '').replace(/\s+/g, ' ').trim().slice(0, 200);
  }
  function parentOf(e) {                           // crosses shadow boundaries
    var p = e.parentNode;
    if (p && p.host && p.nodeType === 11) return p.host;
    return p && p.nodeType === 1 ? p : null;
  }
  function inChrome(e) {                           // site header, footer, menus: not course materials
    for (var p = e; p; p = parentOf(p)) {
      var role = (p.getAttribute && p.getAttribute('role') || '').toLowerCase();
      if (/^(HEADER|FOOTER|NAV)$/.test(p.tagName) || /^(banner|contentinfo|navigation|menubar)$/.test(role)) return true;
    }
    return false;
  }
  function isLogin() {
    if (/\/(s\/)?login\b|sign-?in|\/sso\b|saml|oauth|\/auth\b|\/idp\//i.test(location.pathname)) return true;
    var els = all(document, []);
    for (var i = 0; i < els.length; i++) {
      if (els[i].tagName === 'INPUT' && (els[i].type || '').toLowerCase() === 'password' && visible(els[i])) return true;
    }
    return false;
  }
  function tabs() {
    var out = [], els = all(document, []);
    for (var i = 0; i < els.length; i++) {
      var e = els[i];
      if ((e.getAttribute('role') || '').toLowerCase() === 'tab' && !mine(e))
        out.push({el: e, text: label(e), selected: e.getAttribute('aria-selected') === 'true'});
    }
    return out;
  }
  // where the materials are: the panel of the open tab, else the whole page
  function scope() {
    var t = tabs(), sel = null;
    for (var i = 0; i < t.length; i++) if (t[i].selected && visible(t[i].el)) { sel = t[i]; break; }
    var panels = [];
    if (sel) {
      var id = sel.el.getAttribute('aria-controls'), root = sel.el.getRootNode();
      var p = id && root.getElementById ? root.getElementById(id) : null;
      if (!p && id) p = document.getElementById(id);
      if (p && visible(p)) panels.push(p);
      if (!panels.length) {
        var els = all(document, []);
        for (var j = 0; j < els.length; j++) {
          var e = els[j];
          if ((e.getAttribute('role') || '').toLowerCase() === 'tabpanel' && visible(e) && !mine(e)) panels.push(e);
        }
      }
    }
    return {panels: panels, tab: sel ? sel.text : '', tabs: t.map(function (x) { return (x.selected ? '*' : '') + x.text; })};
  }
  function links(sc) {
    var out = [], seen = {}, roots = sc.panels.length ? sc.panels : [document];
    for (var r = 0; r < roots.length; r++) {
      var els = [roots[r]].concat(all(roots[r], []));
      for (var i = 0; i < els.length; i++) {
        var a = els[i];
        if (a.tagName !== 'A' || !a.href || mine(a) || seen[a.href]) continue;
        if (!sc.panels.length && inChrome(a)) continue;
        seen[a.href] = 1;
        out.push({href: a.href, text: label(a), download: a.getAttribute('download') || ''});
      }
    }
    return out;
  }

  // remember the tabs/buttons the user clicks before pressing the button (e.g. "Materials"), to replay
  var clicks = [];
  document.addEventListener('click', function (ev) {
    var path = ev.composedPath ? ev.composedPath() : [ev.target];
    for (var i = 0; i < path.length; i++) {
      var e = path[i];
      if (!e || !e.getAttribute) continue;
      if (mine(e)) return;
      var role = (e.getAttribute('role') || '').toLowerCase();
      if (role === 'tab' || e.tagName === 'BUTTON' || role === 'button' || role === 'menuitem') {
        var t = label(e);
        if (t && t.length <= 60) clicks.push({text: t, role: role, tag: e.tagName});
        if (clicks.length > 5) clicks.shift();
        return;
      }
    }
  }, true);

  var sent = false;
  function scan() {
    if (sent) return;
    if (isLogin()) {
      msg.textContent = 'Đây là trang đăng nhập — hãy đăng nhập trước, rồi bấm tab Materials và bấm lại nút.';
      return;
    }
    var sc = scope(), l = links(sc);
    sent = true;
    msg.textContent = 'Đang gửi ' + l.length + ' link' + (sc.tab ? ' trong tab “' + sc.tab + '”' : '') + '...';
    window.pywebview.api.scan({url: location.href, title: document.title, links: l, ua: navigator.userAgent,
                               clicks: clicks.slice(-3), scope: sc.panels.length ? 'panel' : 'page',
                               tab: sc.tab, tabs: sc.tabs, panels: sc.panels.length});
  }
  document.getElementById('__ltScan').onclick = scan;

  // ---- automatic: on the saved page, open the Materials tab (saved clicks), then scan. Never on a login
  // page, and never when the tab is not found: then the user is asked to do it. ----
  var target = __TARGET__, saved = __CLICKS__;
  if (isLogin()) {
    msg.textContent = 'Hãy đăng nhập — sau đó app tự mở tab Materials và tải tài liệu.';
    return;
  }
  function same(a, b) {
    try { var x = new URL(a), y = new URL(b);
          return x.origin === y.origin && x.pathname.replace(/\/+$/, '') === y.pathname.replace(/\/+$/, '') &&
                 x.search === y.search; } catch (e) { return false; }
  }
  if (!target || !same(location.href, target)) return;
  var cancelled = false;
  var stop = document.createElement('a');
  stop.textContent = 'Huỷ tự động';
  stop.href = 'javascript:void 0';
  stop.style.cssText = 'margin-left:8px;color:#fca5a5';
  stop.onclick = function () { cancelled = true; msg.textContent = 'Đã huỷ — bấm nút khi sẵn sàng.'; stop.remove(); };
  bar.appendChild(stop);
  var MAT = /^(course\s+)?materials?$|^tài liệu$|^resources$/i;
  function find(c) {
    var want = c.text.toLowerCase(), els = all(document, []);
    for (var i = 0; i < els.length; i++) {
      var e = els[i];
      if (mine(e)) continue;
      var role = (e.getAttribute('role') || '').toLowerCase();
      if (c.text === '__auto__') {
        if ((role === 'tab' || e.tagName === 'A' || e.tagName === 'BUTTON' || e.tagName === 'LI') &&
            MAT.test(label(e)) && visible(e)) return e;
      } else if ((role === c.role || e.tagName === c.tag) && label(e).toLowerCase() === want && visible(e)) {
        return e;
      }
    }
    return null;
  }
  if (!saved || !saved.length) saved = [{text: '__auto__'}];   // first time: a tab called Materials
  var noTab = saved.length === 1 && saved[0].text === '__page__';  // last time the files were on the page itself
  // wait until the number of links stops changing (pages built by scripts), then call next
  function settle(next) {
    var last = -1, stable = 0, waited = 0;
    var t = setInterval(function () {
      if (cancelled) { clearInterval(t); return; }
      var n = all(document, []).length;
      stable = (n === last) ? stable + 1 : 0; last = n; waited++;
      if (stable >= 3 || waited >= 30) { clearInterval(t); next(); }
    }, 500);
  }
  var step = 0, opened = 0;
  function replay() {
    if (cancelled) return;
    if (noTab) return countdown();
    if (step >= saved.length) {
      if (!opened) {                               // not signed in yet, or the page changed: let the user do it
        cancelled = true; stop.remove();
        msg.textContent = 'Chưa thấy tab Materials — hãy đăng nhập / bấm tab Materials rồi bấm nút.';
        return;
      }
      return countdown();
    }
    var c = saved[step], tries = 0;
    msg.textContent = 'Đang mở ' + (c.text === '__auto__' ? 'tab Materials' : '“' + c.text + '”') + '...';
    var t = setInterval(function () {
      if (cancelled) { clearInterval(t); return; }
      var e = find(c);
      tries++;
      if (e || tries >= 16) {
        clearInterval(t);
        if (e) { opened++; try { e.click(); } catch (x) {} }
        step++;
        settle(replay);
      }
    }, 500);
  }
  function countdown() {
    var left = 3;
    msg.textContent = 'Tự tải sau ' + left + ' giây...';
    var cd = setInterval(function () {
      if (cancelled) { clearInterval(cd); return; }
      left--;
      if (left > 0) { msg.textContent = 'Tự tải sau ' + left + ' giây...'; return; }
      clearInterval(cd); stop.remove(); scan();
    }, 1000);
  }
  msg.textContent = 'Đang chờ trang tải xong...';
  settle(replay);
})();
"""


def trace(msg):
    """Debug notes when LT_SOURCE_TRACE names a file (tests)."""
    path = os.environ.get("LT_SOURCE_TRACE")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


def is_session(expires, max_age):
    """A session cookie (dropped when the browser closes). WebView2 reports those with a 1970/0001 date."""
    if max_age:
        return False
    if not expires:
        return True
    import re
    m = re.search(r"\b(\d{4})\b", expires)              # (email.utils reads year 0001 as 2001)
    return not m or int(m.group(1)) < 2000


def read_cookies(window):
    """[{name, value, domain, path, secure, httponly, session}] of the page's site."""
    out = []
    for c in window.get_cookies() or []:
        for name, m in c.items():
            out.append({"name": name, "value": m.value, "domain": m["domain"] or "", "path": m["path"] or "/",
                        "secure": bool(m["secure"]), "httponly": bool(m["httponly"]),
                        "session": is_session(m["expires"], m["max-age"]), "exp": m["expires"]})
    return out


def keep_session_cookies(window, cookies, host):
    """Re-save the site's session cookies as 30-day cookies in the browser profile, so the login survives
    closing the window (sites like Salesforce only set session cookies). Windows (WebView2) only."""
    trace("cookies: " + ", ".join(f"{c['name']}(session={c.get('session')}, exp={c.get('exp')})" for c in cookies))
    keep = [c for c in cookies if c.get("session")]
    if sys.platform != "win32" or not keep:
        return 0
    try:
        from System import Action, DateTime
        from Microsoft.Web.WebView2.Core import CoreWebView2CookieSameSiteKind
        form = window.native

        def work():
            mgr = form.browser.webview.CoreWebView2.CookieManager
            for c in keep:
                ck = mgr.CreateCookie(c["name"], c["value"], c["domain"] or host, c["path"] or "/")
                ck.Expires = DateTime.Now.AddDays(30)
                ck.IsHttpOnly = c["httponly"]
                ck.IsSecure = c["secure"]
                ck.SameSite = CoreWebView2CookieSameSiteKind["None"] if c["secure"] else \
                    CoreWebView2CookieSameSiteKind.Lax
                mgr.AddOrUpdateCookie(ck)
        form.Invoke(Action(work))
        return len(keep)
    except Exception as e:  # noqa: BLE001
        trace(f"keep cookies failed: {e}")
        return 0


class Api:
    def __init__(self, out):
        self.out = out
        self._window = None                 # private: pywebview walks public attributes (huge, slow)
        self._host = ""

    def scan(self, page):
        """Called from the page (button or automatic): save links + login cookies, then close the window."""
        cookies = []
        try:
            cookies = read_cookies(self._window)
            keep_session_cookies(self._window, cookies, self._host)
        except Exception as e:  # noqa: BLE001
            page["cookie_error"] = str(e)
        page["cookies"] = cookies
        tmp = self.out + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(page, f, ensure_ascii=False)
        os.replace(tmp, self.out)

        def close():                        # after returning to the page (closing inside the call can hang)
            time.sleep(0.3)
            try:
                self._window.destroy()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(4)
            os._exit(0)                     # the result is written; don't wait for a stuck browser
        threading.Thread(target=close, daemon=True).start()
        return True


def run(url, out, profile, clicks="[]"):
    try:
        import webview
    except Exception as e:  # noqa: BLE001
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"error": f"không mở được cửa sổ trình duyệt: {e}"}, f, ensure_ascii=False)
        return
    from urllib.parse import urlparse
    os.makedirs(profile, exist_ok=True)
    api = Api(out)
    api._host = urlparse(url).hostname or ""
    w = webview.create_window("Source — đăng nhập, mở phần tài liệu rồi bấm 📥 Tải tài liệu mới", url,
                              js_api=api, width=1200, height=860)
    api._window = w
    try:
        saved = json.loads(clicks or "[]")
    except ValueError:
        saved = []
    js = TOOLBAR_JS.replace("__TARGET__", json.dumps(url)).replace("__CLICKS__", json.dumps(saved))
    trace(f"start {url} clicks={saved}")

    def loaded():
        try:
            w.evaluate_js(js)
            trace("injected on " + str(w.get_current_url()))
        except Exception as e:  # noqa: BLE001
            trace(f"inject failed: {e}")
        try:                                # keep the login after every page (also if the user just closes)
            n = keep_session_cookies(w, read_cookies(w), urlparse(w.get_current_url() or url).hostname or "")
            trace(f"kept {n} session cookies")
        except Exception as e:  # noqa: BLE001
            trace(f"cookies: {e}")
    w.events.loaded += loaded
    try:
        webview.start(private_mode=False, storage_path=profile)
    except Exception as e:  # noqa: BLE001
        if not os.path.exists(out):
            with open(out, "w", encoding="utf-8") as f:
                json.dump({"error": f"không mở được cửa sổ trình duyệt: {e}"}, f, ensure_ascii=False)


if __name__ == "__main__":
    run(*sys.argv[1:5])
