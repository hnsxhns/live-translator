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
  bar.style.cssText = 'position:fixed;right:12px;top:50%;transform:translateY(-50%);z-index:2147483647;background:#1f2937;color:#fff;' +
    'font:13px Segoe UI,Arial,sans-serif;padding:10px 12px;border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.35);' +
    'max-width:400px;line-height:1.4';
  bar.innerHTML = '<div id="__ltHint" style="margin-bottom:8px">Live Translator: đăng nhập, bấm tab ' +
    '<b>Materials</b>, rồi bấm nút dưới. Lần sau app tự làm.</div>' +
    '<button id="__ltScan" style="background:#16a34a;color:#fff;border:0;border-radius:6px;padding:7px 12px;' +
    'font:600 13px Segoe UI,Arial,sans-serif;cursor:pointer">📥 Tải tài liệu mới từ trang này</button>' +
    '<div id="__ltMsg" style="margin-top:6px;color:#d1fae5"></div>';
  document.body.appendChild(bar);
  var msg = document.getElementById('__ltMsg');
  // the bar never has to cover the page: "—" folds it to a small tab, and it can be dragged anywhere
  (function () {
    var fold = document.createElement('span');
    fold.textContent = '—';
    fold.title = 'Thu nhỏ / mở lại';
    fold.style.cssText = 'float:right;margin:-4px -4px 0 8px;padding:0 6px;cursor:pointer;font-weight:700;color:#cbd5e1';
    bar.insertBefore(fold, bar.firstChild);
    fold.onclick = function (ev) {
      ev.stopPropagation();
      var small = bar.getAttribute('data-small') !== '1';
      bar.setAttribute('data-small', small ? '1' : '0');
      for (var i = 0; i < bar.children.length; i++) if (bar.children[i] !== fold) bar.children[i].style.display = small ? 'none' : '';
      fold.textContent = small ? 'LT ▸' : '—';
      bar.style.padding = small ? '4px 6px' : '10px 12px';
    };
    var drag = null;
    bar.style.cursor = 'move';
    bar.addEventListener('mousedown', function (ev) {
      if (ev.target !== bar && ev.target.tagName !== 'DIV') return;      // buttons and links still click
      var r = bar.getBoundingClientRect();
      drag = {dx: ev.clientX - r.left, dy: ev.clientY - r.top};
      ev.preventDefault();
    });
    document.addEventListener('mousemove', function (ev) {
      if (!drag) return;
      bar.style.transform = 'none'; bar.style.right = 'auto';
      bar.style.left = Math.max(0, Math.min(innerWidth - 60, ev.clientX - drag.dx)) + 'px';
      bar.style.top = Math.max(0, Math.min(innerHeight - 30, ev.clientY - drag.dy)) + 'px';
    });
    document.addEventListener('mouseup', function () { drag = null; });
  })();

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
  // Salesforce file ids (ContentDocument 069..., ContentVersion 068...) found in an element's attributes
  var SFID = /\b(069|068)[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?\b/;
  var FILE = /\.(pdf|docx?|pptx?|xlsx?|txt|md|csv|png|jpe?g|zip)\b/i;
  var sfPrefix = (location.pathname.match(/^(.*?)\/s\//) || [null, ''])[1];   // e.g. "/community"
  // what the page shows now: real links and Salesforce file ids, only from visible elements (hidden tabs
  // stay out), never the site's header/footer/menus unless inside the open tab's panel
  function items(sc) {
    var out = [], seen = {}, roots = sc && sc.panels.length ? sc.panels : [document];
    for (var r = 0; r < roots.length; r++) {
      var els = [roots[r]].concat(all(roots[r], []));
      for (var i = 0; i < els.length; i++) {
        var e = els[i];
        if (!e.getAttribute || mine(e)) continue;
        var raw = e.tagName === 'A' ? (e.getAttribute('href') || '') : '';
        if (raw && raw !== '#' && !/^javascript:/i.test(raw) && /^https?:/i.test(e.href)) {   // a real link
          if (seen[e.href] !== undefined || !visible(e) || (!(sc && sc.panels.length) && inChrome(e))) continue;
          seen[e.href] = out.length;
          out.push({key: e.href, href: e.href, text: label(e), download: e.getAttribute('download') || ''});
        }
        var attrs = e.attributes || [], took = false;
        // a download address kept in an attribute (Salesforce buttons: data-url-value=".../download/?ids=068...")
        for (var j = 0; j < attrs.length && !took; j++) {
          var v = attrs[j].value || '';
          if (attrs[j].name === 'href' || !/^https?:\/\//i.test(v) || !(/download|servlet|\/sfc\//i.test(v) || FILE.test(v))) continue;
          took = true;
          if (!visible(e)) break;
          var t = label(e).slice(0, 120);
          if (seen[v] !== undefined) {               // same file seen on its icon: the link with the name wins
            if (t && (e.tagName === 'A' || !out[seen[v]].text)) out[seen[v]].text = t;
            break;
          }
          seen[v] = out.length;
          out.push({key: v, href: v, text: t, download: ''});
        }
        if (took) continue;
        for (var j2 = 0; j2 < attrs.length; j2++) {
          if (attrs[j2].name === 'href' && e.tagName === 'A' && seen[e.href] !== undefined) continue;  // already a link
          var m = SFID.exec(attrs[j2].value || '');
          if (!m) continue;
          var id = m[0], k = 'sf:' + id.slice(0, 15);
          if (seen[k] !== undefined || !visible(e)) break;
          seen[k] = out.length;
          out.push({key: k, href: location.origin + sfPrefix + '/s/' + (id.slice(0, 3) === '068' ? 'contentversion/' :
                    'contentdocument/') + id, text: label(e).slice(0, 120), download: '', sf: true});
          break;
        }
      }
    }
    return out;
  }
  function keys(list) { var s = {}; for (var i = 0; i < list.length; i++) s[list[i].key] = 1; return s; }
  // file-like things on screen (for the diagnostic file: what the page really contains)
  function outline() {
    var out = [], els = all(document, []);
    for (var i = 0; i < els.length && out.length < 150; i++) {
      var e = els[i];
      if (!e.getAttribute || mine(e) || !visible(e)) continue;
      var t = (e.childElementCount ? '' : (e.textContent || '').trim());
      var a = [];
      for (var j = 0; j < (e.attributes || []).length; j++) {
        var at = e.attributes[j];
        if (/^(href|title|data-|aria-label|role|class|id)/.test(at.name)) a.push(at.name + '=' + (at.value || '').slice(0, 90));
      }
      var sa = a.join(' ');
      if (FILE.test(t) || SFID.test(sa) || /download|file|attachment/i.test(sa))
        out.push(e.tagName + ' [' + sa.slice(0, 300) + '] ' + t.slice(0, 120));
    }
    return out;
  }

  // remember the tabs/buttons the user clicks before pressing the button (e.g. "Materials"), to replay;
  // just before such a click, note what is on screen: what appears after it is that tab's content
  var clicks = [], before = null;
  function tabLike(e) {
    var role = (e.getAttribute('role') || '').toLowerCase();
    if (role === 'tab' || e.tagName === 'BUTTON' || role === 'button' || role === 'menuitem') return true;
    if (e.tagName !== 'A') return false;              // a link that stays on the page (a tab made of a link)
    var h = e.getAttribute('href') || '';
    return !h || h === '#' || /^javascript:/i.test(h) || (e.href && e.href.split('#')[0] === location.href.split('#')[0]);
  }
  document.addEventListener('click', function (ev) {
    var path = ev.composedPath ? ev.composedPath() : [ev.target];
    for (var i = 0; i < path.length; i++) {
      var e = path[i];
      if (!e || !e.getAttribute) continue;
      if (mine(e)) return;
      if (tabLike(e)) {
        var t = label(e);
        var last = clicks[clicks.length - 1];
        if (last && last.text === t && last.tag === e.tagName) return;   // the same tab again: keep the first
        if (t && t.length <= 60) {
          clicks.push({text: t, role: (e.getAttribute('role') || '').toLowerCase(), tag: e.tagName});
          if (clicks.length > 5) clicks.shift();
          before = keys(items(null));
        }
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
    var sc = scope(), l = items(sc), kind = sc.panels.length ? 'panel' : 'page';
    try { window.pywebview.api.log('scan: ' + l.length + ' items, panels ' + sc.panels.length + ', before ' + (before ? Object.keys(before).length : 'none')); } catch (x) {}
    if (!sc.panels.length && before) {               // no standard tab panel: what the last tab click added
      var added = l.filter(function (x) { return !before[x.key]; });
      if (added.length) { l = added; kind = 'diff'; }
    }
    sent = true;
    msg.textContent = 'Đang gửi ' + l.length + ' mục' + (sc.tab ? ' trong tab “' + sc.tab + '”' : '') + '...';
    window.pywebview.api.scan({url: location.href, title: document.title, links: l, ua: navigator.userAgent,
                               clicks: clicks.slice(-3), scope: kind, tab: sc.tab ||
                               (clicks.length ? clicks[clicks.length - 1].text : ''), tabs: sc.tabs,
                               panels: sc.panels.length, outline: outline()});
  }
  document.getElementById('__ltScan').onclick = scan;

  // ---- automatic: on the saved page, open the Materials tab (saved clicks), then scan. Never on a login
  // page, and never when the tab is not found: then the user is asked to do it. ----
  var target = __TARGET__, saved = __CLICKS__;
  function log(m) { try { window.pywebview.api.log(m); } catch (x) {} }
  log('page ' + location.href + ' | target ' + target + ' | login ' + isLogin() + ' | saved ' + JSON.stringify(saved));
  if (isLogin()) {
    msg.textContent = 'Hãy đăng nhập — sau đó app tự mở tab Materials và tải tài liệu.';
    return;
  }
  function same(a, b) {
    try {
      var x = new URL(a), y = new URL(b), ok = true;
      y.searchParams.forEach(function (v, k) { if (x.searchParams.get(k) !== v) ok = false; });
      return ok && x.origin === y.origin && x.pathname.replace(/\/+$/, '') === y.pathname.replace(/\/+$/, '');
    } catch (e) { return false; }
  }
  if (!target || !same(location.href, target)) { log('not the saved page: no automatic steps'); return; }
  var cancelled = false;
  var stop = document.createElement('a');
  stop.textContent = 'Huỷ tự động';
  stop.href = 'javascript:void 0';
  stop.style.cssText = 'margin-left:8px;color:#fca5a5';
  stop.onclick = function () { cancelled = true; msg.textContent = 'Đã huỷ — bấm nút khi sẵn sàng.'; stop.remove(); };
  bar.appendChild(stop);
  var MAT = /^(course\s+)?materials?$|^tài liệu$|^resources$/i;
  function clickable(e) {
    var role = (e.getAttribute('role') || '').toLowerCase();
    return e.tagName === 'BUTTON' || e.tagName === 'A' || role === 'tab' || role === 'button' || role === 'menuitem';
  }
  // the element to click for a saved tab: same kind of element (tag/role) and same text; a wrapper with that
  // text (e.g. a DIV around the button) is not it - its clickable inside/outside part is used instead
  function find(c) {
    var want = c.text.toLowerCase(), els = all(document, []), loose = null;
    for (var i = 0; i < els.length; i++) {
      var e = els[i];
      if (mine(e) || !e.getAttribute) continue;
      var role = (e.getAttribute('role') || '').toLowerCase();
      var text = label(e);
      var match = c.text === '__auto__' ? MAT.test(text) : text.toLowerCase() === want;
      if (!match || !visible(e)) continue;
      if (c.text === '__auto__' ? clickable(e) || e.tagName === 'LI'
                                : (c.role ? role === c.role : e.tagName === c.tag)) return e;
      if (!loose) loose = e;
    }
    if (!loose) return null;
    var inner = [loose].concat(all(loose, [])).filter(function (x) { return x.getAttribute && clickable(x) && visible(x); });
    if (inner.length) return inner[0];
    for (var p = parentOf(loose); p; p = parentOf(p)) if (clickable(p)) return p;
    return loose;
  }
  if (!saved || !saved.length) saved = [{text: '__auto__'}];   // first time: a tab called Materials
  // last time the files were on the page itself: still open a tab called Materials if the page has one
  var noTab = saved.length === 1 && saved[0].text === '__page__';
  if (noTab) saved = [{text: '__auto__'}];
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
    if (step >= saved.length) {
      if (!opened && !noTab) {                               // not signed in yet, or the page changed: let the user do it
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
        log((e ? 'clicked ' : 'not found ') + JSON.stringify(c) + (e ? ' (' + e.tagName + ' "' + label(e) + '")' : ''));
        if (e) { opened++; before = keys(items(null)); try { e.click(); } catch (x) {} }   // note the screen first
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


def keep_all_session_cookies(window, timeout=5.0):
    """Every session cookie in the window's profile (the course site AND the sign-in services it used, e.g. a
    university login) re-saved as a 30-day cookie, so the next window opens signed in. Windows and Mac.
    Returns how many were kept, or None when this way is not available."""
    if sys.platform == "darwin":
        return mac_keep_all_session_cookies(window, timeout)
    if sys.platform != "win32":
        return None
    try:
        from System import Action, DateTime
        from System.Collections.Generic import List
        from System.Threading.Tasks import Task, TaskScheduler
        from Microsoft.Web.WebView2.Core import CoreWebView2Cookie
    except Exception as e:  # noqa: BLE001
        trace(f"all-cookies unavailable: {e}")
        return None
    form, done, n = window.native, threading.Event(), [0]

    def on_ui():
        mgr = form.browser.webview.CoreWebView2.CookieManager

        def got(task):
            try:
                for c in task.Result:
                    if c.IsSession:
                        c.Expires = DateTime.Now.AddDays(30)
                        mgr.AddOrUpdateCookie(c)
                        n[0] += 1
            except Exception as e:  # noqa: BLE001
                trace(f"all-cookies: {e}")
            finally:
                done.set()
        mgr.GetCookiesAsync("").ContinueWith(Action[Task[List[CoreWebView2Cookie]]](got),
                                             TaskScheduler.FromCurrentSynchronizationContext())
    try:
        form.Invoke(Action(on_ui))
    except Exception as e:  # noqa: BLE001
        trace(f"all-cookies invoke: {e}")
        return None
    done.wait(timeout)
    return n[0]


def read_all_cookies(window, timeout=5.0):
    """Every cookie in the window's profile, of every site (the course site, its file server, its sign-in
    service), so downloads that hop between them work like in the browser. None when not available."""
    if sys.platform == "darwin":
        return mac_read_all_cookies(window, timeout)
    if sys.platform != "win32":
        return None
    try:
        from System import Action
        from System.Collections.Generic import List
        from System.Threading.Tasks import Task, TaskScheduler
        from Microsoft.Web.WebView2.Core import CoreWebView2Cookie
    except Exception:  # noqa: BLE001
        return None
    form, done, out = window.native, threading.Event(), []

    def on_ui():
        mgr = form.browser.webview.CoreWebView2.CookieManager

        def got(task):
            try:
                for c in task.Result:
                    out.append({"name": c.Name, "value": c.Value, "domain": c.Domain, "path": c.Path or "/",
                                "secure": bool(c.IsSecure), "httponly": bool(c.IsHttpOnly),
                                "session": bool(c.IsSession)})
            except Exception as e:  # noqa: BLE001
                trace(f"read cookies: {e}")
            finally:
                done.set()
        mgr.GetCookiesAsync("").ContinueWith(Action[Task[List[CoreWebView2Cookie]]](got),
                                             TaskScheduler.FromCurrentSynchronizationContext())
    try:
        form.Invoke(Action(on_ui))
    except Exception:  # noqa: BLE001
        return None
    done.wait(timeout)
    return out


# ---- Mac (WebKit): the same two jobs through WKHTTPCookieStore (PyObjC) ----
def _mac_cookie_call(window, job, timeout):
    """Run job(store, done) on the main thread with the window's WKHTTPCookieStore; job calls done() when
    finished. Returns False when the store is not reachable (then the callers fall back)."""
    try:
        from PyObjCTools import AppHelper
        from webview.platforms.cocoa import BrowserView
    except Exception as e:  # noqa: BLE001
        trace(f"mac cookies unavailable: {e}")
        return False
    finished = threading.Event()

    def on_main():
        try:
            bv = BrowserView.instances.get(window.uid)
            store = bv.webview.configuration().websiteDataStore().httpCookieStore()
            job(store, finished.set)
        except Exception as e:  # noqa: BLE001
            trace(f"mac cookies: {e}")
            finished.set()
    AppHelper.callAfter(on_main)
    return finished.wait(timeout)


def mac_keep_all_session_cookies(window, timeout=5.0):
    """Session cookies of every site in the window's store re-saved with a 30-day expiry (Mac)."""
    try:
        from Foundation import NSDate, NSHTTPCookie, NSHTTPCookieDiscard, NSHTTPCookieExpires, NSMutableDictionary
    except Exception:  # noqa: BLE001
        return None
    n = [0]

    def job(store, done):
        def got(cookies):
            try:
                for c in cookies or []:
                    if not c.isSessionOnly():
                        continue
                    props = NSMutableDictionary.dictionaryWithDictionary_(c.properties())
                    props.removeObjectForKey_(NSHTTPCookieDiscard)
                    props[NSHTTPCookieExpires] = NSDate.dateWithTimeIntervalSinceNow_(30 * 86400)
                    nc = NSHTTPCookie.cookieWithProperties_(props)
                    if nc is not None:
                        store.setCookie_completionHandler_(nc, None)
                        n[0] += 1
            finally:
                done()
        store.getAllCookies_(got)
    if not _mac_cookie_call(window, job, timeout):
        return None
    trace(f"mac: kept {n[0]} session cookies for 30 days")
    return n[0]


def mac_read_all_cookies(window, timeout=5.0):
    """Every cookie of every site in the window's store (Mac), in read_all_cookies' format."""
    out = []

    def job(store, done):
        def got(cookies):
            try:
                for c in cookies or []:
                    out.append({"name": str(c.name()), "value": str(c.value()), "domain": str(c.domain() or ""),
                                "path": str(c.path() or "/"), "secure": bool(c.isSecure()),
                                "httponly": bool(c.isHTTPOnly()), "session": bool(c.isSessionOnly())})
            finally:
                done()
        store.getAllCookies_(got)
    if not _mac_cookie_call(window, job, timeout):
        return None
    return out


class Api:
    def __init__(self, out):
        self.out = out
        self._window = None                 # private: pywebview walks public attributes (huge, slow)
        self._host = ""

    def log(self, msg):
        """Notes from the page's automatic steps -> source_autolog.txt beside the result file (kept small)."""
        trace("js: " + str(msg))
        try:
            path = os.path.join(os.path.dirname(self.out), "source_autolog.txt")
            old = open(path, encoding="utf-8").read()[-20000:] if os.path.exists(path) else ""
            with open(path, "w", encoding="utf-8") as f:
                f.write(old + time.strftime("%H:%M:%S ") + str(msg)[:2000] + "\n")
        except Exception:  # noqa: BLE001
            pass
        return True

    def scan(self, page):
        """Called from the page (button or automatic): save links + login cookies, then close the window."""
        cookies = []
        try:
            if keep_all_session_cookies(self._window) is None:     # all sites' sign-in, else this site's
                keep_session_cookies(self._window, read_cookies(self._window), self._host)
            cookies = read_all_cookies(self._window)                 # every site: downloads may hop between them
            if cookies is None:
                cookies = read_cookies(self._window)
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
    # near the top of the screen and no taller than it, so the bar at the bottom is always reachable
    sw, sh = 1366, 768
    try:
        s0 = webview.screens[0]
        sw, sh = int(s0.width), int(s0.height)
    except Exception:  # noqa: BLE001
        pass
    width, height = min(1200, sw - 40), min(860, sh - 90)   # leave room for the taskbar
    w = webview.create_window("Source — đăng nhập, mở phần tài liệu rồi bấm 📥 Tải tài liệu mới", url,
                              js_api=api, width=width, height=height, x=max(0, (sw - width) // 2), y=8)
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
            n = keep_all_session_cookies(w)
            if n is None:
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
