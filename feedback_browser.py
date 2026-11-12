"""Survey window for Learning > Feedback (runs as its own process, like the Source window, and shares its login):
reads the questions on the survey page, hands them to the app, and fills in the answers the user approved.
The user always presses the survey's own Next / Submit button.

`LiveTranslator.exe --feedback-browser <survey url> <exchange folder> <profile folder>`
Exchange folder: q_<n>.json (questions of page n, written here) -> a_<n>.json (answers, written by the app).
"""
import json
import os
import sys
import threading
import time

FEEDBACK_JS = r"""
(function () {
  if (window.__ltFb || !document.body) return;
  window.__ltFb = true;
  var bar = document.createElement('div');
  bar.style.cssText = 'position:fixed;right:12px;top:50%;transform:translateY(-50%);z-index:2147483647;background:#1f2937;color:#fff;' +
    'font:13px Segoe UI,Arial,sans-serif;padding:10px 12px;border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,.35);' +
    'max-width:400px;line-height:1.4';
  bar.innerHTML = '<div style="margin-bottom:6px"><b>Live Translator — Feedback</b>: app đọc câu hỏi, Claude soạn ' +
    'câu trả lời, bạn duyệt/sửa trong app rồi app điền vào đây. Bạn tự bấm Next/Submit của trang.</div>' +
    '<button id="__ltRead" style="background:#2563eb;color:#fff;border:0;border-radius:6px;padding:6px 10px;' +
    'font:600 12px Segoe UI,Arial,sans-serif;cursor:pointer">📝 Đọc câu hỏi trang này</button>' +
    '<div id="__ltFbMsg" style="margin-top:6px;color:#d1fae5"></div>';
  document.body.appendChild(bar);
  var msg = document.getElementById('__ltFbMsg');
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
  function mine(e) { return e === bar || bar.contains(e); }
  function all(root, out) {
    var els = root.querySelectorAll('*');
    for (var i = 0; i < els.length; i++) {
      var e = els[i];
      out.push(e);
      if (e.shadowRoot) all(e.shadowRoot, out);
      if (e.tagName === 'IFRAME') { try { if (e.contentDocument) all(e.contentDocument, out); } catch (x) {} }
    }
    return out;
  }
  function visible(e) {
    if (!e || !e.getClientRects || !e.getClientRects().length) return false;
    try { var s = getComputedStyle(e); return s.visibility !== 'hidden' && s.display !== 'none'; } catch (x) { return true; }
  }
  function txt(e) { return ((e && (e.innerText || e.textContent)) || '').replace(/\s+/g, ' ').trim(); }
  function parentOf(e) {
    var p = e.parentNode;
    if (p && p.host && p.nodeType === 11) return p.host;
    return p && p.nodeType === 1 ? p : null;
  }
  function labelFor(e) {                         // the visible label of a control
    var root = e.getRootNode ? e.getRootNode() : document;
    if (e.id) {
      try {
        var l = (root.querySelector ? root : document).querySelector('label[for="' + CSS.escape(e.id) + '"]');
        if (l) return l;
      } catch (x) {}
    }
    for (var p = e; p; p = parentOf(p)) if (p.tagName === 'LABEL') return p;
    return null;
  }
  // what an option says on screen ("5 - Excellent"), not its internal value: its label, its description,
  // the header of its table column (rating grids), or the short text right next to it
  function optionText(e) {
    var l = labelFor(e), t = l && txt(l);
    if (t) return t;
    t = e.getAttribute('aria-label') || e.getAttribute('title');
    if (t) return t.trim();
    var root = e.getRootNode ? e.getRootNode() : document, ids = (e.getAttribute('aria-labelledby') || '').split(/\s+/);
    t = ids.map(function (id) { var n = id && root.getElementById ? root.getElementById(id) : null; return n ? txt(n) : ''; })
           .filter(Boolean).join(' ');
    if (t) return t;
    var cell = null;
    for (var p = e; p; p = parentOf(p)) if (p.tagName === 'TD' || p.tagName === 'TH') { cell = p; break; }
    if (cell) {
      var table = cell; while (table && table.tagName !== 'TABLE') table = parentOf(table);
      var head = table && (table.querySelector('thead tr') || table.querySelector('tr'));
      if (head && head !== cell.parentNode && head.children[cell.cellIndex]) {
        t = txt(head.children[cell.cellIndex]);
        if (t) return t;
      }
    }
    for (var q = parentOf(e), k = 0; q && k < 3; q = parentOf(q), k++) {
      var s = txt(q);
      if (s && s.length <= 60) return s;
    }
    return e.value || '';
  }
  function isControl(e) {
    if (mine(e)) return false;
    if (e.tagName === 'TEXTAREA' || e.tagName === 'SELECT') return true;
    if (e.tagName !== 'INPUT') return false;
    var t = (e.type || 'text').toLowerCase();
    return /^(radio|checkbox|text|email|number|range|tel|url)$/.test(t);
  }
  function shown(e) { return visible(e) || visible(labelFor(e)) || visible(parentOf(e)); }
  // the questions on this page: radio/checkbox groups, text fields, lists; each gets data-lt-q to be filled later
  function read() {
    var els = all(document, []).filter(function (e) { return isControl(e) && !e.disabled && shown(e); });
    var groups = [], byName = {};
    els.forEach(function (e) {
      var t = (e.type || '').toLowerCase();
      if ((t === 'radio' || t === 'checkbox') && e.name) {
        var k = t + ':' + e.name;
        if (byName[k] === undefined) { byName[k] = groups.length; groups.push({type: t, els: []}); }
        groups[byName[k]].els.push(e);
      } else {
        groups.push({type: e.tagName === 'TEXTAREA' ? 'textarea' : e.tagName === 'SELECT' ? 'select' :
                     (t === 'range' ? 'range' : (t === 'radio' || t === 'checkbox') ? t : 'text'), els: [e]});
      }
    });
    var inGroup = new Map();
    groups.forEach(function (g, i) { g.els.forEach(function (e) { inGroup.set(e, i); }); });
    var out = [];
    groups.forEach(function (g, i) {
      // the question: the largest box around this group that holds no other question's fields
      var box = g.els[0], up = parentOf(box);
      while (up) {
        var other = false, inside = all(up, []).filter(isControl);
        for (var j = 0; j < inside.length; j++) { var gi = inGroup.get(inside[j]); if (gi !== undefined && gi !== i) { other = true; break; } }
        if (other) break;
        box = up; up = parentOf(up);
      }
      var options = [];
      if (g.type === 'radio' || g.type === 'checkbox') options = g.els.map(optionText);
      if (g.type === 'select') options = Array.prototype.map.call(g.els[0].options, function (o) { return txt(o); })
                                               .filter(function (o) { return o && !/^(-+|select|chọn)/i.test(o); });
      // the question's own words: the text in the box outside the answer options (labels, lists, fields)
      // for a choice the labels are its options; for a text field its label IS the question (Qualtrics)
      var choice = g.type === 'radio' || g.type === 'checkbox';
      var skip = choice ? g.els.map(function (e) { return labelFor(e); }).filter(Boolean).concat(g.els) : g.els.slice();
      var parts = [];
      (function walk(n) {
        if (n.nodeType === 3) { parts.push(n.nodeValue); return; }
        if (n.nodeType !== 1 || /^(SCRIPT|STYLE|SELECT|OPTION|TEXTAREA)$/.test(n.tagName)) return;
        if (skip.indexOf(n) >= 0 || mine(n)) return;
        var kids = n.shadowRoot ? n.shadowRoot.childNodes : n.childNodes;
        for (var k = 0; k < kids.length; k++) walk(kids[k]);
      })(box);
      var question = parts.join(' ').replace(/\s+/g, ' ').trim();
      var e0 = g.els[0], named = [];                 // texts the page names as the field's label
      (e0.getAttribute('aria-labelledby') || '').split(/\s+/).forEach(function (id) {
        var r = e0.getRootNode ? e0.getRootNode() : document, n = id && r.getElementById ? r.getElementById(id) : null;
        if (n && !choice) named.push(txt(n));
      });
      if (!choice && labelFor(e0)) named.push(txt(labelFor(e0)));
      named.forEach(function (t) { if (t && question.indexOf(t) < 0) question = t + ' ' + question; });
      question = question.replace(/^(.{12,}?)\s+\1(?=\s|$)/, '$1').trim();   // "Session 1: X Session 1: X" -> once
      if (!question || question.length < 3) question = g.els[0].getAttribute('aria-label') || g.els[0].placeholder ||
                                                       g.els[0].name || '(câu hỏi)';
      var qid = 'q' + (i + 1);
      g.els.forEach(function (e) { e.setAttribute('data-lt-q', qid); });
      out.push({qid: qid, type: g.type, question: question.slice(0, 600), options: options.slice(0, 40)});
    });
    return out;
  }
  function setValue(e, v) {                       // so that the page's own scripts see the change
    var proto = e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : e.tagName === 'SELECT' ?
                HTMLSelectElement.prototype : HTMLInputElement.prototype;
    var d = Object.getOwnPropertyDescriptor(proto, 'value');
    if (d && d.set) d.set.call(e, v); else e.value = v;
    e.dispatchEvent(new Event('input', {bubbles: true}));
    e.dispatchEvent(new Event('change', {bubbles: true}));
  }
  function norm(s) { return (s || '').toLowerCase().replace(/\s+/g, ' ').trim(); }
  window.__ltFill = function (answers) {
    if (!all(document, []).some(function (e) { return e.getAttribute && e.getAttribute('data-lt-q'); })) read();
    var els = all(document, []), n = 0;
    (answers || []).forEach(function (a) {
      var mine2 = els.filter(function (e) { return e.getAttribute && e.getAttribute('data-lt-q') === a.qid; });
      if (!mine2.length) return;
      var e0 = mine2[0], t = (e0.type || '').toLowerCase();
      if (t === 'radio' || t === 'checkbox') {
        var want = t === 'radio' ? [a.choice] : (a.choices || []);
        want.forEach(function (w) {
          if (!w) return;
          var pick = mine2.filter(function (e) { return norm(optionText(e)) === norm(w); })[0] ||
                     mine2.filter(function (e) { return norm(optionText(e)).indexOf(norm(w)) >= 0; })[0];
          if (pick && !pick.checked) { pick.click(); if (!pick.checked) { var l = labelFor(pick); if (l) l.click(); } n++; }
          if (pick) (labelFor(pick) || pick).style.outline = '2px solid #16a34a';
        });
      } else if (e0.tagName === 'SELECT') {
        var o = Array.prototype.filter.call(e0.options, function (o) { return norm(txt(o)) === norm(a.choice); })[0];
        if (o) { setValue(e0, o.value); e0.style.outline = '2px solid #16a34a'; n++; }
      } else if (a.text) {
        setValue(e0, a.text); e0.style.outline = '2px solid #16a34a'; n++;
      }
    });
    msg.textContent = '✓ Đã điền ' + n + ' câu (viền xanh). Kiểm tra, rồi bấm Next/Submit của trang.';
    return n;
  };
  var last = '';
  function send(force) {
    var q = read();
    var sig = q.map(function (x) { return x.question; }).join('|');
    if (!q.length) { if (force) msg.textContent = 'Không thấy câu hỏi nào trên trang này.'; return; }
    if (sig === last && !force) return;
    if (!(window.pywebview && window.pywebview.api && window.pywebview.api.questions)) return;   // not ready yet
    last = sig;                                     // (set only once it really goes: a new page always gets sent)
    msg.textContent = 'Đã đọc ' + q.length + ' câu hỏi — Claude đang soạn, bạn duyệt trong app...';
    window.pywebview.api.questions({url: location.href, title: document.title, questions: q});
  }
  document.getElementById('__ltRead').onclick = function () { send(true); };
  // automatic: read once the page stops changing, and again whenever a new page of questions appears
  var lastCount = -1, still = 0;
  setInterval(function () {
    var n = all(document, []).length;
    still = (n === lastCount) ? still + 1 : 0; lastCount = n;
    if (still >= 3) send(false);                   // keeps trying until the page is sent
  }, 700);
})();
"""


FILLED_JS = r"""
(function () {
  var out = {}, els = document.querySelectorAll('[data-lt-q]');
  for (var i = 0; i < els.length; i++) {
    var e = els[i], q = e.getAttribute('data-lt-q'), t = (e.type || '').toLowerCase();
    if (t === 'radio' || t === 'checkbox') { if (e.checked) (out[q] = out[q] || []).push(e.value); }
    else if (e.tagName === 'SELECT') out[q] = e.options[e.selectedIndex] ? e.options[e.selectedIndex].text : '';
    else out[q] = e.value;
  }
  return out;
})()
"""
TEST_NEXT_JS = r"""
(function () {
  var b = Array.prototype.filter.call(document.querySelectorAll('button,input[type=submit]'), function (x) {
    return /next|submit|tiếp|gửi/i.test(x.innerText || x.value || '') && x.id !== '__ltRead'; })[0];
  if (b) setTimeout(function () { b.click(); }, 300);
})()
"""


def trace(msg):
    path = os.environ.get("LT_SOURCE_TRACE")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("fb: " + str(msg) + "\n")


class FeedbackApi:
    def __init__(self, io_dir):
        self.io = io_dir
        self._window = None
        self._n = 0

    def log(self, msg):
        trace(msg)
        return True

    def questions(self, page):
        """A page of questions: hand it to the app, then fill in the approved answers when they come back."""
        self._n += 1
        n = self._n
        tmp = os.path.join(self.io, f"q_{n}.json.part")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(page, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(self.io, f"q_{n}.json"))
        trace(f"questions {n}: {len(page.get('questions') or [])}")

        def wait():
            path = os.path.join(self.io, f"a_{n}.json")
            end = time.time() + 3600
            while time.time() < end:
                if os.path.exists(path):
                    try:
                        answers = json.load(open(path, encoding="utf-8"))
                    except ValueError:
                        time.sleep(0.3)
                        continue
                    try:
                        self._window.evaluate_js(f"window.__ltFill({json.dumps(answers, ensure_ascii=False)})")
                        trace(f"filled {n}")
                        # what the page now holds (checked options, typed text): a record for checking
                        state = self._window.evaluate_js(FILLED_JS)
                        with open(os.path.join(self.io, f"filled_{n}.json"), "w", encoding="utf-8") as f:
                            json.dump(state, f, ensure_ascii=False)
                        if os.environ.get("LT_FB_TEST_NEXT"):      # tests only: press the page's Next/Submit
                            self._window.evaluate_js(TEST_NEXT_JS)
                    except Exception as e:  # noqa: BLE001
                        trace(f"fill failed: {e}")
                    return
                time.sleep(0.5)
        threading.Thread(target=wait, daemon=True).start()
        return n


def run(url, io_dir, profile):
    import webview
    os.makedirs(io_dir, exist_ok=True)
    os.makedirs(profile, exist_ok=True)
    api = FeedbackApi(io_dir)
    sw, sh = 1366, 768
    try:
        s0 = webview.screens[0]
        sw, sh = int(s0.width), int(s0.height)
    except Exception:  # noqa: BLE001
        pass
    width, height = min(1150, sw - 40), min(860, sh - 90)
    w = webview.create_window("Feedback — duyệt câu trả lời trong app, rồi bấm Next/Submit của trang", url,
                              js_api=api, width=width, height=height, x=max(0, (sw - width) // 2), y=8)
    api._window = w

    def loaded():
        try:
            w.evaluate_js(FEEDBACK_JS)
            trace("injected on " + str(w.get_current_url()))
        except Exception as e:  # noqa: BLE001
            trace(f"inject failed: {e}")
    w.events.loaded += loaded
    webview.start(private_mode=False, storage_path=profile)


if __name__ == "__main__":
    run(*sys.argv[1:4])
