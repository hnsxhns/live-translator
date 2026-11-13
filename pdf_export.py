"""Key takeaways -> PDF (a font with Vietnamese and other letters), and opening files with the system's app."""
import os
import re
import subprocess
import sys
import time


def open_path(path):
    """Open a file or folder with the system's default app."""
    if sys.platform == "win32":
        os.startfile(path)                                  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])

def pdf_fonts():
    """(folder, regular file, bold file) of a font with Vietnamese letters."""
    for folder, reg, bold in ((r"C:\Windows\Fonts", "arial.ttf", "arialbd.ttf"),
                              (r"C:\Windows\Fonts", "segoeui.ttf", "segoeuib.ttf"),
                              ("/System/Library/Fonts/Supplemental", "Arial.ttf", "Arial Bold.ttf"),
                              ("/Library/Fonts", "Arial Unicode.ttf", "Arial Unicode.ttf")):
        if os.path.exists(os.path.join(folder, reg)):
            return folder, reg, bold if os.path.exists(os.path.join(folder, bold)) else reg
    return None


NO_EMOJI = re.compile("[\U00010000-\U0010FFFF☀-➿️‍]")    # the PDF font has no emoji


def _paras(text):
    """Plain text (light Markdown) -> HTML paragraphs."""
    import html as _html
    out = []
    for para in re.split(r"\n\s*\n", text.strip()):
        if not para.strip():
            continue
        lines = []
        for ln in para.split("\n"):
            ln = _html.escape(ln.rstrip())
            ln = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", ln)
            m = re.match(r"#{1,6}\s+(.*)", ln)
            if m:
                ln = f"<b>{m.group(1)}</b>"
            ln = re.sub(r"^(\s*)[-*]\s+", r"\1• ", ln)
            lines.append(ln)
        out.append("<p>" + "<br/>".join(lines) + "</p>")
    return "\n".join(out)


def write_pdf(out, body_html):
    """Write an A4 PDF from simple HTML (with a font that has Vietnamese letters)."""
    import pymupdf
    fonts = pdf_fonts()
    body_html = NO_EMOJI.sub("", body_html)
    css = (".prompt {background-color: #f4efe8; border: 1px solid #d9cfc4; padding: 6pt; font-size: 10pt;} "
           ".plabel {margin-bottom: 0;} "
           "body {font-size: 11pt; line-height: 1.35;} h1 {font-size: 17pt; color: #8a3f22;} "
           "h2 {font-size: 13.5pt; color: #8a3f22; margin-top: 14pt;} h3 {font-size: 12pt; margin-top: 10pt;} "
           ".meta {color: #666; font-size: 9.5pt;} .instr {color: #444; font-style: italic;} p {margin: 4pt 0;}")
    archive = None
    if fonts:
        folder, reg, bold = fonts
        archive = pymupdf.Archive(folder)
        css = (f"@font-face {{font-family: vn; src: url({reg});}} "
               f"@font-face {{font-family: vn; src: url({bold}); font-weight: bold;}} "
               "* {font-family: vn;} ") + css
    story = pymupdf.Story(html=f"<body>{body_html}</body>", user_css=css, archive=archive)
    writer = pymupdf.DocumentWriter(out)
    page = pymupdf.paper_rect("a4")
    where = page + (50, 50, -50, -50)
    more = True
    while more:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()


def takeaways_pdf(out, history, title="Ý chính / Key takeaways"):
    """The whole key-takeaways history (blocks headed "[HH:MM]") as a PDF."""
    import html as _html
    parts = [f"<h1>{_html.escape(title)}</h1>",
             f"<p class='meta'>Xuất ngày {time.strftime('%d/%m/%Y %H:%M')}</p>"]
    for block in re.split(r"\n\s*\n(?=\[\d\d:\d\d\]\n)", history.strip()):
        m = re.match(r"\[(\d\d:\d\d)\]\n", block)
        if m:
            parts.append(f"<h3>{m.group(1)}</h3>")
            block = block[m.end():]
        parts.append(_paras(block))
    write_pdf(out, "\n".join(parts))
    return out
