#!/usr/bin/env python3
"""HostMount: ventana para instalar en la PS5 juegos que se leen por red desde este PC (Windows).

Lanza el instalador (install/blazingmount-install.py) y el servidor (host/serve/dirserve.py) de este
mismo kit, basado en BlazingMount; no cambia cómo funcionan, solo los muestra de forma legible.

Copyright (C) 2026 sakulsol. GPL-3.0-or-later, como BlazingMount.
"""
import ftplib
import glob
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import deque

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

WIN = sys.platform == "win32"
HOME = os.path.expanduser("~")
CFG_DIR = os.path.join(os.environ.get("APPDATA", HOME), "HostMount")
HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(CFG_DIR, "config.json")
KIT_PARENT = os.path.join(HOME, "hostmount-kit")
OLD_KIT = os.path.join(HOME, "blazingmount-kit", "blazingmount")
KIT_MARK = "3"                       # WINDOWS-PATCH del kit compilado para Windows
SERVE_PORT, FTP_PORT, KRAKEN_PORT = 4452, 2121, 8097
STAGE_DIR, HOMEBREW_DIR, LINK_DIR = "/data/blz-stage/incoming", "/data/homebrew", "/data/blazing/link"
NOWINDOW = 0x08000000 if WIN else 0
TID_RX = re.compile(r"\b([A-Z]{4}\d{5})\b")

C = dict(bg="#0F1217", side="#0C0F14", card="#131821", card2="#0F1319", line="#1D222B", field="#0B0E13",
         btn="#171C25", btnh="#202733", btnline="#2A313D", text="#E7EAF0", mute="#AEB6C4", dim="#8A93A3",
         faint="#5B6576", accent="#FF6B2C", accenth="#FF8A4C", ink="#1A0D05", ok="#34D399", okbg="#123826",
         okfg="#6EE7B7", warnbg="#2A1E10", warnline="#5A3B12", warn="#FFD9A8", warnicon="#FFB04C",
         errbg="#2A1416", errline="#5A2323", err="#FF9C9C", log="#090B0F", sel="#1C222D")
FONT = "Segoe UI" if WIN else "DejaVu Sans"
FONT_B = "Segoe UI Semibold" if WIN else "DejaVu Sans"
MONO = "Consolas" if WIN else "DejaVu Sans Mono"


def python_exe():
    """python.exe (con salida) aunque esta ventana corra con pythonw.exe."""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        alt = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.exists(alt):
            return alt
    return exe


PY = python_exe()

# Lanza el instalador del kit avisando de cada archivo subido (@@PUT), para la barra de progreso.
RUNNER = r'''
import importlib.util, os, sys
path = sys.argv[1]
spec = importlib.util.spec_from_file_location("bminstall", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
_put = m.ftp_put
def put(ftp, local, remote):
    _put(ftp, local, remote)
    print("@@PUT %d %s" % (os.path.getsize(local), remote), flush=True)
m.ftp_put = put
sys.argv = [path] + sys.argv[2:]
m.main()
'''


# ---------------------------------------------------------------- utilidades sin interfaz

def load_cfg():
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("ps5", "192.168.1.38")
    cfg.setdefault("fw", "12.60")
    cfg.setdefault("kit", "")
    cfg.setdefault("ampr_off", True)
    cfg.setdefault("games", {})
    return cfg


def save_cfg(cfg):
    os.makedirs(CFG_DIR, exist_ok=True)
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1, ensure_ascii=False)
    os.replace(tmp, CFG_PATH)


def kit_ok(d):
    try:
        with open(os.path.join(d, "WINDOWS-PATCH")) as f:
            return f.read().strip() == KIT_MARK and os.path.isfile(os.path.join(d, "install", "blazingmount-install.py"))
    except OSError:
        return False


def extract_kit(zpath, dest):
    """Extrae la carpeta del kit (la que tiene WINDOWS-PATCH) de un zip en dest. Devuelve dest o None."""
    with zipfile.ZipFile(zpath) as zf:
        names = zf.namelist()
        mark = next((n for n in names if n.endswith("/WINDOWS-PATCH")
                     and n[:-len("WINDOWS-PATCH")] + "install/blazingmount-install.py" in names), None)
        if not mark or zf.read(mark).decode("utf-8", "replace").strip() != KIT_MARK:
            return None
        prefix = mark[:-len("WINDOWS-PATCH")]
        tmp = dest + ".nuevo"
        shutil.rmtree(tmp, ignore_errors=True)
        for n in names:
            if not n.startswith(prefix) or n.endswith("/"):
                continue
            parts = n[len(prefix):].split("/")
            if any(p in ("", "..") for p in parts):
                continue
            out = os.path.join(tmp, *parts)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with zf.open(n) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
    return dest


def find_kit(cfg):
    for d in (HERE, os.path.dirname(os.path.dirname(HERE)), cfg.get("kit"), os.path.join(KIT_PARENT, "hostmount"), OLD_KIT):
        if d and kit_ok(d):
            return d
    zips = []
    for sub in ("Downloads", "Descargas", "Desktop", "Escritorio"):
        zips += glob.glob(os.path.join(HOME, sub, "*.zip"))
    for z in sorted(zips, key=os.path.getmtime, reverse=True):
        try:
            d = extract_kit(z, os.path.join(KIT_PARENT, "hostmount"))
        except (OSError, zipfile.BadZipFile):
            continue
        if d and kit_ok(d):
            return d
    return None


def port_open(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def local_ip(ps5):
    """IP de este PC en la red de la PS5 (la que usa la ruta hacia ella, no la de la VPN)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ps5, 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def fw_of(v):
    v = int(v, 0) if isinstance(v, str) else (v or 0)
    return float("%x.%02x" % ((v >> 56) & 0xFF, (v >> 48) & 0xFF)) if v else None


def find_app_folder(folder):
    """La carpeta con sce_sys/param.json: la elegida o, si no, la única por debajo (2 niveles)."""
    if os.path.isfile(os.path.join(folder, "sce_sys", "param.json")):
        return folder
    hits = []
    for dp, dn, _ in os.walk(folder):
        if dp[len(folder):].count(os.sep) >= 2:
            dn[:] = []
        if os.path.isfile(os.path.join(dp, "sce_sys", "param.json")):
            hits.append(dp)
            dn[:] = []
    return hits[0] if len(hits) == 1 else None


def game_info(app):
    with open(os.path.join(app, "sce_sys", "param.json"), encoding="utf-8-sig") as f:
        d = json.load(f)
    loc = d.get("localizedParameters", {})
    pick = loc.get("es-ES") or loc.get("en-US") or loc.get(loc.get("defaultLanguage", "en-US")) or {}
    nfo_fw, nfo_name = None, None
    for base in (app, os.path.dirname(app), os.path.dirname(os.path.dirname(app))):
        for nfo in glob.glob(os.path.join(base, "*.nfo")):
            try:
                with open(nfo, encoding="latin-1") as f:
                    fws = [float(x) for x in re.findall(r"FW\s*([0-9]+\.[0-9]+)", f.read(), re.I)]
            except (OSError, ValueError):
                continue
            if fws and (nfo_fw is None or min(fws) < nfo_fw):
                nfo_fw, nfo_name = min(fws), os.path.basename(nfo)
    icon = os.path.join(app, "sce_sys", "icon0.png")
    return {"tid": d.get("titleId", "?"), "name": pick.get("titleName") or os.path.basename(app),
            "version": d.get("contentVersion", ""), "fw": fw_of(d.get("requiredSystemSoftwareVersion", 0)),
            "nfo_fw": nfo_fw, "nfo": nfo_name, "icon": icon if os.path.isfile(icon) else None}


def tree_size(folder):
    n = b = 0
    for dp, _, fn in os.walk(folder):
        for x in fn:
            try:
                b += os.path.getsize(os.path.join(dp, x))
                n += 1
            except OSError:
                pass
    return n, b


def human(b):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (b, unit)
        b /= 1024.0


def nfmt(n):
    return "{:,}".format(n).replace(",", ".")


def ftp_open(ps5):
    ftp = ftplib.FTP()
    ftp.encoding = "utf-8"
    ftp.connect(ps5, FTP_PORT, timeout=15)
    ftp.login()
    return ftp


def ftp_names(ftp, path):
    try:
        return [n.rsplit("/", 1)[-1] for n in ftp.nlst(path)]
    except ftplib.all_errors:
        return []


def ftp_rmtree(ftp, path):
    ftp.cwd(path)
    lines = []
    ftp.retrlines("LIST", lines.append)
    for line in lines:
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        name = parts[8].split(" -> ")[0]
        if name in (".", ".."):
            continue
        sub = path + "/" + name
        if line.startswith("d"):
            ftp_rmtree(ftp, sub)
        else:
            ftp.delete(sub)
    ftp.cwd("/")
    ftp.rmd(path)


def kraken_ampr_off(ps5, tid, wait=90):
    """Desactiva ampr-emu-ng para tid en Kraken Mount (POST /api/ampr_title/<tid> off)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = "http://%s:%d/api/ampr_title/%s" % (ps5, KRAKEN_PORT, tid)
    end = time.time() + wait
    while True:
        try:
            with opener.open(urllib.request.Request(url, data=b"off", method="POST"), timeout=6):
                return True, "ampr-emu-ng desactivado para " + tid
        except urllib.error.HTTPError as e:
            if e.code != 404:
                return False, "Kraken Mount respondió %d" % e.code
        except OSError:
            return False, "Kraken Mount no responde en el puerto %d: desactívalo a mano" % KRAKEN_PORT
        if time.time() > end:
            return False, "Kraken Mount aún no ve el juego: desactívalo a mano"
        time.sleep(4)


# ---------------------------------------------------------------- servidor (dirserve)

class Server:
    REQ = re.compile(r" -> (\d{3}) (\d+) B (\d+) ms")

    def __init__(self, app):
        self.app = app
        self.proc = None
        self.roots = []
        self.reqs = 0
        self.hits = deque()
        self.last_tid = None
        self.lock = threading.Lock()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, kit, roots):
        roots = [r for r in dict.fromkeys(roots) if r and os.path.isdir(r)]
        if not roots:
            return "No hay carpetas de juegos que servir."
        if self.running() and roots == self.roots:
            return None
        self.stop()
        if port_open("127.0.0.1", SERVE_PORT, 0.5):
            return "Otro programa ya usa el puerto %d (¿la ventana de PowerShell de antes?). Ciérralo y pulsa Iniciar." % SERVE_PORT
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        script = os.path.join(kit, "host", "serve", "dirserve.py")
        self.proc = subprocess.Popen([PY, "-u", script] + roots + ["--port", str(SERVE_PORT)], cwd=kit, env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     creationflags=NOWINDOW, text=True, encoding="utf-8", errors="replace", bufsize=1)
        self.roots = roots
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        return None

    def _read(self, proc):
        for line in proc.stdout:
            line = line.rstrip()
            m = self.REQ.search(line)
            if m:
                t = TID_RX.search(line)
                with self.lock:
                    self.reqs += 1
                    self.hits.append((time.time(), int(m.group(2))))
                    if t:
                        self.last_tid = t.group(1)
            self.app.post(self.app.on_server_line, line)
        proc.wait()
        self.app.post(self.app.on_server_exit, proc)

    def rate(self):
        """(bytes/s en los últimos 5 s, title id leído más reciente o None)"""
        now = time.time()
        with self.lock:
            while self.hits and self.hits[0][0] < now - 5:
                self.hits.popleft()
            b = sum(x for _, x in self.hits)
            return b / 5.0, (self.last_tid if self.hits else None)

    def stop(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()


# ---------------------------------------------------------------- piezas de interfaz

class Btn(tk.Label):
    """Botón plano con hover. kind: primary | secondary | danger | link"""
    STY = {"primary": ("accent", "accenth", "ink"), "secondary": ("btn", "btnh", "text"),
           "danger": ("errbg", "errline", "err"), "link": (None, None, "accenth")}

    def __init__(self, parent, text, cmd, kind="secondary", bg=None, **kw):
        base, hover, fg = self.STY[kind]
        self.base = C[base] if base else bg
        self.hover = C[hover] if hover else bg
        self.cmd, self.enabled = cmd, True
        font = (FONT_B, 10, "bold") if kind == "primary" else (FONT, 10)
        super().__init__(parent, text=text, bg=self.base, fg=C[fg], font=font, cursor="hand2",
                         padx=kw.pop("padx", 16), pady=kw.pop("pady", 8), **kw)
        if kind in ("secondary", "danger"):
            self.configure(highlightthickness=1, highlightbackground=C["btnline" if kind == "secondary" else "errline"])
        self.fg = C[fg]
        self.bind("<Enter>", lambda e: self.enabled and self.configure(bg=self.hover))
        self.bind("<Leave>", lambda e: self.configure(bg=self.base))
        self.bind("<Button-1>", lambda e: self.enabled and self.cmd and self.cmd())

    def set_enabled(self, on):
        self.enabled = on
        self.configure(fg=self.fg if on else C["faint"], cursor="hand2" if on else "arrow")


def label(parent, text="", size=10, bold=False, fg="text", bg="bg", **kw):
    return tk.Label(parent, text=text, font=(FONT_B if bold else FONT, size) + (("bold",) if bold else ()),
                    fg=C.get(fg, fg), bg=C.get(bg, bg), anchor=kw.pop("anchor", "w"), justify=kw.pop("justify", "left"), **kw)


def frame(parent, bg="bg", border=False, **kw):
    f = tk.Frame(parent, bg=C.get(bg, bg), **kw)
    if border:
        f.configure(highlightthickness=1, highlightbackground=C["line"])
    return f


class Dot(tk.Canvas):
    def __init__(self, parent, color, bg, size):
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0)
        self.o = self.create_oval(0, 0, size - 1, size - 1, fill=color, outline=color)

    def color(self, c):
        self.itemconfigure(self.o, fill=c, outline=c)


class StepIcon(tk.Canvas):
    def __init__(self, parent, s):
        super().__init__(parent, width=s, height=s, bg=C["card"], highlightthickness=0)
        self.s = s
        self.set("wait")

    def set(self, state):
        s, d = self.s, max(2, self.s // 12)
        self.delete("all")
        if state == "ok":
            self.create_oval(1, 1, s - 2, s - 2, fill=C["okbg"], outline=C["okbg"])
            self.create_line(s * .30, s * .52, s * .44, s * .66, s * .70, s * .36, fill=C["ok"], width=d, capstyle="round")
        elif state == "run":
            self.create_oval(d, d, s - d - 1, s - d - 1, outline=C["accent"], width=d)
            self.create_oval(s * .36, s * .36, s * .64, s * .64, fill=C["accent"], outline=C["accent"])
        elif state == "err":
            self.create_oval(1, 1, s - 2, s - 2, fill=C["errbg"], outline=C["errline"])
            self.create_text(s / 2, s / 2, text="!", fill=C["err"], font=(FONT_B, 10, "bold"))
        else:
            self.create_oval(d, d, s - d - 1, s - d - 1, outline=C["btnline"], width=d)


class Check(tk.Frame):
    """Casilla propia (la de ttk no encaja con el tema oscuro)."""

    def __init__(self, parent, text, var, s):
        super().__init__(parent, bg=C["bg"], cursor="hand2")
        self.var, self.s = var, s
        self.cv = tk.Canvas(self, width=s, height=s, bg=C["bg"], highlightthickness=0)
        self.cv.pack(side="left")
        lb = label(self, text, 10, fg="mute")
        lb.pack(side="left", padx=(max(6, s // 2), 0))
        for w in (self, self.cv, lb):
            w.bind("<Button-1>", self.toggle)
        self.draw()

    def toggle(self, e=None):
        self.var.set(not self.var.get())
        self.draw()

    def draw(self):
        s, cv = self.s, self.cv
        cv.delete("all")
        if self.var.get():
            cv.create_rectangle(0, 0, s - 1, s - 1, fill=C["accent"], outline=C["accent"])
            cv.create_line(s * .22, s * .52, s * .42, s * .72, s * .78, s * .3, fill=C["ink"], width=max(2, s // 8))
        else:
            cv.create_rectangle(1, 1, s - 2, s - 2, outline=C["btnline"], width=max(1, s // 12))


class Scroll(tk.Frame):
    """Marco con barra de desplazamiento vertical."""

    def __init__(self, parent, bg):
        super().__init__(parent, bg=bg)
        self.cv = tk.Canvas(self, bg=bg, highlightthickness=0)
        sb = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview, style="BM.Vertical.TScrollbar")
        self.inner = tk.Frame(self.cv, bg=bg)
        self.sb = sb
        self.inner.bind("<Configure>", self._fit)
        win = self.cv.create_window(0, 0, window=self.inner, anchor="nw")
        self.cv.bind("<Configure>", lambda e: (self.cv.itemconfigure(win, width=e.width), self._fit()))
        self.cv.configure(yscrollcommand=sb.set)
        self.cv.pack(side="left", fill="both", expand=True)
        self.cv.bind_all("<MouseWheel>", self._wheel, add="+")

    def _fit(self, e=None):
        self.cv.configure(scrollregion=self.cv.bbox("all"))
        need = self.inner.winfo_reqheight() > self.cv.winfo_height() > 1
        if need and not self.sb.winfo_ismapped():
            self.sb.pack(side="right", fill="y", before=self.cv)
        elif not need and self.sb.winfo_ismapped():
            self.sb.pack_forget()
            self.cv.yview_moveto(0)

    def _wheel(self, e):
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None and w is not self:
            w = w.master
        if w is self:
            self.cv.yview_scroll(int(-e.delta / 120), "units")


# ---------------------------------------------------------------- aplicación

class App:
    def __init__(self):
        if WIN:
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        self.root = tk.Tk()
        self.S = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        self.root.title("HostMount")
        self.root.configure(bg=C["bg"])
        w, h = self.px(1080), self.px(690)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(w, sw - self.px(40)), min(h, sh - self.px(90))
        self.root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, max(0, (sh - h) // 2 - self.px(20))))
        self.root.minsize(self.px(900), self.px(600))
        self.q = queue.Queue()
        self.cfg = load_cfg()
        self.kit = None
        self.server = Server(self)
        self.server_note = None
        self.install_proc = None
        self.current = None        # juego elegido en la página Instalar
        self.icons = {}
        self.ps5_games = None      # carpetas en /data/homebrew, o None si no se pudo leer
        self._style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(50, self._pump)
        self.root.after(1000, self._tick)
        threading.Thread(target=self._boot, daemon=True).start()

    def px(self, n):
        return int(round(n * self.S))

    def post(self, fn, *a):
        self.q.put((fn, a))

    def _pump(self):
        try:
            while True:
                fn, a = self.q.get_nowait()
                fn(*a)
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def _style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure("BM.Horizontal.TProgressbar", troughcolor=C["field"], background=C["accent"],
                     bordercolor=C["field"], lightcolor=C["accent"], darkcolor=C["accent"], thickness=self.px(10))
        st.configure("BM.Vertical.TScrollbar", troughcolor=C["bg"], background=C["btn"], bordercolor=C["bg"],
                     arrowcolor=C["dim"], lightcolor=C["btn"], darkcolor=C["btn"])
        st.configure("BM.TCheckbutton", background=C["bg"], foreground=C["mute"], font=(FONT, 10),
                     indicatorbackground=C["field"], indicatorforeground=C["accent"])
        st.map("BM.TCheckbutton", background=[("active", C["bg"])], indicatorbackground=[("selected", C["field"])])

    # ------------------------------------------------------------ esqueleto

    def _build(self):
        P = self.px
        side = frame(self.root, "side", width=P(214))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Frame(self.root, bg=C["line"], width=1).pack(side="left", fill="y")
        brand = frame(side, "side")
        brand.pack(fill="x", padx=P(16), pady=(P(18), P(18)))
        logo = tk.Canvas(brand, width=P(22), height=P(22), bg=C["side"], highlightthickness=0)
        s = P(22)
        logo.create_polygon(s * .5, s * .05, s * .82, s * .5, s * .7, s * .92, s * .3, s * .92, s * .18, s * .55,
                            s * .38, s * .3, s * .46, s * .55, fill=C["accent"], smooth=True)
        logo.pack(side="left")
        label(brand, "HostMount", 13, True, bg="side").pack(side="left", padx=(P(8), 0))
        self.nav = {}
        for key, text in (("install", "Instalar juego"), ("games", "Mis juegos en red"), ("settings", "Ajustes")):
            b = tk.Label(side, text=text, font=(FONT, 10), bg=C["side"], fg=C["mute"], anchor="w",
                         padx=P(14), pady=P(9), cursor="hand2")
            b.pack(fill="x", padx=P(10), pady=P(1))
            b.bind("<Button-1>", lambda e, k=key: self.show(k))
            self.nav[key] = b
        tk.Frame(side, bg=C["side"]).pack(fill="both", expand=True)
        box = frame(side, "card", border=True)
        box.pack(fill="x", padx=P(12), pady=P(14))
        row = frame(box, "card")
        row.pack(fill="x", padx=P(12), pady=(P(12), P(2)))
        self.srv_dot = Dot(row, C["faint"], C["card"], P(8))
        self.srv_dot.pack(side="left")
        self.srv_title = label(row, "Servidor parado", 10, True, bg="card")
        self.srv_title.pack(side="left", padx=(P(8), 0))
        self.srv_sub = label(box, "Puerto %d" % SERVE_PORT, 9, fg="dim", bg="card", wraplength=P(170))
        self.srv_sub.pack(fill="x", padx=P(12))
        self.srv_btn = Btn(box, "Iniciar", self.toggle_server, pady=P(5))
        self.srv_btn.pack(fill="x", padx=P(12), pady=(P(8), P(12)))

        self.main = frame(self.root)
        self.main.pack(side="left", fill="both", expand=True)
        self.pages = {"install": self._page_install(), "progress": self._page_progress(),
                      "games": self._page_games(), "settings": self._page_settings()}
        self.show("install")

    def show(self, key):
        if key == "install" and self.install_proc is not None:
            key = "progress"
        for k, p in self.pages.items():
            p.place_forget() if k != key else p.place(relx=0, rely=0, relwidth=1, relheight=1)
        active = "install" if key == "progress" else key
        for k, b in self.nav.items():
            b.configure(bg=C["sel"] if k == active else C["side"], fg="#FFFFFF" if k == active else C["mute"],
                        font=(FONT_B, 10, "bold") if k == active else (FONT, 10))
        if key == "games":
            self.refresh_games()

    def _chip(self, parent):
        P = self.px
        f = frame(parent, "card", border=True)
        d = Dot(f, C["faint"], C["card"], P(7))
        d.pack(side="left", padx=(P(10), P(6)))
        l = label(f, "…", 9, bg="card")
        l.pack(side="left", padx=(0, P(10)), pady=P(5))
        f.pack(side="left", padx=(0, P(8)))
        return d, l

    # ------------------------------------------------------------ página: instalar

    def _page_install(self):
        P = self.px
        pg = frame(self.main)
        foot_line = tk.Frame(pg, bg=C["line"], height=1)
        foot = frame(pg)
        foot.pack(side="bottom", fill="x", padx=P(28), pady=P(16))
        foot_line.pack(side="bottom", fill="x", padx=P(28))
        self.ampr_var = tk.BooleanVar(value=self.cfg.get("ampr_off", True))
        Check(foot, "Desactivar ampr-emu-ng en Kraken Mount al terminar", self.ampr_var, P(16)).pack(side="left")
        self.install_btn = Btn(foot, "Instalar en la PS5  →", self.install, "primary", padx=P(22), pady=P(10))
        self.install_btn.pack(side="right")
        self.install_btn.set_enabled(False)
        body = frame(pg)
        body.pack(fill="both", expand=True, padx=P(28), pady=(P(22), 0))
        chips = frame(body)
        chips.pack(fill="x")
        self.chip = {k: self._chip(chips) for k in ("pc", "ps5", "fw", "kraken")}
        label(body, "Instalar un juego para jugarlo por red", 18, True).pack(fill="x", pady=(P(18), P(4)))
        label(body, "Elige la carpeta del juego en este PC. A la PS5 solo se copian los archivos pequeños; "
                    "los grandes se leen de aquí mientras juegas.", 10, fg="mute", wraplength=P(760)).pack(fill="x")
        label(body, "Carpeta del juego", 9, True, fg="mute").pack(fill="x", pady=(P(18), P(6)))
        row = frame(body)
        row.pack(fill="x")
        self.folder_var = tk.StringVar()
        ent = tk.Entry(row, textvariable=self.folder_var, font=(FONT, 10), bg=C["field"], fg=C["text"],
                       insertbackground=C["text"], relief="flat", highlightthickness=1,
                       highlightbackground=C["btnline"], highlightcolor=C["accent"])
        ent.pack(side="left", fill="x", expand=True, ipady=P(8), padx=(0, P(8)))
        ent.bind("<Return>", lambda e: self.pick(self.folder_var.get().strip()))
        Btn(row, "Examinar…", self.browse).pack(side="left")

        self.card = frame(body, "card", border=True)
        self.card_empty = label(body, "Todavía no has elegido ningún juego.", 10, fg="faint")
        self.card_empty.pack(fill="x", pady=(P(18), 0))
        inner = frame(self.card, "card")
        inner.pack(fill="both", padx=P(16), pady=P(16))
        self.icon_lbl = tk.Label(inner, bg=C["sel"], bd=0)
        self.icon_lbl.pack(side="left", anchor="n")
        info = frame(inner, "card")
        info.pack(side="left", fill="both", expand=True, padx=(P(16), 0))
        head = frame(info, "card")
        head.pack(fill="x")
        self.g_name = label(head, "", 15, True, bg="card")
        self.g_name.pack(side="left")
        self.g_meta = label(head, "", 10, fg="dim", bg="card")
        self.g_meta.pack(side="left", padx=(P(10), 0), pady=(P(4), 0))
        stats = frame(info, "card")
        stats.pack(fill="x", pady=(P(10), P(10)))
        self.g_stats = []
        for i in range(3):
            b = frame(stats, "card2")
            b.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else P(10), 0))
            stats.columnconfigure(i, weight=1, uniform="s")
            t = label(b, "", 8, fg="dim", bg="card2")
            t.pack(fill="x", padx=P(10), pady=(P(8), 0))
            v = label(b, "", 10, True, bg="card2")
            v.pack(fill="x", padx=P(10), pady=(P(1), P(8)))
            self.g_stats.append((t, v))
        self.alert = frame(info, "warnbg")
        self.alert.configure(highlightthickness=1)
        self.alert_icon = label(self.alert, "▲", 9, bg="warnbg")
        self.alert_icon.pack(side="left", anchor="n", padx=(P(10), P(6)), pady=P(8))
        self.alert_txt = label(self.alert, "", 10, bg="warnbg", wraplength=P(560))
        self.alert_txt.pack(side="left", fill="x", expand=True, pady=P(8), padx=(0, P(10)))
        self.alert.pack(fill="x")

        return pg

    def browse(self):
        d = filedialog.askdirectory(title="Carpeta del juego (la que tiene sce_sys)", mustexist=True,
                                    initialdir=os.path.dirname(self.folder_var.get()) or HOME)
        if d:
            self.pick(os.path.normpath(d))

    def pick(self, folder):
        if not folder or not os.path.isdir(folder):
            return self.set_alert("err", "Esa carpeta no existe.", show_card=False)
        app = find_app_folder(folder)
        if not app:
            return self.set_alert("err", "No parece un juego de PS5: falta sce_sys\\param.json en esa carpeta.", show_card=False)
        self.folder_var.set(app)
        try:
            g = game_info(app)
        except (OSError, ValueError) as e:
            return self.set_alert("err", "No se pudo leer param.json: %s" % e, show_card=False)
        g["folder"] = app
        self.current = g
        self.g_name.configure(text=g["name"])
        self.g_meta.configure(text="%s · v%s" % (g["tid"], g["version"]))
        self.set_icon(self.icon_lbl, g["icon"], 112)
        cfw = self.console_fw()
        self.g_stats[0][0].configure(text="Tamaño en el PC")
        self.g_stats[0][1].configure(text="calculando…")
        self.g_stats[1][0].configure(text="Firmware que pide")
        self.g_stats[1][1].configure(text=("%.2f" % g["fw"]) if g["fw"] else "—")
        self.g_stats[2][0].configure(text="Tu PS5")
        self.g_stats[2][1].configure(text="%.2f" % cfw)
        req, bp = g["fw"], g["nfo_fw"]
        if not req or req <= cfw:
            g["cfw_arg"], g["risky"] = cfw, False
            self.set_alert("ok", "Firmware compatible con tu PS5." if req else "El juego no indica firmware mínimo.")
        elif bp and bp <= cfw:
            g["cfw_arg"], g["risky"] = 99.99, False
            self.set_alert("warn", "Pide firmware %.2f, pero %s indica backport a FW %.2f: compatible con tu %.2f."
                           % (req, g["nfo"], bp, cfw))
        else:
            g["cfw_arg"], g["risky"] = 99.99, True
            self.set_alert("err", "Pide firmware %.2f y tu PS5 tiene %.2f. No veo un .nfo con backport: "
                                  "lo más probable es que no arranque." % (req, cfw))
        self.install_btn.set_enabled(True)

        def size():
            n, b = tree_size(app)
            self.post(lambda: self.current is g and self.g_stats[0][1].configure(text="%s archivos · %s" % (nfmt(n), human(b))))
        threading.Thread(target=size, daemon=True).start()

    def set_alert(self, level, text, show_card=True):
        if not show_card:
            self.current = None
            self.install_btn.set_enabled(False)
            self.card.pack_forget()
            self.card_empty.configure(text=text, fg=C["err"])
            self.card_empty.pack(fill="x", pady=(self.px(18), 0))
            return
        self.card_empty.pack_forget()
        self.card.pack(fill="x", pady=(self.px(18), 0))
        bg, line, fg, icon, ic = {"ok": ("okbg", "okbg", "okfg", "✓", "ok"), "warn": ("warnbg", "warnline", "warn", "▲", "warnicon"),
                                  "err": ("errbg", "errline", "err", "✕", "err")}[level]
        self.alert.configure(bg=C[bg], highlightbackground=C[line])
        self.alert_icon.configure(bg=C[bg], fg=C[ic], text=icon)
        self.alert_txt.configure(bg=C[bg], fg=C[fg], text=text)

    def set_icon(self, widget, path, size):
        P = self.px(size)
        img = None
        if path:
            key = (path, P)
            img = self.icons.get(key)
            if img is None:
                try:
                    raw = tk.PhotoImage(file=path)
                    f = max(1, round(max(raw.width(), raw.height()) / P))
                    img = raw.subsample(f, f) if f > 1 else raw
                    self.icons[key] = img
                except tk.TclError:
                    img = None
        if img:
            widget.configure(image=img, text="", width=P, height=P, bg=C["card"])
        else:
            blank = self.icons.get(("blank", P))
            if blank is None:
                blank = self.icons[("blank", P)] = tk.PhotoImage(width=P, height=P)
                blank.put(C["sel"], to=(0, 0, P, P))
            widget.configure(image=blank, text="sin icono" if size > 60 else "", compound="center",
                             fg=C["faint"], font=(FONT, 8), bg=C["card"], width=P, height=P)

    def console_fw(self):
        try:
            return float(str(self.cfg.get("fw", "12.60")).replace(",", "."))
        except ValueError:
            return 12.60

    # ------------------------------------------------------------ página: progreso

    STEPS = (("check", "Comprobar PS5 y kit"), ("analyze", "Analizar juego"), ("starter", "Preparar arranque"),
             ("upload", "Subir a la PS5"), ("link", "Escribir enlace"), ("kraken", "Ajustar Kraken Mount"))

    def _page_progress(self):
        P = self.px
        pg = frame(self.main)
        foot = frame(pg)
        foot.pack(side="bottom", fill="x", padx=P(28), pady=P(14))
        tk.Frame(pg, bg=C["line"], height=1).pack(side="bottom", fill="x", padx=P(28))
        self.p_cancel = Btn(foot, "Cancelar", self.cancel_install, "danger")
        self.p_cancel.pack(side="right")
        self.p_done = Btn(foot, "Ver mis juegos", lambda: self.show("games"), "primary")
        self.p_again = Btn(foot, "Instalar otro", lambda: self.show("install"))
        body = frame(pg)
        body.pack(fill="both", expand=True, padx=P(28), pady=(P(22), 0))
        head = frame(body)
        head.pack(fill="x")
        self.p_icon = tk.Label(head, bg=C["sel"], bd=0)
        self.p_icon.pack(side="left")
        ht = frame(head)
        ht.pack(side="left", padx=(P(14), 0))
        self.p_title = label(ht, "", 17, True)
        self.p_title.pack(fill="x")
        self.p_sub = label(ht, "", 10, fg="dim")
        self.p_sub.pack(fill="x")

        bar = frame(body, "card", border=True)
        bar.pack(fill="x", pady=(P(18), P(16)))
        top = frame(bar, "card")
        top.pack(fill="x", padx=P(18), pady=(P(14), P(8)))
        self.p_phase = label(top, "", 10, True, bg="card")
        self.p_phase.pack(side="left")
        self.p_pct = label(top, "", 17, True, bg="card")
        self.p_pct.pack(side="right")
        self.p_bar = ttk.Progressbar(bar, style="BM.Horizontal.TProgressbar", maximum=1000)
        self.p_bar.pack(fill="x", padx=P(18))
        det = frame(bar, "card")
        det.pack(fill="x", padx=P(18), pady=(P(8), P(14)))
        self.p_left = label(det, "", 9, fg="dim", bg="card")
        self.p_left.pack(side="left")
        self.p_right = label(det, "", 9, fg="dim", bg="card")
        self.p_right.pack(side="right")

        cols = frame(body)
        cols.pack(fill="both", expand=True)
        steps = frame(cols, "card", border=True, width=P(320))
        steps.pack(side="left", fill="y")
        steps.pack_propagate(False)
        self.p_steps = {}
        for key, text in self.STEPS:
            r = frame(steps, "card")
            r.pack(fill="x", padx=P(16), pady=(P(11), 0))
            ic = StepIcon(r, P(22))
            ic.pack(side="left", anchor="n", pady=(P(2), 0))
            tx = frame(r, "card")
            tx.pack(side="left", fill="x", expand=True, padx=(P(12), 0))
            t = label(tx, text, 10, True, bg="card")
            t.pack(fill="x")
            d = label(tx, "", 9, fg="dim", bg="card", wraplength=P(240))
            d.pack(fill="x")
            self.p_steps[key] = (ic, t, d)
        logbox = frame(cols, "log", border=True)
        logbox.pack(side="left", fill="both", expand=True, padx=(P(16), 0))
        lh = frame(logbox, "log")
        lh.pack(fill="x", padx=P(12), pady=(P(8), P(4)))
        label(lh, "Registro", 9, fg="dim", bg="log").pack(side="left")
        Btn(lh, "Copiar", self.copy_log, "link", bg=C["log"], padx=0, pady=0).pack(side="right")
        self.p_log = tk.Text(logbox, bg=C["log"], fg="#9AA4B5", font=(MONO, 9), relief="flat", wrap="word",
                             highlightthickness=0, padx=P(12), pady=P(6), state="disabled",
                             selectbackground=C["sel"], insertbackground=C["text"])
        self.p_log.tag_configure("hi", foreground=C["text"])
        self.p_log.tag_configure("err", foreground=C["err"])
        self.p_log.tag_configure("ok", foreground=C["okfg"])
        self.p_log.pack(fill="both", expand=True)

        return pg

    def log(self, text, tag=None):
        self.p_log.configure(state="normal")
        self.p_log.insert("end", text + "\n", tag or ())
        self.p_log.see("end")
        self.p_log.configure(state="disabled")

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.p_log.get("1.0", "end"))

    def step(self, key, state, detail=None):
        ic, t, d = self.p_steps[key]
        ic.set(state)
        if state == "run":
            self.cur_step = key
        if detail is not None:
            d.configure(text=detail, fg=C["accenth"] if state == "run" else C["err"] if state == "err" else C["dim"])
        t.configure(fg=C["faint"] if state == "wait" else C["text"])

    # ------------------------------------------------------------ instalación

    def install(self):
        g = self.current
        if not g or self.install_proc is not None:
            return
        if not self.kit:
            messagebox.showerror("HostMount", "No encuentro el kit de HostMount (WINDOWS-PATCH %s). "
                                 "Indícalo en Ajustes." % KIT_MARK)
            return self.show("settings")
        if g["risky"] and not messagebox.askyesno("Firmware", "El juego pide un firmware mayor que el de tu PS5 y no "
                                                  "veo un backport. ¿Instalar igualmente?"):
            return
        self.cfg["ampr_off"] = self.ampr_var.get()
        save_cfg(self.cfg)
        self.install_proc = "preparing"
        self.cur_step = None
        self.install_started = time.time()
        self.up_total = self.up_done = self.up_bytes = self.up_mib = 0
        self.up_hits = deque()
        self.p_title.configure(text="Instalando %s" % g["name"])
        self.p_sub.configure(text="%s → PS5 %s · no cierres esta ventana" % (g["tid"], self.cfg["ps5"]))
        self.set_icon(self.p_icon, g["icon"], 56)
        self.p_phase.configure(text="Comprobando…")
        self.p_pct.configure(text="")
        self.p_bar.configure(value=0)
        self.p_left.configure(text="")
        self.p_right.configure(text="")
        self.p_log.configure(state="normal")
        self.p_log.delete("1.0", "end")
        self.p_log.configure(state="disabled")
        for k, _ in self.STEPS:
            self.step(k, "wait", "")
        self.step("check", "run", "Conectando con %s…" % self.cfg["ps5"])
        for b in (self.p_done, self.p_again):
            b.pack_forget()
        self.p_cancel.pack(side="right")
        self.p_cancel.set_enabled(True)
        self.show("progress")
        threading.Thread(target=self._precheck, args=(g,), daemon=True).start()

    def _precheck(self, g):
        ps5 = self.cfg["ps5"]
        ip = local_ip(ps5)
        if not port_open(ps5, FTP_PORT, 4):
            return self.post(self.install_failed, "check", "La PS5 no responde por FTP en %s:%d. ¿Está encendida con etaHEN?" % (ps5, FTP_PORT))
        if not ip:
            return self.post(self.install_failed, "check", "No encuentro la IP de este PC en la red de la PS5.")
        found = []
        try:
            ftp = ftp_open(ps5)
            for base in (HOMEBREW_DIR, STAGE_DIR):
                found += [base + "/" + n for n in ftp_names(ftp, base) if n.endswith("(%s)" % g["tid"])]
            ftp.quit()
        except ftplib.all_errors as e:
            return self.post(self.install_failed, "check", "FTP: %s" % e)
        self.post(self._after_check, g, ip, found)

    def _after_check(self, g, ip, found):
        if found:
            msg = ("En la PS5 ya hay restos o una instalación anterior de este juego:\n\n%s\n\n"
                   "Para instalar hay que borrarlos (solo esas carpetas; tus partidas guardadas no se tocan). ¿Borrar y seguir?"
                   % "\n".join(found))
            if not messagebox.askyesno("Ya existe en la PS5", msg, icon="warning"):
                return self.install_failed("check", "Cancelado: ya existe en la PS5.")
        self.step("check", "ok", "FTP %d · FW %.2f · kit v%s" % (FTP_PORT, self.console_fw(), KIT_MARK))
        self.log("PS5 %s:%d OK · este PC %s" % (self.cfg["ps5"], FTP_PORT, ip))
        bp = " · backport FW %.2f (%s)" % (g["nfo_fw"], g["nfo"]) if g["nfo_fw"] and g["cfw_arg"] == 99.99 else ""
        self.step("analyze", "ok", "%s v%s%s" % (g["tid"], g["version"], bp))
        err = self.server.start(self.kit, self.serve_roots(extra=os.path.dirname(g["folder"])))
        if err:
            self.log("Servidor: " + err, "err")
        threading.Thread(target=self._run_installer, args=(g, ip, found), daemon=True).start()

    def _run_installer(self, g, ip, found):
        ps5 = self.cfg["ps5"]
        if found:
            try:
                ftp = ftp_open(ps5)
                for p in found:
                    self.post(self.log, "borrando %s" % p)
                    ftp_rmtree(ftp, p)
                ftp.quit()
            except ftplib.all_errors as e:
                return self.post(self.install_failed, "check", "No se pudo borrar la versión anterior: %s" % e)
        self.post(self.step, "starter", "run", "Copiando eboot y enlazando libSceAmpr…")
        self.post(self.p_phase.configure, {"text": "Preparando el arranque"})
        cfw = g["cfw_arg"]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                   BLAZINGMOUNT_FOLDER_DIR=os.path.dirname(g["folder"]), BLAZINGMOUNT_HOST_IP=ip,
                   BLAZINGMOUNT_MIN_CONSOLE_FW="%.2f" % cfw)
        cmd = [PY, "-u", "-c", RUNNER, os.path.join(self.kit, "install", "blazingmount-install.py"),
               "--console", ps5, "--host-ip", ip, "--game", g["folder"], "--console-fw", "%.2f" % cfw,
               "--ftp-port", str(FTP_PORT)]
        try:
            proc = subprocess.Popen(cmd, cwd=self.kit, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, creationflags=NOWINDOW, text=True,
                                    encoding="utf-8", errors="replace", bufsize=1)
        except OSError as e:
            return self.post(self.install_failed, "starter", "No se pudo lanzar el instalador: %s" % e)
        self.post(setattr, self, "install_proc", proc)
        tail = deque(maxlen=12)
        ok = False
        for line in proc.stdout:
            line = line.rstrip()
            if line.startswith("@@PUT "):
                _, size, remote = line.split(" ", 2)
                if remote.startswith(STAGE_DIR):
                    self.post(self.on_put, int(size), remote)
                continue
            tail.append(line)
            if line.startswith("DONE."):
                ok = True
            self.post(self.on_install_line, line)
        rc = proc.wait()
        self.post(self.on_install_end, g, ip, ok and rc == 0, rc, list(tail))

    def on_install_line(self, line):
        if not line:
            return
        low = line.lower()
        tag = "err" if ("error" in low or "failed" in low or "traceback" in low or "refusing" in low) else None
        if line.startswith("starter:"):
            self.step("starter", "ok", "Arranque listo")
        m = re.match(r"pushing (\d+) files \((\d+) MiB\) -> (.*)", line)
        if m:
            self.up_total, self.up_mib = int(m.group(1)), int(m.group(2))
            self.step("starter", "ok")
            self.step("upload", "run", HOMEBREW_DIR + "/" + m.group(3).rsplit("/", 1)[-1])
            self.p_phase.configure(text="Subiendo archivos a la PS5")
            self.update_progress()
            tag = "hi"
        if line.startswith("placed"):
            self.step("upload", "ok", "%s archivos en la PS5" % nfmt(self.up_total))
            self.step("link", "run", "%s/…cfg" % LINK_DIR)
            self.p_bar.configure(value=1000)
            self.p_pct.configure(text="100%")
            tag = "ok"
        if line.startswith("link config:"):
            m = re.search(r"(/data/\S+\.cfg)", line)
            self.step("link", "ok", m.group(1) if m else "")
        if line.startswith("DONE."):
            tag = "ok"
        self.log(line, tag)

    def on_put(self, size, remote):
        self.up_done += 1
        self.up_bytes += size
        self.up_hits.append((time.time(), size))
        if self.up_done % 5 == 0 or self.up_done == self.up_total:
            self.update_progress()
        if self.up_done % 50 == 0:
            self.log("subidos %d/%d · %s" % (self.up_done, self.up_total, remote.rsplit("/", 1)[-1]))

    def update_progress(self):
        if not self.up_total:
            return
        frac = self.up_done / float(self.up_total)
        self.p_bar.configure(value=int(frac * 1000))
        self.p_pct.configure(text="%d%%" % int(frac * 100))
        self.p_left.configure(text="%s / %s archivos · %s / %s" % (nfmt(self.up_done), nfmt(self.up_total),
                                                                  human(self.up_bytes), human(self.up_mib << 20)))
        now = time.time()
        while self.up_hits and self.up_hits[0][0] < now - 8:
            self.up_hits.popleft()
        if len(self.up_hits) > 1:
            span = max(0.5, now - self.up_hits[0][0])
            fps = len(self.up_hits) / span
            left = (self.up_total - self.up_done) / fps if fps else 0
            self.p_right.configure(text="%s/s · quedan ~%s" % (human(sum(s for _, s in self.up_hits) / span), self.eta(left)))

    @staticmethod
    def eta(sec):
        sec = int(sec)
        return "%d s" % sec if sec < 90 else "%d min" % round(sec / 60.0)

    def on_install_end(self, g, ip, ok, rc, tail):
        if self.install_proc == "cancelled":
            return
        if not ok:
            failed = getattr(self, "cur_step", None) or "starter"
            msg = next((l for l in reversed(tail) if l.strip()), "el instalador terminó con código %s" % rc)
            return self.install_failed(failed, msg)
        self.cfg["games"][g["tid"]] = {"name": g["name"], "folder": g["folder"], "version": g["version"],
                                       "installed": time.strftime("%Y-%m-%d %H:%M")}
        save_cfg(self.cfg)
        self.server.start(self.kit, self.serve_roots())
        if self.cfg.get("ampr_off", True):
            self.step("kraken", "run", "Esperando a que Kraken Mount vea %s…" % g["tid"])
            self.p_phase.configure(text="Ajustando Kraken Mount")

            def go():
                res = kraken_ampr_off(self.cfg["ps5"], g["tid"])
                self.post(self.finish, g, res)
            threading.Thread(target=go, daemon=True).start()
        else:
            self.step("kraken", "ok", "Sin cambios (desactivado en Instalar)")
            self.finish(g, (True, None))

    def finish(self, g, res):
        ok, msg = res
        if msg:
            self.step("kraken", "ok" if ok else "err", msg)
            self.log(msg, "ok" if ok else "err")
        self.install_proc = None
        self.p_phase.configure(text="Listo: búscalo en la pantalla de inicio de la PS5")
        self.p_bar.configure(value=1000)
        self.p_pct.configure(text="100%")
        self.p_sub.configure(text="%s · deja el servidor activo mientras juegas" % g["tid"])
        self.p_cancel.pack_forget()
        self.p_done.pack(side="right")
        self.p_again.configure(text="Instalar otro")
        self.p_again.pack(side="right", padx=(0, self.px(10)))

    def install_failed(self, key, msg):
        if self.install_proc not in (None, "cancelled"):
            self.install_proc = None
        self.step(key, "err", msg[:180])
        self.log(msg, "err")
        self.p_phase.configure(text="No se pudo instalar")
        self.p_cancel.pack_forget()
        self.p_again.configure(text="Volver")
        self.p_again.pack(side="right")
        self.install_proc = None

    def cancel_install(self):
        p = self.install_proc
        if not isinstance(p, subprocess.Popen):
            return
        if not messagebox.askyesno("Cancelar", "¿Cancelar la instalación? Lo subido a medias se borrará en el próximo intento."):
            return
        self.install_proc = "cancelled"
        p.terminate()
        self.log("Cancelado.", "err")
        self.p_phase.configure(text="Cancelado")
        self.p_cancel.pack_forget()
        self.p_again.configure(text="Volver")
        self.p_again.pack(side="right")
        self.root.after(500, lambda: setattr(self, "install_proc", None))

    # ------------------------------------------------------------ página: mis juegos

    def _page_games(self):
        P = self.px
        pg = frame(self.main)
        body = frame(pg)
        body.pack(fill="both", expand=True, padx=P(28), pady=P(22))
        head = frame(body)
        head.pack(fill="x")
        Btn(head, "+ Instalar otro", lambda: self.show("install"), "primary").pack(side="right", anchor="s")
        Btn(head, "Añadir uno ya instalado…", self.add_existing).pack(side="right", anchor="s", padx=(0, P(10)))
        ht = frame(head)
        ht.pack(side="left", fill="x", expand=True)
        label(ht, "Mis juegos en red", 18, True).pack(fill="x")
        label(ht, "Instalados en la PS5; leen sus datos desde este PC.", 10, fg="mute").pack(fill="x", pady=(P(4), 0))
        stats = frame(body)
        stats.pack(fill="x", pady=(P(16), P(14)))
        self.s_cards = []
        for i, t in enumerate(("Servidor", "Leyendo ahora", "Juegos en la lista")):
            b = frame(stats, "card", border=True)
            b.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else P(12), 0))
            stats.columnconfigure(i, weight=1, uniform="c")
            label(b, t, 9, fg="dim", bg="card").pack(fill="x", padx=P(14), pady=(P(12), 0))
            v = label(b, "—", 10, True, bg="card")
            v.pack(fill="x", padx=P(14), pady=(P(3), P(12)))
            self.s_cards.append(v)
        self.g_list = Scroll(body, C["bg"])
        self.g_list.pack(fill="both", expand=True)
        logbox = frame(body, "log", border=True)
        logbox.pack(fill="x", pady=(P(12), 0))
        label(logbox, "Servidor · últimas lecturas", 9, fg="dim", bg="log").pack(fill="x", padx=P(12), pady=(P(8), 0))
        self.srv_log = tk.Text(logbox, height=5, bg=C["log"], fg="#9AA4B5", font=(MONO, 9), relief="flat",
                               highlightthickness=0, padx=P(12), pady=P(6), state="disabled", wrap="none")
        self.srv_log.pack(fill="x")
        return pg

    def add_existing(self):
        d = filedialog.askdirectory(title="Carpeta del juego ya instalado en la PS5", mustexist=True)
        if not d:
            return
        app = find_app_folder(os.path.normpath(d))
        if not app:
            return messagebox.showerror("HostMount", "No encuentro sce_sys\\param.json en esa carpeta.")
        g = game_info(app)
        self.cfg["games"][g["tid"]] = {"name": g["name"], "folder": app, "version": g["version"], "installed": "antes"}
        save_cfg(self.cfg)
        if self.kit:
            err = self.server.start(self.kit, self.serve_roots())
            self.server_note = err
        self.refresh_games()

    def refresh_games(self):
        for w in self.g_list.inner.winfo_children():
            w.destroy()
        P = self.px
        games = self.cfg["games"]
        if not games:
            label(self.g_list.inner, "Aún no hay juegos. Instala uno o añade el que ya tengas en la PS5.", 10,
                  fg="faint").pack(fill="x", pady=P(10))
        for tid, g in sorted(games.items(), key=lambda kv: kv[1]["name"].lower()):
            row = frame(self.g_list.inner, "card", border=True)
            row.pack(fill="x", pady=(0, P(10)))
            ic = tk.Label(row, bg=C["sel"], bd=0)
            icon = os.path.join(g["folder"], "sce_sys", "icon0.png")
            self.set_icon(ic, icon if os.path.isfile(icon) else None, 56)
            ic.pack(side="left", padx=P(12), pady=P(12))
            Btn(row, "✕", lambda t=tid: self.remove_game(t), padx=P(10), pady=P(5)).pack(side="right", padx=(P(6), P(12)))
            Btn(row, "Reinstalar", lambda f=g["folder"]: (self.show("install"), self.pick(f)), pady=P(5)).pack(side="right")
            here = os.path.isdir(g["folder"])
            on_ps5 = None if self.ps5_games is None else any(n.endswith("(%s)" % tid) for n in self.ps5_games)
            if not here:
                txt, bg, fg = "Carpeta no encontrada", "errbg", "err"
            elif on_ps5 is False:
                txt, bg, fg = "No está en la PS5", "warnbg", "warn"
            elif on_ps5:
                txt, bg, fg = "Listo para jugar", "okbg", "okfg"
            else:
                txt, bg, fg = "En el PC", "btn", "mute"
            label(row, txt, 9, True, fg=fg, bg=bg, padx=P(10), pady=P(3)).pack(side="right", padx=P(12))
            tx = frame(row, "card")
            tx.pack(side="left", fill="x", expand=True)
            label(tx, g["name"], 11, True, bg="card").pack(fill="x")
            label(tx, "%s · %s" % (tid, g["folder"]), 9, fg="dim", bg="card").pack(fill="x")
        self.s_cards[2].configure(text="%d" % len(games))

        def scan():
            try:
                ftp = ftp_open(self.cfg["ps5"])
                names = ftp_names(ftp, HOMEBREW_DIR)
                ftp.quit()
            except (ftplib.all_errors, OSError):
                names = None
            if names != self.ps5_games:
                self.ps5_games = names
                self.post(self.refresh_games)
        if not getattr(self, "_scanning", False):
            self._scanning = True

            def run():
                try:
                    scan()
                finally:
                    self._scanning = False
            threading.Thread(target=run, daemon=True).start()

    def remove_game(self, tid):
        g = self.cfg["games"].get(tid)
        if not g:
            return
        ans = messagebox.askyesnocancel(
            "Quitar %s" % g["name"],
            "¿Borrarlo también de la PS5?\n\nSí: borra su carpeta en %s y el enlace %s/%s.cfg (cierra el juego antes). "
            "Las partidas guardadas no se tocan.\nNo: solo lo quita de esta lista.\n\nLos archivos de este PC no se borran nunca."
            % (HOMEBREW_DIR, LINK_DIR, tid), icon="warning")
        if ans is None:
            return
        if ans:
            def go():
                try:
                    ftp = ftp_open(self.cfg["ps5"])
                    for n in ftp_names(ftp, HOMEBREW_DIR):
                        if n.endswith("(%s)" % tid):
                            ftp_rmtree(ftp, HOMEBREW_DIR + "/" + n)
                    try:
                        ftp.delete("%s/%s.cfg" % (LINK_DIR, tid))
                    except ftplib.error_perm:
                        pass
                    ftp.quit()
                    self.post(self._removed, tid)
                except ftplib.all_errors as e:
                    self.post(messagebox.showerror, "HostMount", "No se pudo borrar de la PS5: %s" % e)
            threading.Thread(target=go, daemon=True).start()
        else:
            self._removed(tid)

    def _removed(self, tid):
        self.cfg["games"].pop(tid, None)
        save_cfg(self.cfg)
        self.ps5_games = None
        if self.kit and self.server.running():
            roots = self.serve_roots()
            if roots:
                self.server.start(self.kit, roots)
            else:
                self.server.stop()
        self.refresh_games()

    # ------------------------------------------------------------ página: ajustes

    def _page_settings(self):
        P = self.px
        pg = frame(self.main)
        body = frame(pg)
        body.pack(fill="both", expand=True, padx=P(28), pady=P(22))
        label(body, "Ajustes", 18, True).pack(fill="x")
        self.set_vars = {}
        for key, title, hint in (("ps5", "IP de la PS5", "La que muestra etaHEN; el FTP debe estar en el puerto %d." % FTP_PORT),
                                 ("fw", "Firmware de la PS5", "Se usa para saber si un juego necesita backport.")):
            label(body, title, 9, True, fg="mute").pack(fill="x", pady=(P(18), P(6)))
            v = tk.StringVar(value=self.cfg[key])
            tk.Entry(body, textvariable=v, font=(FONT, 10), bg=C["field"], fg=C["text"], insertbackground=C["text"],
                     relief="flat", highlightthickness=1, highlightbackground=C["btnline"],
                     highlightcolor=C["accent"], width=24).pack(anchor="w", ipady=P(7))
            label(body, hint, 9, fg="dim").pack(fill="x", pady=(P(4), 0))
            self.set_vars[key] = v
        label(body, "Kit de HostMount", 9, True, fg="mute").pack(fill="x", pady=(P(18), P(6)))
        kr = frame(body)
        kr.pack(fill="x")
        self.kit_lbl = label(kr, "buscando…", 10, fg="text")
        self.kit_lbl.pack(side="left")
        Btn(kr, "Elegir zip del kit…", self.choose_kit, pady=P(5)).pack(side="right")
        label(body, "Servidor: puerto %d · Kraken Mount: puerto %d · configuración en %s" % (SERVE_PORT, KRAKEN_PORT, CFG_PATH),
              9, fg="dim", wraplength=P(760)).pack(fill="x", pady=(P(18), 0))
        Btn(body, "Guardar", self.save_settings, "primary").pack(anchor="w", pady=(P(22), 0))
        return pg

    def save_settings(self):
        ps5 = self.set_vars["ps5"].get().strip()
        try:
            socket.inet_aton(ps5)
            float(self.set_vars["fw"].get().replace(",", "."))
        except (OSError, ValueError):
            return messagebox.showerror("Ajustes", "Revisa la IP y el firmware (por ejemplo 192.168.1.38 y 12.60).")
        self.cfg["ps5"], self.cfg["fw"] = ps5, self.set_vars["fw"].get().strip().replace(",", ".")
        save_cfg(self.cfg)
        self.ps5_games = None
        threading.Thread(target=self._probe, daemon=True).start()
        if self.current:
            self.pick(self.current["folder"])
        self.show("install")

    def choose_kit(self):
        z = filedialog.askopenfilename(title="Zip del kit de HostMount", filetypes=[("Zip", "*.zip")])
        if not z:
            return
        try:
            d = extract_kit(z, os.path.join(KIT_PARENT, "hostmount"))
        except (OSError, zipfile.BadZipFile) as e:
            return messagebox.showerror("Kit", "No se pudo abrir: %s" % e)
        if not d or not kit_ok(d):
            return messagebox.showerror("Kit", "Ese zip no es el kit para Windows (falta WINDOWS-PATCH %s)." % KIT_MARK)
        self.cfg["kit"] = d
        save_cfg(self.cfg)
        self.set_kit(d)

    def set_kit(self, d):
        self.kit = d
        self.kit_lbl.configure(text=d if d else "No encontrado: elige el zip del kit", fg=C["text"] if d else C["err"])

    # ------------------------------------------------------------ estado general

    def _boot(self):
        kit = find_kit(self.cfg)
        self.post(self.set_kit, kit)
        if kit and self.cfg["games"]:
            self.post(self.start_server_quiet)
        self._probe()

    def start_server_quiet(self):
        self.server_note = self.server.start(self.kit, self.serve_roots())
        self.update_server_box()

    def _probe(self):
        ps5 = self.cfg["ps5"]
        res = {"ip": local_ip(ps5), "ftp": port_open(ps5, FTP_PORT), "kraken": port_open(ps5, KRAKEN_PORT)}
        self.post(self.set_chips, res)

    def set_chips(self, r):
        (d, l) = self.chip["pc"]
        d.color(C["ok"] if r["ip"] else C["err"])
        l.configure(text="PC " + (r["ip"] or "sin red"))
        (d, l) = self.chip["ps5"]
        d.color(C["ok"] if r["ftp"] else C["err"])
        l.configure(text="PS5 %s · %s" % (self.cfg["ps5"], "FTP OK" if r["ftp"] else "sin FTP"))
        (d, l) = self.chip["fw"]
        d.color(C["dim"])
        l.configure(text="Firmware %s" % self.cfg["fw"])
        (d, l) = self.chip["kraken"]
        d.color(C["ok"] if r["kraken"] else C["warnicon"])
        l.configure(text="Kraken Mount " + ("activo" if r["kraken"] else "no responde"))

    def serve_roots(self, extra=None):
        roots = [os.path.dirname(g["folder"]) for g in self.cfg["games"].values()]
        if extra:
            roots.append(extra)
        return roots

    def toggle_server(self):
        if self.server.running():
            if messagebox.askyesno("Servidor", "Si lo detienes, los juegos en red dejarán de cargar. ¿Detener?"):
                self.server.stop()
                self.server_note = None
        else:
            if not self.kit:
                return messagebox.showerror("HostMount", "Falta el kit: elígelo en Ajustes.")
            self.server_note = self.server.start(self.kit, self.serve_roots())
            if self.server_note:
                messagebox.showwarning("Servidor", self.server_note)
        self.update_server_box()

    def on_server_line(self, line):
        self.srv_log.configure(state="normal")
        self.srv_log.insert("end", line + "\n")
        if int(self.srv_log.index("end-1c").split(".")[0]) > 300:
            self.srv_log.delete("1.0", "100.0")
        self.srv_log.see("end")
        self.srv_log.configure(state="disabled")

    def on_server_exit(self, proc):
        if proc is self.server.proc or self.server.proc is None:
            self.on_server_line("— servidor detenido (código %s) —" % proc.returncode)
        self.update_server_box()

    def update_server_box(self):
        if self.server.running():
            rate, tid = self.server.rate()
            self.srv_dot.color(C["ok"])
            self.srv_title.configure(text="Servidor activo")
            self.srv_sub.configure(text="Puerto %d · %s lecturas" % (SERVE_PORT, nfmt(self.server.reqs)))
            self.srv_btn.configure(text="Detener")
            self.s_cards[0].configure(text="Activo · %s:%d" % (self.chip["pc"][1].cget("text")[3:], SERVE_PORT))
            name = self.cfg["games"].get(tid, {}).get("name", tid) if tid else None
            self.s_cards[1].configure(text=("%s · %s/s" % (name, human(rate))) if name else "Nada ahora mismo")
        else:
            other = self.server_note and "puerto" in self.server_note
            self.srv_dot.color(C["warnicon"] if other else C["faint"])
            self.srv_title.configure(text="Otro servidor activo" if other else "Servidor parado")
            self.srv_sub.configure(text="Cierra la ventana de PowerShell que usa el %d" % SERVE_PORT if other
                                   else "Puerto %d" % SERVE_PORT)
            self.srv_btn.configure(text="Iniciar")
            self.s_cards[0].configure(text="Parado")
            self.s_cards[1].configure(text="—")

    def _tick(self):
        self.update_server_box()
        if isinstance(self.install_proc, subprocess.Popen) and self.up_total:
            self.update_progress()
        self._ticks = getattr(self, "_ticks", 0) + 1
        if self._ticks % 15 == 0:
            threading.Thread(target=self._probe, daemon=True).start()
        self.root.after(1000, self._tick)

    def on_close(self):
        if self.install_proc is not None:
            if not messagebox.askyesno("Cerrar", "Hay una instalación en curso. ¿Cancelarla y cerrar?"):
                return
            if isinstance(self.install_proc, subprocess.Popen):
                self.install_proc.terminate()
        elif self.server.running() and not messagebox.askyesno(
                "Cerrar HostMount", "Al cerrar se detiene el servidor y los juegos en red dejarán de cargar datos.\n\n"
                                       "Si vas a jugar, minimiza la ventana en vez de cerrarla. ¿Cerrar?"):
            return
        self.server.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
