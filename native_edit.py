"""A native Windows text box (Win32 EDIT control) inside a Tkinter frame.

Tk's own Text widget handles Vietnamese input methods badly on Windows (the built-in Telex/VNI
keyboards use TSF, and Unikey/EVKey often lose or double letters). The native EDIT control works
with every input method, like typing in Notepad.

The class offers the few Text-like methods the app uses: get / insert / delete / focus_set.
Enter calls `on_enter`; Shift+Enter makes a new line; Ctrl+A selects all.
"""
import ctypes
import sys
from ctypes import wintypes

if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = wintypes.LPARAM
    SUBCLASSPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                      wintypes.LPARAM, ctypes.c_size_t, ctypes.c_size_t)
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = LRESULT
    user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    user32.GetKeyState.argtypes = [ctypes.c_int]
    user32.GetKeyState.restype = ctypes.c_short
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
    gdi32.CreateFontW.restype = wintypes.HFONT
    comctl32.SetWindowSubclass.argtypes = [wintypes.HWND, SUBCLASSPROC, ctypes.c_size_t, ctypes.c_size_t]
    comctl32.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    comctl32.DefSubclassProc.restype = LRESULT
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

WS_CHILD, WS_VISIBLE, WS_VSCROLL, WS_TABSTOP = 0x40000000, 0x10000000, 0x00200000, 0x00010000
ES_MULTILINE, ES_AUTOVSCROLL, ES_WANTRETURN = 0x0004, 0x0040, 0x1000
WM_SETFONT, WM_KEYDOWN, WM_CHAR = 0x0030, 0x0100, 0x0102
EM_SETSEL, EM_SETMARGINS, EM_REPLACESEL, EM_SCROLLCARET = 0x00B1, 0x00D3, 0x00C2, 0x00B7
EC_LEFTMARGIN, EC_RIGHTMARGIN = 0x1, 0x2
VK_RETURN, VK_SHIFT, VK_CONTROL = 0x0D, 0x10, 0x11


class NativeEdit:
    def __init__(self, host, on_enter, font_family="Segoe UI", font_points=12):
        """`host` is an empty tk.Frame that the native box fills."""
        if sys.platform != "win32":
            raise OSError("native edit box is Windows-only")
        self.host = host
        self.on_enter = on_enter
        host.update_idletasks()
        self.hwnd = user32.CreateWindowExW(
            0, "EDIT", "", WS_CHILD | WS_VISIBLE | WS_VSCROLL | WS_TABSTOP | ES_MULTILINE | ES_AUTOVSCROLL
            | ES_WANTRETURN, 0, 0, max(10, host.winfo_width()), max(10, host.winfo_height()),
            host.winfo_id(), None, kernel32.GetModuleHandleW(None), None)
        if not self.hwnd:
            raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
        px = int(round(font_points * host.winfo_fpixels("1i") / 72))
        self.font = gdi32.CreateFontW(-px, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, font_family)   # 5 = ClearType
        user32.SendMessageW(self.hwnd, WM_SETFONT, self.font, 1)
        user32.SendMessageW(self.hwnd, EM_SETMARGINS, EC_LEFTMARGIN | EC_RIGHTMARGIN, (8 << 16) | 8)
        self._enter = False
        self._proc = SUBCLASSPROC(self._wndproc)        # keep a reference: Windows calls it later
        comctl32.SetWindowSubclass(self.hwnd, self._proc, 1, 0)
        host.bind("<Configure>", self._resize)
        host.bind("<Button-1>", lambda e: self.focus_set())

    def _resize(self, event):
        if user32.IsWindow(self.hwnd):
            user32.MoveWindow(self.hwnd, 0, 0, event.width, event.height, True)

    def _wndproc(self, hwnd, msg, wparam, lparam, uid, data):
        shift = user32.GetKeyState(VK_SHIFT) & 0x8000
        ctrl = user32.GetKeyState(VK_CONTROL) & 0x8000
        if msg == WM_KEYDOWN and wparam == VK_RETURN and not shift:
            # Enter = send (Shift+Enter = new line). Windows calls us from inside Tk's event loop,
            # where calling back into Tk would crash, so just raise a flag; the app polls it.
            self._enter = True
            return 0
        if msg == WM_CHAR and wparam in (13, 10) and not shift:
            return 0                                       # swallow the Enter character itself
        if msg == WM_KEYDOWN and ctrl and wparam == ord("A"):
            user32.SendMessageW(hwnd, EM_SETSEL, 0, -1)    # Ctrl+A = select all
            return 0
        if msg == WM_CHAR and wparam == 1:                 # the Ctrl+A character: no beep
            return 0
        return comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)

    def take_enter(self):
        """True once after Enter was pressed (polled by the app)."""
        hit, self._enter = self._enter, False
        return hit

    # --- the Text-like methods the app uses ---
    def get(self, *_):
        n = user32.GetWindowTextLengthW(self.hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(self.hwnd, buf, n + 1)
        return buf.value.replace("\r\n", "\n")

    def delete(self, *_):
        user32.SetWindowTextW(self.hwnd, "")

    def insert(self, _index, text):
        user32.SendMessageW(self.hwnd, EM_SETSEL, -1, -1)      # caret to the end
        buf = ctypes.create_unicode_buffer(text.replace("\n", "\r\n"))
        user32.SendMessageW(self.hwnd, EM_REPLACESEL, 1, ctypes.cast(buf, ctypes.c_void_p).value)
        user32.SendMessageW(self.hwnd, EM_SCROLLCARET, 0, 0)

    def focus_set(self):
        user32.SetFocus(self.hwnd)
