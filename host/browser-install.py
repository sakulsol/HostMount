#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""browser-install.py - install PS4/PS5 .pkg files from the PS5's own web browser.

Open http://<this mac>:4490/ in the PS5 browser: it lists every .pkg under the library folders, tap Install and
THIS console (found from the browser's IP) installs it through BlazingMount (blazingmountd >= 2.10, :8094
/api/install?url=). This computer serves the pkg itself (Range requests); nothing is copied first.
Why a server-side hop: blazingmountd wants an X-BM-Request header and answers no CORS preflight, so a page cannot call
the console directly.
    python3 browser-install.py [--port 4490] [--dir <folder> ...]
"""
import argparse, hashlib, html, http.server, json, os, socket, socketserver, threading, urllib.parse, urllib.request

DIRS = ["/Volumes/Untitled/PS4 Games", "/Volumes/Untitled/PS5 Games", "/Volumes/Untitled/PS5 Games - not packed yet",
        "/Volumes/Untitled/PS5-DOWNLOADS", "/Volumes/Untitled/BlazingGames"]
BM = 8094
LOCK = threading.Lock()
PKGS = {}  # id -> path


def scan(dirs):
    out = {}
    for d in dirs:
        for root, subs, files in os.walk(d):
            subs[:] = [s for s in subs if not s.startswith((".", "$"))]
            if root.count(os.sep) - d.count(os.sep) > 3:
                subs[:] = []
            for f in files:
                if f.lower().endswith(".pkg") and not f.startswith("._"):
                    p = os.path.join(root, f)
                    try:
                        with open(p, "rb") as fh:
                            if fh.read(4) != b"\x7fCNT":
                                continue
                    except OSError:
                        continue
                    out[hashlib.sha1(p.encode()).hexdigest()[:12]] = p
    return out


def bm(console, method, path, timeout=8):
    req = urllib.request.Request("http://%s:%d%s" % (console, BM, path), method=method, headers={"X-BM-Request": "1"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def my_ip_for(console):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((console, BM)); return s.getsockname()[0]
    finally:
        s.close()


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><title>Install to PS5</title>
<style>body{background:#0A0A0B;color:#fffc;font:22px system-ui;margin:24px}h1{color:#fff;font-size:34px}
.r{display:flex;align-items:center;gap:16px;padding:14px 8px;border-bottom:2px solid #FF3D4733}.n{flex:1}.s{color:#fff9;font-size:18px}
button{background:#0E0A0C;color:#fff;border:3px solid #FF3D4766;border-radius:18px;padding:12px 22px;font-size:22px}
button:focus,button:hover{border-color:#FF3D47;outline:none}#st{font-size:24px;color:#fff;padding:12px 0}</style>
<h1>Install to this PS5</h1><div id=st>Console: %(console)s</div>%(rows)s
<script>
function I(id,b){b.disabled=true;fetch('/install?id='+id,{method:'POST'}).then(r=>r.json()).then(j=>{st.textContent=j.msg;b.disabled=false})}
function P(){fetch('/status').then(r=>r.json()).then(j=>{if(j.line)st.textContent=j.line})}
setInterval(P,2000);P()
</script>"""


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        print('%s %s' % (self.client_address[0], fmt % a), flush=True)

    def send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)

    def console(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return q.get("console", [self.client_address[0]])[0]

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            global PKGS
            with LOCK:
                PKGS = scan(ARGS.dir)
            rows = []
            for i, p in sorted(PKGS.items(), key=lambda kv: os.path.basename(kv[1]).lower()):
                sz = os.path.getsize(p) / 1e9
                kind = "PS4" if "/PS4 " in p or os.path.basename(p)[:2] in ("CU", "EP", "UP", "JP", "HP", "EZ") and "PPSA" not in p else "PS5"
                rows.append("<div class=r><div class=n>%s<div class=s>%s pkg · %.1f GB · %s</div></div>"
                            "<button onclick=\"I('%s',this)\">Install</button></div>"
                            % (html.escape(os.path.basename(p)[:-4]), kind, sz, html.escape(os.path.dirname(p).split("/")[-1]), i))
            return self.send(200, PAGE % {"console": html.escape(self.console()), "rows": "".join(rows) or "<p>No pkgs found.</p>"},
                             "text/html; charset=utf-8")
        if u.path == "/status":
            code, j = bm(self.console(), "GET", "/api/install")
            line = ""
            if code == 200 and j.get("state") not in (None, "idle"):
                line = "%s: %s %.0f%%%s%s" % (j.get("name") or "install", j.get("state"), float(j.get("progress") or 0),
                                              (" · %.0f MB/s" % (j["rate"] / 1e6)) if j.get("rate") else "",
                                              (" · " + j["error"]) if j.get("error") else "")
            return self.send(200, json.dumps({"line": line, "raw": j}))
        if u.path.startswith("/pkg/"):
            return self.serve_pkg(u.path[5:].split("/")[0])
        self.send(404, "{}")

    def do_HEAD(self):
        u = urllib.parse.urlparse(self.path)
        if u.path.startswith("/pkg/"):
            return self.serve_pkg(u.path[5:].split("/")[0], head=True)
        self.send(404, "")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/install":
            return self.send(404, "{}")
        q = urllib.parse.parse_qs(u.query)
        i = q.get("id", [""])[0]
        p = PKGS.get(i)
        if not p:
            return self.send(404, json.dumps({"msg": "pkg not found - reload the page"}))
        console = self.console()
        url = "http://%s:%d/pkg/%s/%s" % (my_ip_for(console), ARGS.port, i, urllib.parse.quote(os.path.basename(p)))
        code, j = bm(console, "POST", "/api/install?" + urllib.parse.urlencode({"url": url, "name": os.path.basename(p)[:-4]}), 20)
        msg = "Installing %s ..." % os.path.basename(p)[:-4] if code == 200 else "Refused (%s): %s" % (code, j.get("error") or j)
        self.send(200, json.dumps({"msg": msg, "code": code, "bm": j}))

    def serve_pkg(self, i, head=False):
        p = PKGS.get(i)
        if not p:
            return self.send(404, "")
        size = os.path.getsize(p)
        a, b = 0, size - 1
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            s, _, e = rng[6:].split(",")[0].partition("-")
            if s:
                a = int(s); b = int(e) if e else size - 1
            else:
                a = size - int(e)
            b = min(b, size - 1)
            if a > b or a >= size:
                self.send_response(416); self.send_header("Content-Range", "bytes */%d" % size); self.send_header("Content-Length", "0"); self.end_headers(); return
            self.send_response(206); self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, size))
        else:
            self.send_response(200)
        self.send_header("Accept-Ranges", "bytes"); self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(b - a + 1)); self.end_headers()
        if head:
            return
        with open(p, "rb") as fh:
            fh.seek(a); n = b - a + 1
            while n > 0:
                d = fh.read(min(1 << 20, n))
                if not d:
                    break
                try:
                    self.wfile.write(d)
                except (BrokenPipeError, ConnectionResetError):
                    return
                n -= len(d)


class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4490)
    ap.add_argument("--dir", action="append", default=None)
    ARGS = ap.parse_args()
    ARGS.dir = ARGS.dir or DIRS
    PKGS = scan(ARGS.dir)
    print("browser-install on :%d, %d pkgs" % (ARGS.port, len(PKGS)), flush=True)
    S(("0.0.0.0", ARGS.port), H).serve_forever()
