#!/usr/bin/env python3
"""meh 0.1.0 - a small text expander for Windows.

Type a trigger such as  :resc  and it is replaced by the text saved for it in
meh.yml.

Run it in the background, with no window:
    pythonw meh.py                     uses meh.yml next to this script
    pythonw meh.py D:\\path\\other.yml  uses another file

Run it again while it is running to get a "Quit meh?" prompt.
Saving meh.yml applies the changes within about a second.

Standard library only (PyYAML is used if it happens to be installed).
No network access, no log files, never writes to the clipboard.
"""

import datetime
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time

VERSION = "0.1.0"
MAGIC = 0x6D6568  # tags meh's own synthetic keystrokes so it never reads them back

SEPARATORS = set(" ,.?!)}]>'\"\r\t\n:;")  # characters that end a word
VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
CURSOR = "$|$"
VAR_TYPES = ("date", "clipboard", "echo", "random", "shell")


# --------------------------------------------------------------------------
# YAML: PyYAML when available, otherwise a reader for the subset meh.yml
# uses (mappings, lists, quoted/plain scalars, | and > blocks,
# [flow] lists, {flow} maps, comments).
# --------------------------------------------------------------------------

def parse_yaml(text):
    try:
        import yaml  # optional
    except ImportError:
        return _MiniYaml(text).parse()
    return yaml.safe_load(text)


class _MiniYaml:
    _ESC = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/",
            "0": "\0", "a": "\a", "b": "\b", "e": "\x1b", "f": "\f", "v": "\v",
            " ": " ", "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029"}

    def __init__(self, text):
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self.lines = text.split("\n")
        self.i = 0

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _ind(line):
        return len(line) - len(line.lstrip(" "))

    @staticmethod
    def _is_item(s):
        return s == "-" or s.startswith("- ")

    def _err(self, msg):
        raise ValueError("line %d: %s" % (min(self.i, len(self.lines)) + 1, msg))

    def _skip(self):
        while self.i < len(self.lines):
            line = self.lines[self.i]
            s = line.strip()
            if s and not s.startswith("#") and s not in ("---", "..."):
                if "\t" in line[:len(line) - len(line.lstrip())]:
                    self._err("tabs are not allowed for indentation; use spaces")
                return
            self.i += 1

    @staticmethod
    def _quote_end(s, start):
        q = s[start]
        j = start + 1
        while j < len(s):
            c = s[j]
            if q == '"' and c == "\\":
                j += 2
                continue
            if c == q:
                if q == "'" and j + 1 < len(s) and s[j + 1] == "'":
                    j += 2
                    continue
                return j
            j += 1
        return -1

    def _uncomment(self, text):
        t = text.strip()
        if not t or t.startswith("#"):
            return ""
        if t[0] in "\"'":
            e = self._quote_end(t, 0)
            return t[:e + 1] if e >= 0 else t
        if t[0] in "[{":
            q = None
            for j, c in enumerate(t):
                if q:
                    if c == q:
                        q = None
                elif c in "\"'":
                    q = c
                elif c == "#" and j and t[j - 1] == " ":
                    return t[:j].rstrip()
            return t
        k = t.find(" #")
        return t[:k].rstrip() if k >= 0 else t

    def _split_key(self, s):
        if s[:1] in "\"'":
            e = self._quote_end(s, 0)
            if e < 0:
                return None
            after = s[e + 1:].lstrip()
            if after == ":" or after.startswith(": "):
                return str(self._scalar(s[:e + 1])), after[1:].strip()
            return None
        if s[:1] in "[{#&*!|>%@`":
            return None
        j = 0
        while True:
            j = s.find(":", j)
            if j < 0:
                return None
            if j == len(s) - 1 or s[j + 1] == " ":
                key = s[:j].strip()
                return (key, s[j + 1:].strip()) if key else None
            j += 1

    # -- structure --------------------------------------------------------
    def parse(self):
        self._skip()
        if self.i >= len(self.lines):
            return None
        value = self._node(self._ind(self.lines[self.i]))
        self._skip()
        if self.i < len(self.lines):
            self._err("unexpected indentation")
        return value

    def _node(self, ind):
        s = self.lines[self.i].strip()
        if self._is_item(s):
            return self._seq(ind)
        if self._split_key(s) is not None:
            return self._map(ind)
        self.i += 1
        return self._value(s, ind)

    def _seq(self, ind):
        out = []
        while True:
            self._skip()
            if self.i >= len(self.lines):
                break
            line = self.lines[self.i]
            if self._ind(line) != ind or not self._is_item(line.strip()):
                break
            rest = line[ind + 1:]
            body = rest.strip()
            if not body:
                self.i += 1
                self._skip()
                if self.i < len(self.lines) and self._ind(self.lines[self.i]) > ind:
                    out.append(self._node(self._ind(self.lines[self.i])))
                else:
                    out.append(None)
            elif self._is_item(body) or self._split_key(body) is not None:
                col = ind + 1 + (len(rest) - len(rest.lstrip(" ")))
                self.lines[self.i] = " " * col + rest.lstrip(" ")
                out.append(self._node(col))
            else:
                self.i += 1
                out.append(self._value(body, ind))
        return out

    def _map(self, ind):
        out = {}
        while True:
            self._skip()
            if self.i >= len(self.lines):
                break
            line = self.lines[self.i]
            cur = self._ind(line)
            if cur < ind:
                break
            if cur > ind:
                self._err("bad indentation")
            s = line.strip()
            if self._is_item(s):
                break
            kv = self._split_key(s)
            if kv is None:
                self._err("expected 'key: value', got %r" % s)
            key, rest = kv
            self.i += 1
            rest = self._uncomment(rest)
            if rest:
                out[key] = self._value(rest, ind)
                continue
            out[key] = None
            self._skip()
            if self.i < len(self.lines):
                nxt = self.lines[self.i]
                ni = self._ind(nxt)
                if ni > ind or (ni == ind and self._is_item(nxt.strip())):
                    out[key] = self._node(ni)
        return out

    # -- values -----------------------------------------------------------
    def _value(self, text, parent_ind):
        text = self._uncomment(text)
        if text[:1] in ("|", ">"):
            return self._block(text, parent_ind)
        return self._scalar(text)

    def _block(self, header, parent_ind):
        style, chomp, explicit = header[0], "clip", 0
        for c in header[1:]:
            if c == "-":
                chomp = "strip"
            elif c == "+":
                chomp = "keep"
            elif c.isdigit():
                explicit = int(c)
        content_ind = None
        for j in range(self.i, len(self.lines)):
            if self.lines[j].strip():
                content_ind = self._ind(self.lines[j])
                break
        if explicit:
            content_ind = parent_ind + explicit
        if content_ind is None or content_ind <= parent_ind:
            return ""
        lines = []
        while self.i < len(self.lines):
            ln = self.lines[self.i]
            if not ln.strip():
                lines.append("")
            elif self._ind(ln) < content_ind:
                break
            else:
                lines.append(ln[content_ind:])
            self.i += 1
        trail = 0
        while lines and lines[-1] == "":
            lines.pop()
            trail += 1
        if not lines:
            return "\n" * trail if chomp == "keep" else ""
        if style == "|":
            body = "\n".join(lines)
        else:
            body, prev = lines[0], lines[0]
            for ln in lines[1:]:
                if ln == "":
                    body += "\n"
                elif prev == "":
                    body += ln
                elif ln.startswith(" ") or prev.startswith(" "):
                    body += "\n" + ln
                else:
                    body += " " + ln
                prev = ln
        if chomp == "strip":
            return body
        if chomp == "clip":
            return body + "\n"
        return body + "\n" + "\n" * trail

    def _dq(self, t):
        out, j = [], 1
        while j < len(t):
            c = t[j]
            if c == '"':
                return "".join(out)
            if c == "\\":
                e = t[j + 1] if j + 1 < len(t) else ""
                if e in self._ESC:
                    out.append(self._ESC[e])
                    j += 2
                    continue
                if e in ("x", "u", "U"):
                    n = {"x": 2, "u": 4, "U": 8}[e]
                    try:
                        out.append(chr(int(t[j + 2:j + 2 + n], 16)))
                    except ValueError:
                        self._err("bad \\%s escape" % e)
                    j += 2 + n
                    continue
                self._err("unknown escape \\%s" % e)
            out.append(c)
            j += 1
        self._err("string is missing its closing \"")

    def _scalar(self, t):
        t = t.strip()
        if not t:
            return None
        if t[0] == '"':
            return self._dq(t)
        if t[0] == "'":
            e = self._quote_end(t, 0)
            if e < 0:
                self._err("string is missing its closing '")
            return t[1:e].replace("''", "'")
        if t[0] in "[{":
            value, end = self._flow(t, 0)
            if t[end:].strip():
                self._err("unexpected text after %r" % t[:end])
            return value
        low = t.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if low in ("null", "~"):
            return None
        if re.fullmatch(r"[-+]?\d+", t):
            return int(t)
        if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?", t):
            return float(t)
        return t

    def _flow(self, t, j):
        def ws(k):
            while k < len(t) and t[k] == " ":
                k += 1
            return k

        j = ws(j)
        if j >= len(t):
            return None, j
        c = t[j]
        if c in "[{":
            close = "]" if c == "[" else "}"
            res = [] if c == "[" else {}
            j = ws(j + 1)
            while True:
                if j >= len(t):
                    self._err("missing %s" % close)
                if t[j] == close:
                    return res, j + 1
                if c == "[":
                    v, j = self._flow(t, j)
                    res.append(v)
                else:
                    k, j = self._flow(t, j)
                    j = ws(j)
                    v = None
                    if j < len(t) and t[j] == ":":
                        v, j = self._flow(t, j + 1)
                    res[str(k)] = v
                j = ws(j)
                if j < len(t) and t[j] == ",":
                    j = ws(j + 1)
                elif not (j < len(t) and t[j] == close):
                    self._err("expected , or %s" % close)
        if c in "\"'":
            e = self._quote_end(t, j)
            if e < 0:
                self._err("unclosed quote")
            return self._scalar(t[j:e + 1]), e + 1
        k = j
        while k < len(t) and t[k] not in ",]}" and not (
                t[k] == ":" and (k + 1 == len(t) or t[k + 1] in " ,]}")):
            k += 1
        return self._scalar(t[j:k]), k


# --------------------------------------------------------------------------
# Matching and rendering (no Windows APIs here)
# --------------------------------------------------------------------------

class Match:
    __slots__ = ("triggers", "replace", "word", "propagate_case", "vars")


class Engine:
    def __init__(self):
        self.index = []          # (trigger as compared, Match), longest first
        self.global_vars = []
        self.toggle_key = "OFF"
        self.maxlen = 0
        self.buf = ""

    @staticmethod
    def _vars(raw, where, problems):
        out = []
        if raw is None:
            return out
        if not isinstance(raw, list):
            problems.append("%s: 'vars' must be a list" % where)
            return out
        for v in raw:
            if not isinstance(v, dict) or not v.get("name") or not v.get("type"):
                problems.append("%s: every var needs a name and a type" % where)
                continue
            if v["type"] not in VAR_TYPES:
                problems.append("%s: var type '%s' is not supported (use %s)"
                                % (where, v["type"], ", ".join(VAR_TYPES)))
                continue
            params = v.get("params") or {}
            if not isinstance(params, dict):
                problems.append("%s: params of var '%s' must be 'key: value' lines"
                                % (where, v["name"]))
                continue
            out.append({"name": str(v["name"]), "type": v["type"], "params": params})
        return out

    def load(self, cfg):
        """Replace the active snippets; return a list of problems found."""
        problems = []
        if cfg is None:
            cfg = {}
        if not isinstance(cfg, dict):
            return ["The file must start with settings such as 'matches:'."]
        gvars = self._vars(cfg.get("global_vars"), "global_vars", problems)
        matches = cfg.get("matches") or []
        if not isinstance(matches, list):
            problems.append("'matches' must be a list (each item starts with '- ').")
            matches = []
        index, seen = [], set()
        for n, item in enumerate(matches, 1):
            where = "match #%d" % n
            if not isinstance(item, dict):
                problems.append("%s: expected 'trigger:' and 'replace:'" % where)
                continue
            trigs = item.get("triggers", item.get("trigger"))
            if isinstance(trigs, (str, int, float)):
                trigs = [trigs]
            trigs = [str(t) for t in (trigs if isinstance(trigs, list) else [])
                     if t is not None and str(t) != ""]
            if not trigs:
                problems.append("%s: missing 'trigger'" % where)
                continue
            where = "match %s" % trigs[0]
            if "replace" not in item:
                other = [k for k in ("form", "image_path", "html", "markdown", "regex")
                         if k in item]
                problems.append("%s: %s" % (where, "'%s' is not supported" % other[0]
                                            if other else "missing 'replace'"))
                continue
            m = Match()
            m.triggers = trigs
            m.replace = "" if item["replace"] is None else str(item["replace"])
            m.word = bool(item.get("word", False))
            m.propagate_case = bool(item.get("propagate_case", False))
            m.vars = self._vars(item.get("vars"), where, problems)
            known = {v["name"] for v in gvars + m.vars}
            for name in VAR_RE.findall(m.replace):
                if name not in known:
                    problems.append("%s: {{%s}} is used but not defined in vars"
                                    % (where, name))
            for t in trigs:
                key = t.lower() if m.propagate_case else t
                if key in seen:
                    problems.append("trigger %s is defined twice; the first one is used" % t)
                    continue
                seen.add(key)
                index.append((key, m))
        index.sort(key=lambda e: -len(e[0]))
        tk = cfg.get("toggle_key", "OFF")
        tk = "OFF" if tk in (None, False) else str(tk).upper()
        if tk not in ("OFF", "ALT", "CTRL", "SHIFT"):
            problems.append("toggle_key must be ALT, CTRL, SHIFT or OFF")
            tk = "OFF"
        self.index, self.global_vars, self.toggle_key = index, gvars, tk
        self.maxlen = max((len(k) for k, _ in index), default=0) + 2
        self.buf = ""
        return problems

    def reset(self):
        self.buf = ""

    def backspace(self):
        self.buf = self.buf[:-1]

    def feed(self, ch):
        """Add one typed character. On a hit return (match, typed trigger, separator)."""
        if not self.index:
            return None
        self.buf = buf = (self.buf + ch)[-self.maxlen:]
        is_sep = ch in SEPARATORS
        for key, m in self.index:
            n = len(key)
            if m.word:
                if not is_sep or len(buf) < n + 1:
                    continue
                typed, before = buf[-n - 1:-1], buf[:-n - 1]
            else:
                if len(buf) < n:
                    continue
                typed, before = buf[-n:], ""
            if (typed.lower() if m.propagate_case else typed) != key:
                continue
            if m.word and before and before[-1] not in SEPARATORS:
                continue
            self.buf = ""
            return m, typed, (ch if m.word else "")
        return None

    def render(self, m, typed):
        """Return (text, chars_after_cursor or None)."""
        used = set(VAR_RE.findall(m.replace))
        for v in m.vars:
            used |= set(VAR_RE.findall(repr(v["params"])))
        values = {}
        for v in [g for g in self.global_vars if g["name"] in used] + m.vars:
            values[v["name"]] = eval_var(v, values)
        text = VAR_RE.sub(lambda mo: values.get(mo.group(1), mo.group(0)), m.replace)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if m.propagate_case:
            letters = [c for c in typed if c.isalpha()]
            if len(letters) > 1 and all(c.isupper() for c in letters):
                text = text.upper()
            elif letters and letters[0].isupper():
                for k, c in enumerate(text):
                    if c.isalpha():
                        text = text[:k] + c.upper() + text[k + 1:]
                        break
        k = text.find(CURSOR)
        if k < 0:
            return text, None
        text = text[:k] + text[k + len(CURSOR):]
        return text, len(text) - k


def eval_var(v, values):
    def inject(x):
        if isinstance(x, str):
            return VAR_RE.sub(lambda mo: str(values.get(mo.group(1), mo.group(0))), x)
        return x

    p = {k: inject(x) for k, x in v["params"].items()}
    t = v["type"]
    if t == "date":
        when = datetime.datetime.now() + datetime.timedelta(seconds=int(p.get("offset") or 0))
        return when.strftime(str(p.get("format") or "%Y-%m-%d"))
    if t == "clipboard":
        return read_clipboard()
    if t == "echo":
        return str(p.get("echo", ""))
    if t == "random":
        choices = p.get("choices") or [""]
        return str(inject(random.choice(choices)))
    if t == "shell":
        return run_shell(str(p.get("cmd", "")), str(p.get("shell") or "powershell"),
                         p.get("trim", True))
    raise ValueError("unsupported var type %s" % t)


def run_shell(cmd, shell, trim):
    commands = {
        "cmd": ["cmd", "/c", cmd],
        "powershell": ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
        "pwsh": ["pwsh", "-NoProfile", "-NonInteractive", "-Command", cmd],
    }
    if shell not in commands:
        raise ValueError("shell must be cmd, powershell or pwsh")
    r = subprocess.run(commands[shell], capture_output=True, text=True, timeout=10,
                       stdin=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout.strip() if trim else r.stdout


def read_clipboard():  # replaced below on Windows
    return ""


# --------------------------------------------------------------------------
# Windows: keyboard hook, synthetic typing, clipboard, message boxes
# --------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    import winsound
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ULONG_PTR = ctypes.c_size_t
    LRESULT = ctypes.c_ssize_t

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    def _sig(fn, restype, *argtypes):
        fn.restype, fn.argtypes = restype, list(argtypes)

    _sig(user32.SetWindowsHookExW, wintypes.HHOOK, ctypes.c_int, HOOKPROC,
         wintypes.HINSTANCE, wintypes.DWORD)
    _sig(user32.CallNextHookEx, LRESULT, wintypes.HHOOK, ctypes.c_int,
         wintypes.WPARAM, wintypes.LPARAM)
    _sig(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG),
         wintypes.HWND, wintypes.UINT, wintypes.UINT)
    _sig(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
    _sig(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
    _sig(user32.SendInput, wintypes.UINT, wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    _sig(user32.ToUnicodeEx, ctypes.c_int, wintypes.UINT, wintypes.UINT,
         ctypes.POINTER(ctypes.c_ubyte), wintypes.LPWSTR, ctypes.c_int,
         wintypes.UINT, wintypes.HKL)
    _sig(user32.GetKeyboardLayout, wintypes.HKL, wintypes.DWORD)
    _sig(user32.GetForegroundWindow, wintypes.HWND)
    _sig(user32.GetWindowThreadProcessId, wintypes.DWORD, wintypes.HWND, ctypes.c_void_p)
    _sig(user32.GetAsyncKeyState, ctypes.c_short, ctypes.c_int)
    _sig(user32.GetKeyState, ctypes.c_short, ctypes.c_int)
    _sig(user32.MessageBoxW, ctypes.c_int, wintypes.HWND, wintypes.LPCWSTR,
         wintypes.LPCWSTR, wintypes.UINT)
    _sig(user32.OpenClipboard, wintypes.BOOL, wintypes.HWND)
    _sig(user32.CloseClipboard, wintypes.BOOL)
    _sig(user32.GetClipboardData, wintypes.HANDLE, wintypes.UINT)
    _sig(kernel32.GlobalLock, ctypes.c_void_p, wintypes.HANDLE)
    _sig(kernel32.GlobalUnlock, wintypes.BOOL, wintypes.HANDLE)
    _sig(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
    _sig(kernel32.CreateMutexW, wintypes.HANDLE, ctypes.c_void_p, wintypes.BOOL,
         wintypes.LPCWSTR)
    _sig(kernel32.CreateEventW, wintypes.HANDLE, ctypes.c_void_p, wintypes.BOOL,
         wintypes.BOOL, wintypes.LPCWSTR)
    _sig(kernel32.SetEvent, wintypes.BOOL, wintypes.HANDLE)
    _sig(kernel32.ResetEvent, wintypes.BOOL, wintypes.HANDLE)
    _sig(kernel32.WaitForSingleObject, wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
    KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
    VK_BACK, VK_TAB, VK_RETURN, VK_LEFT = 0x08, 0x09, 0x0D, 0x25
    VK_SHIFT, VK_CONTROL, VK_MENU, VK_CAPITAL = 0x10, 0x11, 0x12, 0x14
    VK_LWIN, VK_RWIN = 0x5B, 0x5C
    MODIFIER_VKS = {0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0x90, 0x91,
                    0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}
    RESET_VKS = {0x1B, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
    TOGGLE_VKS = {"ALT": {0x12, 0xA4, 0xA5}, "CTRL": {0x11, 0xA2, 0xA3},
                  "SHIFT": {0x10, 0xA0, 0xA1}}

    def _down(vk):
        return user32.GetAsyncKeyState(vk) & 0x8000

    def _translate(vk, scan):
        """Turn a key-down into an event for the worker (runs inside the hook)."""
        hwnd = user32.GetForegroundWindow() or 0
        if vk in MODIFIER_VKS:
            return ("mod", vk, hwnd)
        ctrl, alt = _down(VK_CONTROL), _down(VK_MENU)
        if _down(VK_LWIN) or _down(VK_RWIN) or (ctrl and not alt) or (alt and not ctrl):
            return ("reset", hwnd)              # a shortcut, not typing
        if vk == VK_BACK:
            return ("back", hwnd)
        if vk == VK_RETURN:
            return ("char", "\n", hwnd)
        if vk == VK_TAB:
            return ("char", "\t", hwnd)
        if vk in RESET_VKS:
            return ("reset", hwnd)              # caret moved
        state = (ctypes.c_ubyte * 256)()
        if _down(VK_SHIFT):
            state[VK_SHIFT] = 0x80
        if user32.GetKeyState(VK_CAPITAL) & 1:
            state[VK_CAPITAL] = 0x01
        if ctrl and alt:                        # AltGr
            state[VK_CONTROL] = state[VK_MENU] = 0x80
        hkl = user32.GetKeyboardLayout(user32.GetWindowThreadProcessId(hwnd, None))
        out = ctypes.create_unicode_buffer(8)
        n = user32.ToUnicodeEx(vk, scan, state, out, 8, 0x4, hkl)  # 0x4: keep dead-key state
        if n > 0:
            s = out[:n]
            if s.isprintable():
                return ("char", s, hwnd)
        return ("other", hwnd)

    def _make_hook(events):
        @HOOKPROC
        def proc(code, wparam, lparam):
            if code == 0:
                try:
                    kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if kb.dwExtraInfo != MAGIC:
                        if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                            events.put(_translate(kb.vkCode, kb.scanCode))
                        elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                            events.put(("up", kb.vkCode))
                except Exception:
                    pass
            return user32.CallNextHookEx(None, code, wparam, lparam)
        return proc

    def _key(vk=0, scan=0, flags=0):
        i = INPUT()
        i.type = 1  # INPUT_KEYBOARD
        i.u.ki = KEYBDINPUT(vk, scan, flags, 0, MAGIC)
        return i

    def _tap(vk, flags=0):
        return [_key(vk, 0, flags), _key(vk, 0, flags | KEYEVENTF_KEYUP)]

    def _send(seq):
        user32.SendInput(len(seq), (INPUT * len(seq))(*seq), ctypes.sizeof(INPUT))

    def type_out(backspaces, text, lefts):
        # One character per SendInput with a short pause: when several Unicode
        # keystrokes are queued at once, some apps (Chromium/Electron, Office)
        # turn all of them into the last character of the batch.
        if backspaces:
            _send([k for _ in range(backspaces) for k in _tap(VK_BACK)])
            time.sleep(0.01)
        for ch in text:
            if ch == "\n":
                # Shift+Enter: a new line in chat apps (Teams, Slack) where a
                # plain Enter sends the message, and still a new line elsewhere.
                _send([_key(VK_SHIFT)] + _tap(VK_RETURN) + [_key(VK_SHIFT, 0, KEYEVENTF_KEYUP)])
                time.sleep(0.03)
            elif ch == "\t":
                _send(_tap(VK_TAB))
            else:
                b = ch.encode("utf-16-le")
                seq = []
                for k in range(0, len(b), 2):
                    u = int.from_bytes(b[k:k + 2], "little")
                    seq += [_key(0, u, KEYEVENTF_UNICODE),
                            _key(0, u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
                _send(seq)
            time.sleep(0.004)
        if lefts:
            _send([k for _ in range(lefts) for k in _tap(VK_LEFT, KEYEVENTF_EXTENDEDKEY)])

    def read_clipboard():  # noqa: F811 - Windows version
        for _ in range(10):
            if user32.OpenClipboard(None):
                break
            time.sleep(0.02)
        else:
            return ""
        try:
            h = user32.GetClipboardData(13)  # CF_UNICODETEXT
            if not h:
                return ""
            p = kernel32.GlobalLock(h)
            if not p:
                return ""
            try:
                return ctypes.wstring_at(p)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()

    _last_problem = [None]

    def show_problem(msg):
        if msg == _last_problem[0]:
            return
        _last_problem[0] = msg
        threading.Thread(target=user32.MessageBoxW,
                         args=(None, msg, "meh", 0x30 | 0x10000 | 0x40000),
                         daemon=True).start()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    if sys.platform != "win32":
        sys.exit("meh runs on Windows only.")
    if len(sys.argv) > 1:
        path = os.path.abspath(sys.argv[1])
    else:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meh.yml")
    name = os.path.basename(path)

    # One copy at a time; starting it again offers to quit the running one.
    mutex = kernel32.CreateMutexW(None, False, "Local\\meh-text-expander")  # noqa: F841
    already_running = ctypes.get_last_error() == 183
    quit_event = kernel32.CreateEventW(None, True, False, "Local\\meh-quit")
    if already_running:
        if user32.MessageBoxW(None, "meh is already running.\n\nQuit it?", "meh",
                              0x4 | 0x20 | 0x40000) == 6:
            kernel32.SetEvent(quit_event)
        return
    kernel32.ResetEvent(quit_event)

    engine = Engine()
    events = queue.Queue()
    enabled = [True]

    def load():
        try:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
        except OSError as e:
            show_problem("Could not open the snippet file:\n%s\n\n%s" % (path, e.strerror))
            return
        try:
            problems = engine.load(parse_yaml(text))
        except Exception as e:
            show_problem("Could not read %s:\n%s\n\nThe previous snippets stay active."
                         % (name, e))
            return
        if problems:
            show_problem("Problems in %s:\n\n- %s" % (name, "\n- ".join(problems[:15])))
        else:
            _last_problem[0] = None

    def watch():
        last = ()
        while True:
            try:
                st = os.stat(path)
                sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                sig = None
            if sig != last:
                time.sleep(0.3)                 # let the editor finish saving
                try:
                    st = os.stat(path)
                    sig = (st.st_mtime_ns, st.st_size)
                except OSError:
                    sig = None
                last = sig
                load()
            time.sleep(1)

    def expand(hit):
        m, typed, sep = hit
        try:
            text, after_cursor = engine.render(m, typed)
        except Exception as e:
            show_problem("Could not expand %s:\n%s" % (m.triggers[0], e))
            return
        lefts = after_cursor + len(sep) if after_cursor is not None else 0
        time.sleep(0.01)
        type_out(len(typed) + len(sep), text + sep, lefts)

    def worker():
        last_hwnd, pending, last_tap = None, None, 0.0
        while True:
            ev = events.get()
            try:
                kind = ev[0]
                toggle_vks = TOGGLE_VKS.get(engine.toggle_key, ())
                if kind == "mod":
                    if ev[1] in toggle_vks:
                        if pending is None:
                            pending = time.monotonic()
                    else:
                        pending, last_tap = None, 0.0
                    continue
                if kind == "up":
                    if ev[1] in toggle_vks and pending is not None:
                        now = time.monotonic()
                        if now - pending < 0.3:
                            if now - last_tap < 0.4:
                                enabled[0] = not enabled[0]
                                engine.reset()
                                last_tap = 0.0
                                winsound.Beep(880 if enabled[0] else 440, 120)
                            else:
                                last_tap = now
                        else:
                            last_tap = 0.0
                        pending = None
                    continue
                pending, last_tap = None, 0.0
                hwnd = ev[-1]
                if hwnd != last_hwnd:
                    engine.reset()
                    last_hwnd = hwnd
                if not enabled[0]:
                    continue
                if kind == "back":
                    engine.backspace()
                elif kind == "reset":
                    engine.reset()
                elif kind == "char":
                    for ch in ev[1]:
                        hit = engine.feed(ch)
                        if hit:
                            expand(hit)
            except Exception as e:
                show_problem("meh ran into an error:\n%r" % (e,))

    threading.Thread(target=watch, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()

    def wait_for_quit():
        kernel32.WaitForSingleObject(quit_event, 0xFFFFFFFF)
        os._exit(0)

    threading.Thread(target=wait_for_quit, daemon=True).start()

    proc = _make_hook(events)
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, kernel32.GetModuleHandleW(None), 0)
    if not hook:
        user32.MessageBoxW(None, "Could not start the keyboard hook (error %d)."
                           % ctypes.get_last_error(), "meh", 0x10)
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    print("Meh Expander is running.")
    main()
