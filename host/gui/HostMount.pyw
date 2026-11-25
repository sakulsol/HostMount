#!/usr/bin/env python3
"""HostMount: instala en la PS5 juegos que se leen por red desde este PC (Windows).

Lanza el instalador (install/blazingmount-install.py) y el servidor (host/serve/dirserve.py) de este
mismo kit, basado en BlazingMount; no cambia cómo funcionan, solo los muestra de forma legible.
Ventana compacta para instalar; maximizada muestra la biblioteca de juegos.

Copyright (C) 2026 sakulsol. GPL-3.0-or-later, como BlazingMount.
"""
import os
import sys


def _child_mode():
    """HostMount.exe hace también de intérprete para los scripts del kit (no hace falta Python).

    `HostMount.exe [-I] [-u] script.py args...` ejecuta script.py; `HostMount.exe -c CODE args...` ejecuta
    CODE. Así el exe puede lanzar dirserve, el instalador y el mkgame que este llama con sys.executable.
    """
    args = sys.argv[1:]
    while args and args[0] in ("-I", "-u", "-B"):
        args = args[1:]
    if not args or not (args[0] == "-c" or args[0].lower().endswith(".py")):
        return False
    if sys.stdout is None:                       # exe sin consola: usar los handles que dio el padre
        try:
            import ctypes
            import msvcrt
            for n, name in ((-11, "stdout"), (-12, "stderr")):
                h = ctypes.windll.kernel32.GetStdHandle(n)
                if h and h != -1:
                    f = os.fdopen(msvcrt.open_osfhandle(h, 0), "w", encoding="utf-8", errors="replace", buffering=1)
                else:
                    f = open(os.devnull, "w")
                setattr(sys, name, f)
        except Exception:
            sys.stdout = sys.stderr = open(os.devnull, "w")
    import runpy
    if args[0] == "-c":
        sys.argv = ["-c"] + args[2:]
        exec(compile(args[1], "<hostmount>", "exec"), {"__name__": "__main__"})
    else:
        sys.argv = args
        sys.path.insert(0, os.path.dirname(os.path.abspath(args[0])))
        runpy.run_path(args[0], run_name="__main__")
    return True


if getattr(sys, "frozen", False) and __name__ == "__main__":
    try:
        if _child_mode():
            sys.exit(0)
    except SystemExit:
        raise
    except BaseException:
        import traceback
        traceback.print_exc()
        sys.exit(1)

import ftplib
import glob
import json
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import deque

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

WIN = sys.platform == "win32"
FROZEN = getattr(sys, "frozen", False)
HOME = os.path.expanduser("~")
CFG_DIR = os.path.join(os.environ.get("APPDATA", HOME), "HostMount")
HERE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
CFG_PATH = os.path.join(CFG_DIR, "config.json")
KIT_PARENT = os.path.join(HOME, "hostmount-kit")
OLD_KIT = os.path.join(HOME, "blazingmount-kit", "blazingmount")
KIT_MARK = "3"                       # WINDOWS-PATCH del kit compilado para Windows
SERVE_PORT, FTP_PORT, KRAKEN_PORT = 4452, 2121, 8097
STAGE_DIR, HOMEBREW_DIR, LINK_DIR = "/data/blz-stage/incoming", "/data/homebrew", "/data/blazing/link"
NOWINDOW = 0x08000000 if WIN else 0
TID_RX = re.compile(r"\b([A-Z]{4}\d{5})\b")
COMPACT = (440, 620)                 # ventana normal; maximizada muestra además la biblioteca
WIDE_FROM = 860

C = dict(bg="#111419", panel="#0F1216", card="#151920", cardline="#1F242D", line="#1B1F26", field="#0C0F13",
         fieldline="#262B35", btn="#181C23", btnh="#20252E", text="#E8EBF1", mute="#8D96A5", dim="#6B7483",
         soft="#B5BDCA", accent="#4F7CFF", accenth="#6A90FF", accent2="#7A5BFF", link="#8FA9FF", ink="#FFFFFF",
         ok="#3DDC97", okbg="#11291F", okfg="#5BE3A7", warnbg="#2A2210", warn="#F3C46B", errbg="#2A1416",
         errline="#3A2226", err="#FF9C9C", log="#0C0F13", logfg="#7F8999", sel="#1C2230", trough="#1B2029",
         cover="#1C2230")
FONT = "Segoe UI" if WIN else "DejaVu Sans"
FONT_B = "Segoe UI Semibold" if WIN else "DejaVu Sans"
MONO = "Consolas" if WIN else "DejaVu Sans Mono"


def python_exe():
    """Con qué lanzar los scripts del kit: el propio exe, o python.exe aunque esto corra con pythonw.exe."""
    exe = sys.executable
    if not FROZEN and exe.lower().endswith("pythonw.exe"):
        alt = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.exists(alt):
            return alt
    return exe


PY = python_exe()

# Lanza el instalador del kit avisando de cada archivo subido (@@PUT), para la barra de progreso.
# Los subprocesos que lance (mkgame) escriben en la misma salida.
RUNNER = r'''
import importlib.util, os, sys
path = sys.argv[1]
import subprocess
_run = subprocess.run
def run(cmd, *a, **k):
    sys.stdout.flush()
    k.setdefault("stdout", sys.stdout)
    k.setdefault("stderr", subprocess.STDOUT)
    return _run(cmd, *a, **k)
subprocess.run = run
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


def local_ip(ps5, override=None):
    """IP de este PC en la red de la PS5. Prefiere una IP de la misma subred que la PS5: con una VPN
    (Tailscale, por ejemplo) la ruta hacia la PS5 puede salir por la VPN, y la PS5 no llega a esa IP."""
    if override:
        return override
    ips = set()
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(ai[4][0])
    except OSError:
        pass
    route = None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ps5, 9))
        route = s.getsockname()[0]
        ips.add(route)
    except OSError:
        pass
    finally:
        s.close()
    net = ps5.rsplit(".", 1)[0] + "."
    same = sorted(ip for ip in ips if ip.startswith(net) and ip != ps5)
    if route in same:
        return route
    return same[0] if same else route


# Motivos conocidos por los que el instalador se niega, en palabras claras.
REFUSALS = (
    ("does not load libSceAmpr", "Este juego no se puede jugar por red: su eboot no usa libSceAmpr, que es lo que "
                                 "permite leer los datos desde el PC. Instálalo de la forma normal."),
    ("needs FW", "El juego pide un firmware mayor que el de tu PS5 y no tiene backport."),
    ("already exist", "Ya existe en la PS5: quítalo desde la biblioteca y vuelve a instalarlo."),
)


def explain_failure(lines, rc):
    """El motivo útil de un fallo del instalador: la línea «refusing»/«ERROR», no el traceback."""
    key = next((l for l in lines if l.lower().startswith(("refusing", "error", "verify failed"))), None)
    key = key or next((l for l in reversed(lines) if l.strip() and not l.startswith((" ", "Traceback", "subprocess."))), None)
    key = key or "el instalador terminó con código %s" % rc
    for needle, text in REFUSALS:
        if needle.lower() in key.lower():
            return text
    return key


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
    STY = {"primary": ("accent", "accenth", "ink", None), "secondary": ("btn", "btnh", "text", "fieldline"),
           "danger": ("errbg", "errline", "err", "errline"), "link": (None, None, "link", None)}

    def __init__(self, parent, text, cmd, kind="secondary", bg=None, size=10, **kw):
        base, hover, fg, line = self.STY[kind]
        self.base = C[base] if base else (bg or C["bg"])
        self.hover = C[hover] if hover else self.base
        self.cmd, self.enabled, self.fg = cmd, True, C[fg]
        bold = kind == "primary"
        super().__init__(parent, text=text, bg=self.base, fg=self.fg, cursor="hand2",
                         font=(FONT_B, size + (1 if bold else 0), "bold") if bold else (FONT, size),
                         padx=kw.pop("padx", 14), pady=kw.pop("pady", 7), **kw)
        if line:
            self.configure(highlightthickness=1, highlightbackground=C[line], highlightcolor=C[line])
        self.bind("<Enter>", lambda e: self.enabled and self.configure(bg=self.hover))
        self.bind("<Leave>", lambda e: self.configure(bg=self.base))
        self.bind("<Button-1>", lambda e: self.enabled and self.cmd and self.cmd())

    def set_enabled(self, on):
        self.enabled = on
        self.configure(fg=self.fg if on else C["dim"], cursor="hand2" if on else "arrow",
                       bg=self.base if on else C["btn"])


def label(parent, text="", size=10, bold=False, fg="text", bg="bg", **kw):
    return tk.Label(parent, text=text, font=(FONT_B, size, "bold") if bold else (FONT, size),
                    fg=C.get(fg, fg), bg=C.get(bg, bg), anchor=kw.pop("anchor", "w"),
                    justify=kw.pop("justify", "left"), **kw)


def frame(parent, bg="bg", border=None, **kw):
    f = tk.Frame(parent, bg=C.get(bg, bg), **kw)
    if border:
        f.configure(highlightthickness=1, highlightbackground=C[border], highlightcolor=C[border])
    return f


class Dot(tk.Canvas):
    def __init__(self, parent, color, bg, size):
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0)
        self.o = self.create_oval(0, 0, size - 1, size - 1, fill=color, outline=color)

    def color(self, c):
        self.itemconfigure(self.o, fill=c, outline=c)


class Pill(tk.Frame):
    """Píldora de estado: punto de color + texto."""

    def __init__(self, parent, app, text="…", dot=True):
        super().__init__(parent, bg=C["btn"], highlightthickness=1, highlightbackground=C["fieldline"])
        self.dot = Dot(self, C["dim"], C["btn"], app.px(6)) if dot else None
        if self.dot:
            self.dot.pack(side="left", padx=(app.px(9), 0))
        self.lbl = label(self, text, 8, fg="soft", bg="btn")
        self.lbl.pack(side="left", padx=(app.px(6) if dot else app.px(9), app.px(9)), pady=app.px(3))

    def set(self, text, color=None):
        self.lbl.configure(text=text)
        if self.dot and color:
            self.dot.color(C.get(color, color))


class Badge(tk.Label):
    KINDS = {"ok": ("okbg", "okfg"), "warn": ("warnbg", "warn"), "err": ("errbg", "err"), "off": ("btn", "mute"),
             "info": ("sel", "link")}

    def __init__(self, parent, app, text="", kind="ok", bg="bg"):
        super().__init__(parent, font=(FONT_B, 8, "bold"), padx=app.px(7), pady=app.px(2))
        self.set(text, kind)

    def set(self, text, kind="ok"):
        b, f = self.KINDS[kind]
        self.configure(text=text, bg=C[b], fg=C[f])


class Bar(tk.Canvas):
    """Barra de progreso fina con degradado aproximado (dos tramos)."""

    def __init__(self, parent, app, bg="card"):
        super().__init__(parent, height=app.px(6), bg=C[bg], highlightthickness=0)
        self.v = 0.0
        self.bind("<Configure>", lambda e: self.draw())

    def set(self, v):
        self.v = max(0.0, min(1.0, v))
        self.draw()

    def draw(self):
        w, h = self.winfo_width(), int(self["height"])
        self.delete("all")
        self.create_rectangle(0, 0, w, h, fill=C["trough"], outline=C["trough"])
        x = int(w * self.v)
        if x > 0:
            self.create_rectangle(0, 0, x, h, fill=C["accent"], outline=C["accent"])
            self.create_rectangle(int(x * .6), 0, x, h, fill="#6468FF", outline="#6468FF")
            self.create_rectangle(int(x * .85), 0, x, h, fill=C["accent2"], outline=C["accent2"])


class StepIcon(tk.Canvas):
    def __init__(self, parent, s, bg):
        super().__init__(parent, width=s, height=s, bg=bg, highlightthickness=0)
        self.s = s
        self.set("wait")

    def set(self, state):
        s, d = self.s, max(1, self.s // 9)
        self.delete("all")
        if state == "ok":
            self.create_line(s * .18, s * .55, s * .42, s * .78, s * .85, s * .25, fill=C["okfg"], width=d + 1, capstyle="round")
        elif state == "run":
            self.create_oval(s * .2, s * .2, s * .8, s * .8, fill=C["accent"], outline=C["accent"])
        elif state == "err":
            self.create_text(s / 2, s / 2, text="✕", fill=C["err"], font=(FONT_B, 9, "bold"))
        else:
            self.create_oval(s * .22, s * .22, s * .78, s * .78, outline="#3A404C", width=d)


class Check(tk.Frame):
    def __init__(self, parent, text, var, s, bg="bg"):
        super().__init__(parent, bg=C[bg], cursor="hand2")
        self.var, self.s, self.bgc = var, s, C[bg]
        self.cv = tk.Canvas(self, width=s, height=s, bg=self.bgc, highlightthickness=0)
        self.cv.pack(side="left")
        lb = label(self, text, 9, fg="soft", bg=bg)
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
            cv.create_line(s * .22, s * .52, s * .42, s * .72, s * .78, s * .3, fill="#FFFFFF", width=max(2, s // 8))
        else:
            cv.create_rectangle(1, 1, s - 2, s - 2, outline=C["fieldline"], width=max(1, s // 12))


class Scroll(tk.Frame):
    """Marco con barra de desplazamiento vertical que solo aparece si hace falta."""

    def __init__(self, parent, bg):
        super().__init__(parent, bg=bg)
        self.cv = tk.Canvas(self, bg=bg, highlightthickness=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview, style="HM.Vertical.TScrollbar")
        self.inner = tk.Frame(self.cv, bg=bg)
        self.win = self.cv.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._fit)
        self.cv.bind("<Configure>", lambda e: (self.cv.itemconfigure(self.win, width=e.width), self._fit()))
        self.cv.configure(yscrollcommand=self.sb.set)
        self.cv.pack(side="left", fill="both", expand=True)
        self.cv.bind_all("<MouseWheel>", self._wheel, add="+")
        self.cv.bind_all("<Button-4>", lambda e: self._wheel(e, -120), add="+")
        self.cv.bind_all("<Button-5>", lambda e: self._wheel(e, 120), add="+")

    def _fit(self, e=None):
        self.cv.configure(scrollregion=self.cv.bbox("all"))
        need = self.inner.winfo_reqheight() > self.cv.winfo_height() > 1
        if need and not self.sb.winfo_ismapped():
            self.sb.pack(side="right", fill="y", before=self.cv)
        elif not need and self.sb.winfo_ismapped():
            self.sb.pack_forget()
            self.cv.yview_moveto(0)

    def _wheel(self, e, delta=None):
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None and w is not self:
            w = w.master
        if w is self and self.sb.winfo_ismapped():
            self.cv.yview_scroll(int(-(delta if delta is not None else e.delta) / 120), "units")


# ---------------------------------------------------------------- aplicación

class App:
    STEPS = (("check", "Comprobar PS5 y analizar juego"), ("starter", "Preparar arranque"),
             ("upload", "Subir a la PS5"), ("link", "Escribir enlace en la PS5"), ("kraken", "Ajustar Kraken Mount"))

    def __init__(self):
        if WIN:
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("sakulsol.HostMount")
            except Exception:
                pass
        self.root = tk.Tk()
        self.S = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        self.root.title("HostMount")
        self.root.configure(bg=C["bg"])
        ico = next((p for p in (os.path.join(getattr(sys, "_MEIPASS", HERE), "HostMount.ico"),
                                os.path.join(HERE, "HostMount.ico")) if os.path.isfile(p)), None)
        if WIN and ico:
            try:
                self.root.iconbitmap(ico)
            except tk.TclError:
                pass
        w, h = self.px(COMPACT[0]), self.px(COMPACT[1])
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        h = min(h, sh - self.px(80))
        self.root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, max(0, (sh - h) // 2 - self.px(20))))
        self.root.minsize(self.px(400), self.px(540))
        self.q = queue.Queue()
        self.cfg = load_cfg()
        self.kit = None
        self.server = Server(self)
        self.server_note = None
        self.install_proc = None
        self.current = None          # juego elegido para instalar
        self.selected = None         # juego elegido en la biblioteca
        self.icons = {}
        self.ps5_games = None        # carpetas en /data/homebrew, o None si no se pudo leer
        self.pc_ip = None
        self.wide = False
        self.page = None
        self._lib_w = 0
        self._style()
        self._build()
        self.root.bind("<Configure>", self._on_resize)
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
        st.configure("HM.Vertical.TScrollbar", troughcolor=C["bg"], background=C["btn"], bordercolor=C["bg"],
                     arrowcolor=C["dim"], lightcolor=C["btn"], darkcolor=C["btn"], gripcount=0)

    # ------------------------------------------------------------ esqueleto: biblioteca | panel

    def _build(self):
        self.body = frame(self.root)
        self.body.pack(fill="both", expand=True)
        self.lib = self._build_library(self.body)
        self.sep = tk.Frame(self.body, bg=C["line"], width=1)
        self.side = frame(self.body, "bg", width=self.px(COMPACT[0]))
        self.side.pack(side="right", fill="both", expand=True)
        self.pages = {"install": self._page_install(), "progress": self._page_progress(), "done": self._page_done(),
                      "game": self._page_game(), "settings": self._page_settings()}
        self.show("install")

    def _on_resize(self, e):
        if e.widget is not self.root:
            return
        wide = e.width >= self.px(WIDE_FROM)
        if wide != self.wide:
            self.wide = wide
            if wide:
                self.side.pack_forget()
                self.side.configure(width=self.px(COMPACT[0]))
                self.side.pack(side="right", fill="y")
                self.side.pack_propagate(False)
                self.sep.pack(side="right", fill="y")
                self.lib.pack(side="left", fill="both", expand=True)
                self.refresh_library()
            else:
                self.lib.pack_forget()
                self.sep.pack_forget()
                self.side.pack_forget()
                self.side.pack_propagate(True)
                self.side.pack(side="right", fill="both", expand=True)
            self.lib_link.configure(text="" if wide else "Biblioteca · %d  ↗" % len(self.cfg["games"]))
        if wide and abs(self.lib.winfo_width() - self._lib_w) > self.px(40):
            self.root.after_cancel(getattr(self, "_rs", "")) if getattr(self, "_rs", None) else None
            self._rs = self.root.after(150, self.refresh_library)

    def maximize(self):
        try:
            if WIN:
                self.root.state("zoomed")
            else:
                self.root.attributes("-zoomed", True)
        except tk.TclError:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            self.root.geometry("%dx%d+0+0" % (sw, sh - self.px(60)))

    def show(self, key):
        if key == "install" and isinstance(self.install_proc, (subprocess.Popen, str)) and self.install_proc != "cancelled":
            key = "progress"
        self.page = key
        for k, p in self.pages.items():
            p.place_forget() if k != key else p.place(relx=0, rely=0, relwidth=1, relheight=1)
        if key == "game":
            self.fill_game()

    def pad(self, parent):
        P = self.px
        f = frame(parent)
        f.pack(fill="both", expand=True, padx=P(18), pady=P(16))
        return f

    # ------------------------------------------------------------ página: instalar

    def _page_install(self):
        P = self.px
        pg = frame(self.side)
        b = self.pad(pg)
        st = frame(b)
        st.pack(fill="x")
        self.pill_ps5 = Pill(st, self, "PS5 …")
        self.pill_ps5.pack(side="left", padx=(0, P(6)))
        self.pill_srv = Pill(st, self, "Servidor …")
        self.pill_srv.pack(side="left", padx=(0, P(6)))
        self.pill_fw = Pill(st, self, "FW " + self.cfg["fw"], dot=False)
        self.pill_fw.pack(side="left")
        label(b, "Instalar un juego", 15, True).pack(fill="x", pady=(P(14), P(3)))
        self.intro = label(b, "Se copian a la PS5 solo los archivos pequeños; el resto se lee de este PC al jugar.",
                           9, fg="mute", wraplength=P(390))
        self.intro.pack(fill="x")
        row = frame(b)
        row.pack(fill="x", pady=(P(14), 0))
        self.folder_var = tk.StringVar()
        ent = tk.Entry(row, textvariable=self.folder_var, font=(FONT, 9), bg=C["field"], fg=C["soft"],
                       insertbackground=C["text"], relief="flat", highlightthickness=1,
                       highlightbackground=C["fieldline"], highlightcolor=C["accent"])
        ent.pack(side="left", fill="x", expand=True, ipady=P(7), padx=(0, P(8)))
        ent.bind("<Return>", lambda e: self.pick(self.folder_var.get().strip()))
        Btn(row, "Buscar", self.browse, pady=P(6)).pack(side="left")
        # tarjeta del juego
        self.gcard = frame(b, "card", "cardline")
        inner = frame(self.gcard, "card")
        inner.pack(fill="x", padx=P(14), pady=P(14))
        self.g_cover = tk.Label(inner, bg=C["card"], bd=0)
        self.g_cover.pack(side="left", anchor="n")
        info = frame(inner, "card")
        info.pack(side="left", fill="both", expand=True, padx=(P(14), 0))
        self.g_name = label(info, "", 11, True, bg="card", wraplength=P(250))
        self.g_name.pack(fill="x")
        self.g_meta = label(info, "", 8, fg="mute", bg="card")
        self.g_meta.pack(fill="x", pady=(P(3), P(6)))
        bad = frame(info, "card")
        bad.pack(fill="x")
        self.g_b1 = Badge(bad, self)
        self.g_b1.pack(side="left", padx=(0, P(6)))
        self.g_b2 = Badge(bad, self)
        self.g_b2.pack(side="left")
        self.g_note = label(info, "", 8, fg="mute", bg="card", wraplength=P(250))
        self.g_note.pack(fill="x", pady=(P(6), 0))
        self.g_empty = label(b, "Elige la carpeta del juego (la que tiene sce_sys).", 9, fg="dim")
        self.g_empty.pack(fill="x", pady=(P(14), 0))
        self.ampr_var = tk.BooleanVar(value=self.cfg.get("ampr_off", True))
        # pie
        foot = frame(b)
        foot.pack(side="bottom", fill="x")
        self.lib_link = Btn(foot, "Biblioteca · %d  ↗" % len(self.cfg["games"]), self.maximize, "link", padx=0, pady=0, size=9)
        self.lib_link.pack(side="left")
        Btn(foot, "Ajustes", lambda: self.show("settings"), "link", padx=0, pady=0, size=9).pack(side="right")
        self.install_btn = Btn(b, "Instalar en la PS5", self.install, "primary", pady=P(11))
        self.install_btn.pack(side="bottom", fill="x", pady=(0, P(12)))
        self.install_btn.set_enabled(False)
        self.ampr_chk = Check(b, "Desactivar ampr-emu-ng en Kraken al terminar", self.ampr_var, P(15))
        self.ampr_chk.pack(side="bottom", fill="x", pady=(0, P(14)))
        return pg

    def browse(self):
        d = filedialog.askdirectory(title="Carpeta del juego (la que tiene sce_sys)", mustexist=True,
                                    initialdir=os.path.dirname(self.folder_var.get()) or HOME)
        if d:
            self.pick(os.path.normpath(d))

    def pick(self, folder):
        if not folder or not os.path.isdir(folder):
            return self.pick_error("Esa carpeta no existe.")
        app = find_app_folder(folder)
        if not app:
            return self.pick_error("No parece un juego de PS5: falta sce_sys\\param.json.")
        self.folder_var.set(app)
        try:
            g = game_info(app)
        except (OSError, ValueError) as e:
            return self.pick_error("No se pudo leer param.json: %s" % e)
        g["folder"] = app
        self.current = g
        self.g_empty.pack_forget()
        self.gcard.pack(fill="x", pady=(self.px(14), 0))
        self.g_name.configure(text=g["name"])
        self.g_meta.configure(text="%s · v%s · calculando…" % (g["tid"], g["version"]))
        self.set_icon(self.g_cover, g["icon"], 84)
        cfw = self.console_fw()
        req, bp = g["fw"], g["nfo_fw"]
        if not req or req <= cfw:
            g["cfw_arg"], g["risky"] = cfw, False
            self.g_b1.set("FW %.2f" % req if req else "Sin FW mínimo", "off")
            self.g_b2.set("✓ Compatible", "ok")
            note = ""
        elif bp and bp <= cfw:
            g["cfw_arg"], g["risky"] = 99.99, False
            self.g_b1.set("FW %.2f → backport %.2f" % (req, bp), "warn")
            self.g_b2.set("✓ Compatible", "ok")
            note = "Backport indicado en %s." % g["nfo"]
        else:
            g["cfw_arg"], g["risky"] = 99.99, True
            self.g_b1.set("Pide FW %.2f" % req, "err")
            self.g_b2.set("Sin backport", "err")
            note = "Tu PS5 tiene %.2f: lo más probable es que no arranque." % cfw
        self.g_note.configure(text=note)
        self.g_note.pack_forget() if not note else self.g_note.pack(fill="x", pady=(self.px(6), 0))
        self.install_btn.set_enabled(True)

        def size():
            n, b = tree_size(app)
            self.post(lambda: self.current is g and self.g_meta.configure(
                text="%s · v%s · %s" % (g["tid"], g["version"], human(b))))
        threading.Thread(target=size, daemon=True).start()

    def pick_error(self, text):
        self.current = None
        self.install_btn.set_enabled(False)
        self.gcard.pack_forget()
        self.g_empty.configure(text=text, fg=C["err"])
        self.g_empty.pack(fill="x", pady=(self.px(14), 0))

    def set_icon(self, widget, path, size, bg="card"):
        P = self.px(size)
        img = None
        if path:
            key = (path, P)
            img = self.icons.get(key)
            if img is None:
                try:
                    raw = tk.PhotoImage(file=path)
                    f = max(1, -(-max(raw.width(), raw.height()) // P))
                    img = raw.subsample(f, f) if f > 1 else raw
                    self.icons[key] = img
                except tk.TclError:
                    img = None
        if img is None:
            img = self.icons.get(("blank", P))
            if img is None:
                img = self.icons[("blank", P)] = tk.PhotoImage(width=P, height=P)
                img.put(C["cover"], to=(0, 0, P, P))
        widget.configure(image=img, width=img.width(), height=img.height(), bg=C.get(bg, bg))
        return img

    def console_fw(self):
        try:
            return float(str(self.cfg.get("fw", "12.60")).replace(",", "."))
        except ValueError:
            return 12.60

    # ------------------------------------------------------------ página: instalando

    def _page_progress(self):
        P = self.px
        pg = frame(self.side)
        b = self.pad(pg)
        head = frame(b)
        head.pack(fill="x")
        self.p_cover = tk.Label(head, bg=C["bg"], bd=0)
        self.p_cover.pack(side="left")
        ht = frame(head)
        ht.pack(side="left", fill="x", expand=True, padx=(P(12), 0))
        self.p_name = label(ht, "", 11, True, wraplength=P(300))
        self.p_name.pack(fill="x")
        self.p_sub = label(ht, "", 8, fg="mute")
        self.p_sub.pack(fill="x", pady=(P(2), 0))
        card = frame(b, "card", "cardline")
        card.pack(fill="x", pady=(P(14), P(12)))
        top = frame(card, "card")
        top.pack(fill="x", padx=P(14), pady=(P(12), P(8)))
        self.p_phase = label(top, "", 9, True, bg="card")
        self.p_phase.pack(side="left")
        self.p_pct = label(top, "", 16, True, bg="card")
        self.p_pct.pack(side="right")
        self.p_bar = Bar(card, self)
        self.p_bar.pack(fill="x", padx=P(14))
        det = frame(card, "card")
        det.pack(fill="x", padx=P(14), pady=(P(8), P(12)))
        self.p_left = label(det, "", 8, fg="mute", bg="card")
        self.p_left.pack(side="left")
        self.p_right = label(det, "", 8, fg="mute", bg="card")
        self.p_right.pack(side="right")
        self.p_steps = {}
        for key, text in self.STEPS:
            r = frame(b)
            r.pack(fill="x", pady=(0, P(5)))
            ic = StepIcon(r, P(14), C["bg"])
            ic.pack(side="left")
            t = label(r, text, 9)
            t.pack(side="left", padx=(P(9), 0))
            d = label(r, "", 8, fg="dim")
            d.pack(side="left", padx=(P(8), 0))
            self.p_steps[key] = (ic, t, d)
        self.p_btn = Btn(b, "Cancelar", self.cancel_install, "danger", pady=P(8))
        self.p_btn.pack(side="bottom", fill="x", pady=(P(10), 0))
        lh = frame(b)
        lh.pack(fill="x", pady=(P(8), P(4)))
        label(lh, "Registro", 8, fg="mute").pack(side="left")
        Btn(lh, "Copiar", self.copy_log, "link", padx=0, pady=0, size=8).pack(side="right")
        lb = frame(b, "log", "line")
        lb.pack(fill="both", expand=True)
        self.p_log = tk.Text(lb, bg=C["log"], fg=C["logfg"], font=(MONO, 8), relief="flat", wrap="word", height=4,
                             highlightthickness=0, padx=P(9), pady=P(7), state="disabled",
                             selectbackground=C["sel"], insertbackground=C["text"])
        for tag, col in (("hi", C["soft"]), ("err", C["err"]), ("ok", C["okfg"])):
            self.p_log.tag_configure(tag, foreground=col)
        self.p_log.pack(fill="both", expand=True)
        return pg

    def log(self, text, tag=None):
        self.p_log.configure(state="normal")
        self.p_log.insert("end", text + "\n", tag or ())
        if int(self.p_log.index("end-1c").split(".")[0]) > 2000:
            self.p_log.delete("1.0", "500.0")
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
        t.configure(fg=C["dim"] if state == "wait" else C["err"] if state == "err" else C["text"],
                    font=(FONT_B, 9, "bold") if state == "run" else (FONT, 9))
        if detail is not None:
            d.configure(text=detail[:60])

    # ------------------------------------------------------------ página: listo

    def _page_done(self):
        P = self.px
        pg = frame(self.side)
        b = self.pad(pg)
        btns = frame(b)
        btns.pack(side="bottom", fill="x")
        btns.columnconfigure(0, weight=1, uniform="b")
        btns.columnconfigure(1, weight=1, uniform="b")
        Btn(btns, "Instalar otro", lambda: (self.reset_install(), self.show("install")), pady=P(11)).grid(
            row=0, column=0, sticky="ew", padx=(0, P(5)))
        Btn(btns, "Ver biblioteca", self.open_library, "primary", pady=P(11)).grid(row=0, column=1, sticky="ew", padx=(P(5), 0))
        mid = frame(b)
        mid.place(relx=.5, rely=.45, anchor="center")
        hold = frame(mid)
        hold.pack()
        self.d_cover = tk.Label(hold, bg=C["bg"], bd=0)
        self.d_cover.pack(padx=P(10), pady=P(10))
        s = P(32)
        chk = tk.Canvas(hold, width=s, height=s, bg=C["bg"], highlightthickness=0)
        chk.create_oval(1, 1, s - 1, s - 1, fill=C["ok"], outline=C["bg"], width=P(3))
        chk.create_line(s * .3, s * .52, s * .45, s * .67, s * .72, s * .36, fill="#0B1A13", width=P(3), capstyle="round")
        chk.place(relx=1, rely=1, anchor="se")
        self.d_title = label(mid, "", 14, True, anchor="center", justify="center", wraplength=P(380))
        self.d_title.pack(pady=(P(10), P(6)))
        label(mid, "Búscalo en la pantalla de inicio de la PS5. Deja HostMount abierto (puedes minimizarlo) "
                   "mientras juegas.", 9, fg="mute", anchor="center", justify="center", wraplength=P(330)).pack()
        bd = frame(mid)
        bd.pack(pady=(P(12), 0))
        self.d_b1 = Badge(bd, self)
        self.d_b1.pack(side="left", padx=P(3))
        self.d_b2 = Badge(bd, self)
        self.d_b2.pack(side="left", padx=P(3))
        return pg

    def open_library(self):
        if not self.wide:
            self.maximize()
        self.show("game" if self.selected else "install")

    # ------------------------------------------------------------ página: juego (biblioteca)

    def _page_game(self):
        P = self.px
        pg = frame(self.side)
        b = self.pad(pg)
        Btn(b, "←  Instalar un juego", lambda: self.show("install"), "link", padx=0, pady=0, size=9).pack(anchor="w")
        head = frame(b)
        head.pack(fill="x", pady=(P(14), 0))
        self.gd_cover = tk.Label(head, bd=0)
        self.gd_cover.pack(side="left")
        ht = frame(head)
        ht.pack(side="left", fill="x", expand=True, padx=(P(12), 0))
        self.gd_name = label(ht, "", 12, True, wraplength=P(280))
        self.gd_name.pack(fill="x")
        self.gd_meta = label(ht, "", 8, fg="mute")
        self.gd_meta.pack(fill="x", pady=(P(2), 0))
        bd = frame(b)
        bd.pack(fill="x", pady=(P(12), 0))
        self.gd_b1 = Badge(bd, self)
        self.gd_b1.pack(side="left", padx=(0, P(6)))
        self.gd_b2 = Badge(bd, self)
        self.gd_b2.pack(side="left")
        card = frame(b, "card", "cardline")
        card.pack(fill="x", pady=(P(12), 0))
        self.gd_rows = {}
        for k, t in (("read", "Leyendo ahora"), ("installed", "Instalado"), ("folder", "Carpeta")):
            r = frame(card, "card")
            r.pack(fill="x", padx=P(14), pady=(P(10) if k == "read" else P(4), P(10) if k == "folder" else 0))
            label(r, t, 9, fg="mute", bg="card").pack(side="left")
            v = label(r, "", 9, bg="card", anchor="e")
            v.pack(side="right")
            self.gd_rows[k] = v
        row = frame(b)
        row.pack(fill="x", pady=(P(12), 0))
        row.columnconfigure(0, weight=1, uniform="b")
        row.columnconfigure(1, weight=1, uniform="b")
        Btn(row, "Reinstalar", self.reinstall_selected, pady=P(7)).grid(row=0, column=0, sticky="ew", padx=(0, P(5)))
        Btn(row, "Quitar", lambda: self.selected and self.remove_game(self.selected), "danger", pady=P(7)).grid(
            row=0, column=1, sticky="ew", padx=(P(5), 0))
        label(b, "Servidor · últimas lecturas", 8, fg="mute").pack(fill="x", pady=(P(16), P(4)))
        lb = frame(b, "log", "line")
        lb.pack(fill="both", expand=True)
        self.srv_log = tk.Text(lb, bg=C["log"], fg=C["logfg"], font=(MONO, 8), relief="flat", wrap="none", height=4,
                               highlightthickness=0, padx=P(9), pady=P(7), state="disabled")
        self.srv_log.pack(fill="both", expand=True)
        return pg

    def fill_game(self):
        tid = self.selected
        g = self.cfg["games"].get(tid)
        if not g:
            return self.show("install")
        icon = os.path.join(g["folder"], "sce_sys", "icon0.png")
        self.set_icon(self.gd_cover, icon if os.path.isfile(icon) else None, 64, "bg")
        self.gd_name.configure(text=g["name"])
        self.gd_meta.configure(text="%s · v%s" % (tid, g.get("version", "")))
        st, kind = self.game_state(tid, g)
        self.gd_b1.set(st, kind)
        if self.server.running():
            self.gd_b2.set("Servidor activo", "info")
        else:
            self.gd_b2.set("Servidor parado", "off")
        self.gd_rows["installed"].configure(text=g.get("installed", "—"))
        f = g["folder"]
        self.gd_rows["folder"].configure(text=("…" + f[-34:]) if len(f) > 35 else f)
        self.update_reading()

    def update_reading(self):
        if self.page != "game" or not self.selected:
            return
        rate, tid = self.server.rate()
        self.gd_rows["read"].configure(text=("%s/s" % human(rate)) if tid == self.selected else "—")

    def reinstall_selected(self):
        g = self.cfg["games"].get(self.selected)
        if g:
            self.show("install")
            self.pick(g["folder"])

    # ------------------------------------------------------------ biblioteca

    def _build_library(self, parent):
        P = self.px
        lib = frame(parent)
        head = frame(lib)
        head.pack(fill="x", padx=P(28), pady=(P(22), P(16)))
        Btn(head, "Añadir ya instalado", self.add_existing, pady=P(6)).pack(side="right", anchor="s")
        ht = frame(head)
        ht.pack(side="left", fill="x", expand=True)
        label(ht, "Biblioteca", 17, True).pack(fill="x")
        self.lib_sub = label(ht, "", 9, fg="mute")
        self.lib_sub.pack(fill="x", pady=(P(3), 0))
        self.grid = Scroll(lib, C["bg"])
        self.grid.pack(fill="both", expand=True, padx=(P(28), P(12)), pady=(0, P(16)))
        return lib

    def game_state(self, tid, g):
        if not os.path.isdir(g["folder"]):
            return "Carpeta no encontrada", "err"
        on = None if self.ps5_games is None else any(n.endswith("(%s)" % tid) for n in self.ps5_games)
        if on is False:
            return "No está en la PS5", "warn"
        return ("Listo", "ok") if on else ("En el PC", "off")

    def refresh_library(self):
        if not self.wide:
            return
        P = self.px
        games = self.cfg["games"]
        self.lib_sub.configure(text="%d juego%s · se leen desde este PC mientras juegas"
                               % (len(games), "" if len(games) == 1 else "s"))
        self.lib_link.configure(text="")
        for w in self.grid.inner.winfo_children():
            w.destroy()
        avail = max(P(300), self.lib.winfo_width() - P(60))
        self._lib_w = self.lib.winfo_width()
        gap = P(18)
        cols = max(2, min(7, (avail + gap) // (P(170) + gap)))
        tile = (avail - gap * (cols - 1)) // cols
        rate, reading = self.server.rate()
        items = sorted(games.items(), key=lambda kv: kv[1]["name"].lower())
        for i, (tid, g) in enumerate(items + [(None, None)]):
            t = frame(self.grid.inner)
            t.grid(row=i // cols, column=i % cols, sticky="nw", padx=(0, gap if i % cols < cols - 1 else 0), pady=(0, gap))
            if tid is None:
                box = tk.Canvas(t, width=tile, height=tile, bg=C["bg"], highlightthickness=0, cursor="hand2")
                box.create_rectangle(2, 2, tile - 2, tile - 2, outline="#2C323D", dash=(6, 4), width=2)
                box.create_text(tile / 2, tile / 2, text="+", fill=C["dim"], font=(FONT, 26))
                box.pack()
                box.bind("<Button-1>", lambda e: self.show("install"))
                Btn(t, "Instalar juego", lambda: self.show("install"), "link", padx=0, pady=P(6), size=9).pack(anchor="w")
                continue
            sel = tid == self.selected
            hold = frame(t, "accent" if sel else "bg")
            hold.pack()
            icon = os.path.join(g["folder"], "sce_sys", "icon0.png")
            cv = tk.Label(hold, bd=0, cursor="hand2")
            img = self.set_icon(cv, icon if os.path.isfile(icon) else None, tile / self.S - 4, "bg")
            cv.pack(padx=P(2), pady=P(2))
            if img.width() < tile - P(4):
                cv.configure(width=tile - P(4), height=tile - P(4))
            if reading == tid:
                label(hold, "▶ leyendo", 8, True, fg="link", bg="#0A0C10", padx=P(6), pady=P(1)).place(x=P(10), y=P(10))
            name = label(t, g["name"], 9, True, cursor="hand2", width=1)
            name.pack(fill="x", pady=(P(6), P(2)))
            r = frame(t)
            r.pack(fill="x")
            label(r, tid, 8, fg="dim").pack(side="left")
            st, kind = self.game_state(tid, g)
            Badge(r, self, st, kind).pack(side="right")
            t.configure(width=tile)
            for w in (cv, name, hold):
                w.bind("<Button-1>", lambda e, x=tid: self.select(x))
        for c in range(cols):
            self.grid.inner.columnconfigure(c, minsize=tile)
        self.scan_ps5()

    def select(self, tid):
        self.selected = tid
        self.show("game")
        self.refresh_library()

    def scan_ps5(self):
        if getattr(self, "_scanning", False) or time.time() - getattr(self, "_scanned", 0) < 20:
            return
        self._scanning = True

        def run():
            try:
                ftp = ftp_open(self.cfg["ps5"])
                names = ftp_names(ftp, HOMEBREW_DIR)
                ftp.quit()
            except (ftplib.all_errors, OSError):
                names = None
            self._scanned = time.time()
            self._scanning = False
            if names != self.ps5_games:
                self.ps5_games = names
                self.post(self.refresh_library)
                self.post(lambda: self.page == "game" and self.fill_game())
        threading.Thread(target=run, daemon=True).start()

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
        self._scanned = 0
        if self.kit:
            self.server_note = self.server.start(self.kit, self.serve_roots())
        self.select(g["tid"])

    def remove_game(self, tid):
        g = self.cfg["games"].get(tid)
        if not g:
            return
        ans = messagebox.askyesnocancel(
            "Quitar %s" % g["name"],
            "¿Borrarlo también de la PS5?\n\nSí: borra su carpeta en %s y el enlace %s/%s.cfg (cierra el juego antes). "
            "Las partidas guardadas no se tocan.\nNo: solo lo quita de la biblioteca.\n\nLos archivos de este PC no se borran nunca."
            % (HOMEBREW_DIR, LINK_DIR, tid), icon="warning")
        if ans is None:
            return
        if not ans:
            return self._removed(tid)

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

    def _removed(self, tid):
        self.cfg["games"].pop(tid, None)
        save_cfg(self.cfg)
        self._scanned = 0
        if self.selected == tid:
            self.selected = None
            self.show("install")
        if self.kit and self.server.running():
            roots = self.serve_roots()
            self.server.start(self.kit, roots) if roots else self.server.stop()
        self.refresh_library()

    # ------------------------------------------------------------ página: ajustes

    def _page_settings(self):
        P = self.px
        pg = frame(self.side)
        b = self.pad(pg)
        Btn(b, "←  Volver", lambda: self.show("install"), "link", padx=0, pady=0, size=9).pack(anchor="w")
        label(b, "Ajustes", 15, True).pack(fill="x", pady=(P(12), 0))
        self.set_vars = {}
        for key, title, hint in (("ps5", "IP de la PS5", "La de etaHEN; FTP en el puerto %d." % FTP_PORT),
                                 ("fw", "Firmware de la PS5", "Para saber si un juego necesita backport."),
                                 ("pc_ip", "IP de este PC", "Vacío = automática (la de la red de la PS5).")):
            label(b, title, 8, True, fg="soft").pack(fill="x", pady=(P(14), P(5)))
            v = tk.StringVar(value=self.cfg.get(key, ""))
            tk.Entry(b, textvariable=v, font=(FONT, 9), bg=C["field"], fg=C["text"], insertbackground=C["text"],
                     relief="flat", highlightthickness=1, highlightbackground=C["fieldline"],
                     highlightcolor=C["accent"]).pack(fill="x", ipady=P(6))
            label(b, hint, 8, fg="dim").pack(fill="x", pady=(P(3), 0))
            self.set_vars[key] = v
        label(b, "Servidor de archivos", 8, True, fg="soft").pack(fill="x", pady=(P(16), P(5)))
        sr = frame(b)
        sr.pack(fill="x")
        self.set_srv = label(sr, "", 9)
        self.set_srv.pack(side="left")
        self.set_srv_btn = Btn(sr, "Iniciar", self.toggle_server, pady=P(4))
        self.set_srv_btn.pack(side="right")
        label(b, "Kit", 8, True, fg="soft").pack(fill="x", pady=(P(16), P(5)))
        kr = frame(b)
        kr.pack(fill="x")
        Btn(kr, "Elegir zip…", self.choose_kit, pady=P(4)).pack(side="right")
        self.kit_lbl = label(kr, "buscando…", 8, fg="mute", wraplength=P(280))
        self.kit_lbl.pack(side="left", fill="x", expand=True)
        Btn(b, "Guardar", self.save_settings, "primary", pady=P(10)).pack(side="bottom", fill="x")
        label(b, "Puertos: servidor %d · Kraken Mount %d. Configuración en %s" % (SERVE_PORT, KRAKEN_PORT, CFG_PATH),
              8, fg="dim", wraplength=P(390)).pack(side="bottom", fill="x", pady=(0, P(12)))
        return pg

    def save_settings(self):
        ps5 = self.set_vars["ps5"].get().strip()
        pc = self.set_vars["pc_ip"].get().strip()
        try:
            socket.inet_aton(ps5)
            pc and socket.inet_aton(pc)
            float(self.set_vars["fw"].get().replace(",", "."))
        except (OSError, ValueError):
            return messagebox.showerror("Ajustes", "Revisa la IP y el firmware (por ejemplo 192.168.1.38 y 12.60).")
        self.cfg["ps5"], self.cfg["fw"] = ps5, self.set_vars["fw"].get().strip().replace(",", ".")
        self.cfg["pc_ip"] = pc
        save_cfg(self.cfg)
        self.ps5_games, self._scanned = None, 0
        self.pill_fw.set("FW " + self.cfg["fw"])
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
            return messagebox.showerror("Kit", "Ese zip no es el kit de HostMount (falta WINDOWS-PATCH %s)." % KIT_MARK)
        self.cfg["kit"] = d
        save_cfg(self.cfg)
        self.set_kit(d)

    def set_kit(self, d):
        self.kit = d
        self.kit_lbl.configure(text=d if d else "No encontrado: elige el zip del kit", fg=C["mute"] if d else C["err"])

    # ------------------------------------------------------------ instalación

    def reset_install(self):
        self.install_proc = None

    def install(self):
        g = self.current
        if not g or self.install_proc is not None:
            return
        if not self.kit:
            messagebox.showerror("HostMount", "No encuentro el kit de HostMount (WINDOWS-PATCH %s). Indícalo en Ajustes." % KIT_MARK)
            return self.show("settings")
        if g["risky"] and not messagebox.askyesno("Firmware", "El juego pide un firmware mayor que el de tu PS5 y no "
                                                  "veo un backport. ¿Instalar igualmente?"):
            return
        self.cfg["ampr_off"] = self.ampr_var.get()
        save_cfg(self.cfg)
        self.install_proc = "preparing"
        self.cur_step = None
        self.up_total = self.up_done = self.up_bytes = self.up_mib = 0
        self.up_hits = deque()
        self.p_name.configure(text=g["name"])
        self.p_sub.configure(text="Instalando en la PS5 %s…" % self.cfg["ps5"], fg=C["mute"], wraplength=self.px(330))
        self.set_icon(self.p_cover, g["icon"], 52, "bg")
        self.p_phase.configure(text="Comprobando…", fg=C["text"])
        self.p_pct.configure(text="")
        self.p_bar.set(0)
        self.p_left.configure(text="")
        self.p_right.configure(text="")
        self.p_log.configure(state="normal")
        self.p_log.delete("1.0", "end")
        self.p_log.configure(state="disabled")
        for k, _ in self.STEPS:
            self.step(k, "wait", "")
        self.step("check", "run")
        self.p_btn.configure(text="Cancelar")
        self.p_btn.cmd = self.cancel_install
        self.show("progress")
        threading.Thread(target=self._precheck, args=(g,), daemon=True).start()

    def _precheck(self, g):
        ps5 = self.cfg["ps5"]
        ip = local_ip(ps5, self.cfg.get("pc_ip"))
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
        if self.install_proc == "cancelled":
            return
        if found:
            msg = ("En la PS5 ya hay una instalación anterior o restos de este juego:\n\n%s\n\n"
                   "Para instalar hay que borrarlos (solo esas carpetas; las partidas guardadas no se tocan). ¿Borrar y seguir?"
                   % "\n".join(found))
            if not messagebox.askyesno("Ya existe en la PS5", msg, icon="warning"):
                return self.install_failed("check", "Cancelado: ya existe en la PS5.")
        bp = " · backport %.2f" % g["nfo_fw"] if g["nfo_fw"] and g["cfw_arg"] == 99.99 else ""
        self.step("check", "ok", "FW %.2f%s" % (self.console_fw(), bp))
        self.log("PS5 %s:%d OK · este PC %s" % (self.cfg["ps5"], FTP_PORT, ip))
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
        self.post(self.step, "starter", "run")
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
        self.post(self._set_proc, proc)
        tail = deque(maxlen=60)
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
        self.post(self.on_install_end, g, ok and rc == 0, rc, list(tail))

    def _set_proc(self, proc):
        if self.install_proc == "cancelled":
            proc.terminate()
        else:
            self.install_proc = proc

    def on_install_line(self, line):
        if not line:
            return
        low = line.lower()
        tag = "err" if ("error" in low or "failed" in low or "traceback" in low or "refusing" in low) else None
        if line.startswith("starter:"):
            self.step("starter", "ok")
        m = re.match(r"pushing (\d+) files \((\d+) MiB\) -> (.*)", line)
        if m:
            self.up_total, self.up_mib = int(m.group(1)), int(m.group(2))
            self.step("starter", "ok")
            self.step("upload", "run")
            self.p_phase.configure(text="Subiendo archivos")
            self.update_progress()
            tag = "hi"
        if line.startswith("placed"):
            self.step("upload", "ok", "%s archivos" % nfmt(self.up_total))
            self.step("link", "run")
            self.p_bar.set(1)
            self.p_pct.configure(text="100%")
            tag = "ok"
        if line.startswith("link config:"):
            self.step("link", "ok")
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
        self.p_bar.set(frac)
        self.p_pct.configure(text="%d%%" % int(frac * 100))
        self.p_left.configure(text="%s / %s · %s / %s" % (nfmt(self.up_done), nfmt(self.up_total),
                                                         human(self.up_bytes), human(self.up_mib << 20)))
        now = time.time()
        while self.up_hits and self.up_hits[0][0] < now - 8:
            self.up_hits.popleft()
        if len(self.up_hits) > 1:
            span = max(0.5, now - self.up_hits[0][0])
            fps = len(self.up_hits) / span
            left = (self.up_total - self.up_done) / fps if fps else 0
            self.p_right.configure(text="%s/s · ~%s" % (human(sum(s for _, s in self.up_hits) / span), self.eta(left)))

    @staticmethod
    def eta(sec):
        sec = int(sec)
        return "%d s" % sec if sec < 90 else "%d min" % round(sec / 60.0)

    def on_install_end(self, g, ok, rc, tail):
        if self.install_proc == "cancelled":
            return
        if not ok:
            msg = explain_failure(tail, rc)
            return self.install_failed(getattr(self, "cur_step", None) or "starter", msg)
        self.cfg["games"][g["tid"]] = {"name": g["name"], "folder": g["folder"], "version": g["version"],
                                       "installed": time.strftime("%d/%m/%Y %H:%M")}
        save_cfg(self.cfg)
        self._scanned = 0
        self.server.start(self.kit, self.serve_roots())
        self.selected = g["tid"]
        if self.cfg.get("ampr_off", True):
            self.step("kraken", "run")
            self.p_phase.configure(text="Ajustando Kraken Mount")

            def go():
                self.post(self.finish, g, kraken_ampr_off(self.cfg["ps5"], g["tid"]))
            threading.Thread(target=go, daemon=True).start()
        else:
            self.step("kraken", "ok", "sin cambios")
            self.finish(g, (None, None))

    def finish(self, g, res):
        ok, msg = res
        if msg:
            self.log(msg, "ok" if ok else "err")
        self.install_proc = None
        self.set_icon(self.d_cover, g["icon"], 148, "bg")
        self.d_title.configure(text="%s está listo" % g["name"])
        self.d_b1.set("%s en la PS5" % human(self.up_mib << 20) if self.up_mib else "Instalado", "ok")
        if ok is None:
            self.d_b2.set("Kraken sin cambios", "off")
        elif ok:
            self.d_b2.set("ampr-emu-ng desactivado", "ok")
        else:
            self.d_b2.set("Desactiva ampr-emu-ng a mano", "warn")
        self.refresh_library()
        self.lib_link.configure(text="" if self.wide else "Biblioteca · %d  ↗" % len(self.cfg["games"]))
        self.show("done")

    def install_failed(self, key, msg):
        self.install_proc = None
        self.step(key, "err")
        self.log(msg, "err")
        self.p_phase.configure(text="No se pudo instalar", fg=C["err"])
        self.p_sub.configure(text=msg[:240], fg=C["err"], wraplength=self.px(330))
        self.p_btn.configure(text="Volver")
        self.p_btn.cmd = lambda: self.show("install")

    def cancel_install(self):
        p = self.install_proc
        if p is None:
            return self.show("install")
        if not messagebox.askyesno("Cancelar", "¿Cancelar la instalación? Lo subido a medias se borrará en el próximo intento."):
            return
        self.install_proc = "cancelled"
        if isinstance(p, subprocess.Popen):
            p.terminate()
        self.log("Cancelado.", "err")
        self.p_phase.configure(text="Cancelado", fg=C["err"])
        self.p_btn.configure(text="Volver")
        self.p_btn.cmd = lambda: (self.reset_install(), self.show("install"))

    # ------------------------------------------------------------ estado general

    def _boot(self):
        kit = find_kit(self.cfg)
        self.post(self.set_kit, kit)
        if kit and self.cfg["games"]:
            self.post(self.start_server_quiet)
        self._probe()

    def start_server_quiet(self):
        self.server_note = self.server.start(self.kit, self.serve_roots())
        self.update_server()

    def _probe(self):
        ps5 = self.cfg["ps5"]
        res = (local_ip(ps5, self.cfg.get("pc_ip")), port_open(ps5, FTP_PORT), port_open(ps5, KRAKEN_PORT))
        self.post(self.set_probe, res)

    def set_probe(self, res):
        self.pc_ip, ftp_ok, kraken_ok = res
        self.pill_ps5.set("PS5 %s" % self.cfg["ps5"], "ok" if ftp_ok else "err")
        self.ps5_ok, self.kraken_ok = ftp_ok, kraken_ok

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
        self.update_server()

    def on_server_line(self, line):
        self.srv_log.configure(state="normal")
        self.srv_log.insert("end", line + "\n")
        if int(self.srv_log.index("end-1c").split(".")[0]) > 300:
            self.srv_log.delete("1.0", "100.0")
        self.srv_log.see("end")
        self.srv_log.configure(state="disabled")

    def on_server_exit(self, proc):
        self.on_server_line("— servidor detenido (código %s) —" % proc.returncode)
        self.update_server()

    def update_server(self):
        if self.server.running():
            rate, _ = self.server.rate()
            self.pill_srv.set("Servidor · %s/s" % human(rate) if rate > 1024 else "Servidor activo", "ok")
            self.set_srv.configure(text="Activo · %s:%d · %s lecturas" % (self.pc_ip or "?", SERVE_PORT, nfmt(self.server.reqs)))
            self.set_srv_btn.configure(text="Detener")
        else:
            other = self.server_note and "puerto" in self.server_note
            self.pill_srv.set("Puerto %d ocupado" % SERVE_PORT if other else "Servidor parado", "warn" if other else "dim")
            self.set_srv.configure(text=("Otro programa usa el puerto %d" % SERVE_PORT) if other else "Parado")
            self.set_srv_btn.configure(text="Iniciar")

    def _tick(self):
        self.update_server()
        self.update_reading()
        if isinstance(self.install_proc, subprocess.Popen) and self.up_total:
            self.update_progress()
        self._ticks = getattr(self, "_ticks", 0) + 1
        if self._ticks % 15 == 0:
            threading.Thread(target=self._probe, daemon=True).start()
        if self.wide and self._ticks % 5 == 0:
            _, tid = self.server.rate()
            if tid != getattr(self, "_last_reading", None):
                self._last_reading = tid
                self.refresh_library()
        self.root.after(1000, self._tick)

    def on_close(self):
        if self.install_proc is not None and self.install_proc != "cancelled":
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
