#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Developer - TRHACKNON
TurboFTP-GUI — Hacker/Anonymous styled FTP browser & downloader
- Browse remote FTP (MLSD preferred, falls back to LIST/NLST)
- Reliable downloads with resume (single-stream)
- Optional turbo segmented download (multi-connection, N segments)
- Retries + timeouts, progress, speed, ETA
- FTPS (explicit TLS) optional

Notes:
* Segmented downloads open multiple FTP connections and use REST to start each segment.
  We intentionally stop each segment after its quota of bytes; the server may log a
  "transfer aborted" — this is expected and safe.
* Some servers limit parallel connections; if you see many failures, disable turbo or
  lower thread count.
* Requires only the Python standard library.
"""

import os
import io
import time
import math
import queue
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from ftplib import FTP, FTP_TLS, error_perm, all_errors

# ===================== Config =====================
DEFAULT_TIMEOUT = 15          # seconds for socket ops
CONNECT_RETRIES = 3           # retries for connecting/login
LIST_RETRIES = 2              # retries for listing
DL_RETRIES = 4                # retries per whole download (single) or per segment (turbo)
BLOCKSIZE = 1024 * 64         # 64 KiB per chunk
MAX_TURBO_THREADS = 8         # safety cap

# ===================== Helpers =====================
@dataclass
class FTPServer:
    host: str = "ptiq-ftp.itron-hosting.com"
    port: int = 21
    user: str = "eb_parquetecno"
    password: str = "EverBlu888"
    use_ftps: bool = False
    passive: bool = True


def human_bytes(n: Optional[int]) -> str:
    if n is None:
        return "?"
    units = ["B","KB","MB","GB","TB"]
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units)-1:
        f /= 1024
        i += 1
    return f"{f:.2f} {units[i]}"


def mk_client(srv: FTPServer):
    Client = FTP_TLS if srv.use_ftps else FTP
    ftp = Client()
    ftp.timeout = DEFAULT_TIMEOUT
    for attempt in range(1, CONNECT_RETRIES+1):
        try:
            ftp.connect(srv.host, srv.port, timeout=DEFAULT_TIMEOUT)
            ftp.login(srv.user, srv.password)
            if isinstance(ftp, FTP_TLS):
                ftp.prot_p()  # protect data channel
            ftp.set_pasv(srv.passive)
            return ftp
        except all_errors as e:
            try:
                ftp.close()
            except Exception:
                pass
            if attempt >= CONNECT_RETRIES:
                raise
            time.sleep(1.5 * attempt)
    return ftp


def try_size(ftp: FTP, path: str) -> Optional[int]:
    try:
        return ftp.size(path)
    except all_errors:
        return None


def try_pwd(ftp: FTP) -> str:
    try:
        return ftp.pwd()
    except all_errors:
        return "/"


def mlsd_supported(ftp: FTP) -> bool:
    try:
        ftp.sendcmd("OPTS MLST type;size;modify;")
        # If no error, assume MLSD available
        return True
    except all_errors:
        return False


def list_dir(ftp: FTP, cwd: str) -> List[Tuple[str, dict]]:
    """Return list of (name, facts) where facts has keys: type (file/dir), size (int|None), modify (YYYYMMDDhhmmss|None)"""
    for attempt in range(1, LIST_RETRIES+1):
        try:
            ftp.cwd(cwd)
            items: List[Tuple[str, dict]] = []
            if mlsd_supported(ftp):
                for name, facts in ftp.mlsd():
                    f = {
                        'type': facts.get('type') or 'file',
                        'size': int(facts['size']) if 'size' in facts and facts['size'].isdigit() else None,
                        'modify': facts.get('modify')
                    }
                    items.append((name, f))
            else:
                # Fallback: NLST names, and try SIZE for each
                names = ftp.nlst()
                for name in names:
                    # Determine if directory by trying CWD
                    is_dir = False
                    try:
                        ftp.cwd(name)
                        ftp.cwd('..')
                        is_dir = True
                    except all_errors:
                        is_dir = False
                    size = None if is_dir else try_size(ftp, name)
                    items.append((name, {'type': 'dir' if is_dir else 'file', 'size': size, 'modify': None}))
            # Sort: directories first, then files by name
            items.sort(key=lambda t: (0 if t[1]['type'] == 'dir' else 1, t[0].lower()))
            return items
        except all_errors as e:
            if attempt >= LIST_RETRIES:
                raise
            time.sleep(1.0 * attempt)
    return []


# ===================== Downloaders =====================
class StopAfterBytes(Exception):
    pass


def download_single(ftp: FTP, remote_path: str, local_path: str, on_progress=None):
    os.makedirs(os.path.dirname(local_path) or '.', exist_ok=True)
    remote_size = try_size(ftp, remote_path)
    local_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0

    if remote_size is not None and local_size >= remote_size:
        # already complete
        if on_progress:
            on_progress(remote_size, remote_size, 0.0, 0.0)
        return

    # Open file for append/binary
    f = open(local_path, 'ab')
    transferred = local_size
    start_time = time.time()

    def _cb(chunk: bytes):
        nonlocal transferred, start_time
        f.write(chunk)
        transferred += len(chunk)
        if on_progress:
            elapsed = max(1e-6, time.time() - start_time)
            speed = (transferred - local_size) / elapsed  # bytes/s since (re)start
            eta = (remote_size - transferred) / speed if (remote_size and speed>0) else 0.0
            on_progress(transferred, remote_size or 0, speed, eta)

    # Use REST to resume if needed
    rest = local_size if local_size > 0 else None

    for attempt in range(1, DL_RETRIES+1):
        try:
            ftp.voidcmd('TYPE I')
            if rest:
                ftp.retrbinary(f'RETR {remote_path}', _cb, blocksize=BLOCKSIZE, rest=rest)
            else:
                ftp.retrbinary(f'RETR {remote_path}', _cb, blocksize=BLOCKSIZE)
            break  # success
        except all_errors as e:
            if attempt >= DL_RETRIES:
                raise
            # Reconnect and resume
            time.sleep(1.5 * attempt)
            # We need to re-login and set REST to current size
            try:
                # try to NOOP to see if alive
                ftp.voidcmd('NOOP')
            except all_errors:
                # caller must provide a fresh ftp when retrying externally; here we just continue
                pass
            rest = os.path.getsize(local_path)
            start_time = time.time()
    f.close()


def _download_segment(srv: FTPServer, remote_path: str, start: int, length: int, temp_path: str,
                       progress_cb=None, seg_id: int = 0):
    # Separate connection per segment
    ftp = mk_client(srv)
    try:
        received = 0
        os.makedirs(os.path.dirname(temp_path) or '.', exist_ok=True)
        with open(temp_path, 'wb') as f:
            t0 = time.time()

            def _cb(chunk: bytes):
                nonlocal received, t0
                remaining = length - received
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                f.write(chunk)
                received += len(chunk)
                if progress_cb:
                    elapsed = max(1e-6, time.time() - t0)
                    speed = received / elapsed
                    progress_cb(seg_id, received, length, speed)
                if received >= length:
                    # Abort the transfer gracefully by raising a sentinel exception
                    raise StopAfterBytes()

            ftp.voidcmd('TYPE I')
            ftp.retrbinary(f'RETR {remote_path}', _cb, blocksize=BLOCKSIZE, rest=start)
    except StopAfterBytes:
        pass
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def download_turbo(srv: FTPServer, remote_path: str, local_path: str, total_size: int,
                   threads: int, log_fn=None, progress_fn=None):
    # split into segments
    threads = max(1, min(MAX_TURBO_THREADS, threads))
    seg_size = math.ceil(total_size / threads)
    temp_parts = [f"{local_path}.part{i}" for i in range(threads)]

    # Worker wrapper with retries
    def worker(i: int, start: int, length: int):
        for attempt in range(1, DL_RETRIES+1):
            try:
                _download_segment(srv, remote_path, start, length, temp_parts[i],
                                  progress_cb=lambda seg, rec, ln, sp: progress_fn and progress_fn(seg, rec, ln, sp),
                                  seg_id=i)
                return
            except all_errors as e:
                if log_fn:
                    log_fn(f"[seg {i}] retry {attempt}/{DL_RETRIES} after error: {e}")
                time.sleep(1.5 * attempt)
        raise RuntimeError(f"Segment {i} failed after {DL_RETRIES} retries")

    # Launch threads
    ths = []
    for i in range(threads):
        start = i * seg_size
        length = min(seg_size, total_size - start)
        if length <= 0:
            continue
        t = threading.Thread(target=worker, args=(i, start, length), daemon=True)
        ths.append(t)
        t.start()

    # Wait
    for t in ths:
        t.join()

    # Merge
    with open(local_path, 'wb') as out:
        for i in range(threads):
            part = temp_parts[i]
            if not os.path.exists(part):
                continue
            with open(part, 'rb') as p:
                out.write(p.read())
            os.remove(part)


# ===================== GUI =====================
class App(tk.Tk):
    def __init__(self):
        self.current_path = "/"
        super().__init__()
        self.title("TurboFTP-GUI — trhacknon")
        self.geometry("1050x700")
        self.configure(bg="#0b0b0c")
        self._build_style()
        self._build_ui()
        self.ftp: Optional[FTP] = None
        self.server: Optional[FTPServer] = None
        self.current_path = "/"
        self.stop_flag = False

    def _build_style(self):
        self.accent = "#39FF14"  # neon green
        self.accent2 = "#00E5FF" # neon cyan
        self.bg2 = "#151515"
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TLabel', background=self['bg'], foreground=self.accent)
        style.configure('TButton', background=self.bg2, foreground=self.accent, padding=6)
        style.map('TButton', background=[('active', '#0f0f0f')])
        style.configure('Treeview', background=self.bg2, fieldbackground=self.bg2, foreground=self.accent)
        style.configure('Treeview.Heading', background='#101010', foreground=self.accent)
        style.configure('TProgressbar', background=self.accent, troughcolor='#111')

    def _row(self, parent, **grid):
        f = ttk.Frame(parent)
        f.configure(style='TFrame')
        f.grid(**grid)
        return f

    def _build_ui(self):
        top = self._row(self, row=0, column=0, sticky='ew', padx=10, pady=8)
        top.grid_columnconfigure(10, weight=1)

        # Connection fields
        self.host_var = tk.StringVar(value='ptiq-ftp.itron-hosting.com')
        self.port_var = tk.IntVar(value=21)
        self.user_var = tk.StringVar(value='eb_parquetecno')
        self.pass_var = tk.StringVar(value='EverBlu888')
        self.ftps_var = tk.BooleanVar(value=False)
        self.pasv_var = tk.BooleanVar(value=True)

        ttk.Label(top, text="Host").grid(row=0, column=0, padx=5)
        ttk.Entry(top, textvariable=self.host_var, width=28).grid(row=0, column=1)
        ttk.Label(top, text="Port").grid(row=0, column=2, padx=5)
        ttk.Entry(top, textvariable=self.port_var, width=6).grid(row=0, column=3)
        ttk.Label(top, text="User").grid(row=0, column=4, padx=5)
        ttk.Entry(top, textvariable=self.user_var, width=16).grid(row=0, column=5)
        ttk.Label(top, text="Pass").grid(row=0, column=6, padx=5)
        ttk.Entry(top, textvariable=self.pass_var, width=16, show='•').grid(row=0, column=7)
        ttk.Checkbutton(top, text="FTPS", variable=self.ftps_var).grid(row=0, column=8, padx=6)
        ttk.Checkbutton(top, text="PASV", variable=self.pasv_var).grid(row=0, column=9, padx=6)

        self.connect_btn = ttk.Button(top, text="Connect", command=self.connect)
        self.connect_btn.grid(row=0, column=10, padx=6, sticky='e')

        # Path bar
        pathbar = self._row(self, row=1, column=0, sticky='ew', padx=10)
        pathbar.grid_columnconfigure(1, weight=1)
        ttk.Label(pathbar, text="Remote").grid(row=0, column=0, padx=5)
        self.path_var = tk.StringVar(value=self.current_path)
        ttk.Entry(pathbar, textvariable=self.path_var).grid(row=0, column=1, sticky='ew')
        ttk.Button(pathbar, text="Go", command=self.refresh).grid(row=0, column=2, padx=4)
        ttk.Button(pathbar, text="↑ Up", command=self.go_up).grid(row=0, column=3, padx=4)
        ttk.Button(pathbar, text="Refresh", command=self.refresh).grid(row=0, column=4, padx=4)

        # Tree + actions
        center = self._row(self, row=2, column=0, sticky='nsew', padx=10, pady=8)
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        cols = ("name","type","size","modified")
        self.tree = ttk.Treeview(center, columns=cols, show='headings', height=16)
        for c, w in [("name",420),("type",80),("size",120),("modified",160)]:
            self.tree.heading(c, text=c.upper())
            self.tree.column(c, width=w, anchor='w')
        self.tree.grid(row=0, column=0, sticky='nsew')
        yscroll = ttk.Scrollbar(center, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        yscroll.grid(row=0, column=1, sticky='ns')
        center.grid_rowconfigure(0, weight=1)
        center.grid_columnconfigure(0, weight=1)

        self.tree.bind('<Double-1>', self.on_open)

        # Download panel
        dl = self._row(self, row=3, column=0, sticky='ew', padx=10, pady=(0,6))
        ttk.Label(dl, text="Local folder").grid(row=0, column=0, padx=5)
        self.local_dir = tk.StringVar(value=os.path.abspath("downloads"))
        ttk.Entry(dl, textvariable=self.local_dir, width=50).grid(row=0, column=1, sticky='ew')
        ttk.Button(dl, text="Browse", command=self.pick_local).grid(row=0, column=2, padx=4)

        self.turbo_var = tk.BooleanVar(value=False)
        self.turbo_threads = tk.IntVar(value=4)
        ttk.Checkbutton(dl, text="Turbo (multi-conn)", variable=self.turbo_var).grid(row=0, column=3, padx=8)
        ttk.Label(dl, text="Threads").grid(row=0, column=4)
        ttk.Spinbox(dl, from_=1, to=MAX_TURBO_THREADS, textvariable=self.turbo_threads, width=4).grid(row=0, column=5, padx=4)
        self.dl_btn = ttk.Button(dl, text="Download selected", command=self.download_selected)
        self.dl_btn.grid(row=0, column=6, padx=8)

        # Progress + log
        prog = self._row(self, row=4, column=0, sticky='ew', padx=10)
        self.progbar = ttk.Progressbar(prog, mode='determinate')
        self.progbar.grid(row=0, column=0, sticky='ew')
        prog.grid_columnconfigure(0, weight=1)

        stats = self._row(self, row=5, column=0, sticky='ew', padx=10)
        self.stat_var = tk.StringVar(value="Idle")
        ttk.Label(stats, textvariable=self.stat_var).grid(row=0, column=0, sticky='w')

        # Log box
        logf = self._row(self, row=6, column=0, sticky='nsew', padx=10, pady=(4,10))
        self.grid_rowconfigure(6, weight=1)
        self.log = tk.Text(logf, height=10, bg="#131313", fg=self.accent, insertbackground=self.accent)
        self.log.grid(row=0, column=0, sticky='nsew')
        log_scroll = ttk.Scrollbar(logf, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        log_scroll.grid(row=0, column=1, sticky='ns')
        logf.grid_rowconfigure(0, weight=1)
        logf.grid_columnconfigure(0, weight=1)

    # ---------- GUI callbacks ----------
    def log_msg(self, msg: str):
        self.log.insert('end', msg + "\n")
        self.log.see('end')

    def connect(self):
        if self.ftp:
            try:
                self.ftp.quit()
            except Exception:
                pass
            self.ftp = None
        self.server = FTPServer(
            host=self.host_var.get().strip(),
            port=int(self.port_var.get()),
            user=self.user_var.get().strip(),
            password=self.pass_var.get(),
            use_ftps=self.ftps_var.get(),
            passive=self.pasv_var.get(),
        )
        try:
            self.ftp = mk_client(self.server)
            self.current_path = try_pwd(self.ftp)
            self.path_var.set(self.current_path)
            self.log_msg(f"Connected to {self.server.host}:{self.server.port} (PASV={self.server.passive}, FTPS={self.server.use_ftps})")
            self.refresh()
        except all_errors as e:
            messagebox.showerror("Connect error", str(e))

    def refresh(self):
        if not self.ftp:
            return
        path = self.path_var.get().strip() or "/"
        try:
            items = list_dir(self.ftp, path)
            self.tree.delete(*self.tree.get_children())
            for name, facts in items:
                t = facts['type']
                size = human_bytes(facts.get('size'))
                mod = facts.get('modify') or ''
                self.tree.insert('', 'end', values=(name, t, size, mod))
            self.current_path = path
            self.path_var.set(path)
        except all_errors as e:
            self.log_msg(f"List error: {e}")

    def on_open(self, event=None):
        sel = self.tree.focus()
        if not sel:
            return
        name, typ, *_ = self.tree.item(sel, 'values')
        if typ == 'dir':
            # enter directory
            newp = self._join(self.current_path, name)
            self.path_var.set(newp)
            self.refresh()

    def go_up(self):
        if self.current_path == '/':
            return
        newp = os.path.dirname(self.current_path.rstrip('/')) or '/'
        self.path_var.set(newp)
        self.refresh()

    def pick_local(self):
        d = filedialog.askdirectory(title="Choose local download folder")
        if d:
            self.local_dir.set(d)

    def _join(self, a: str, b: str) -> str:
        if a.endswith('/'):
            return a + b
        if a == '/':
            return '/' + b
        return a + '/' + b

    def download_selected(self):
        if not self.ftp:
            return
        sel = self.tree.focus()
        if not sel:
            messagebox.showinfo("Info", "Select a file to download (double-click folders to open).")
            return
        name, typ, *_ = self.tree.item(sel, 'values')
        if typ == 'dir':
            messagebox.showinfo("Info", "Please select a FILE, not a folder.")
            return
        remote = self._join(self.current_path, name)
        local = os.path.join(self.local_dir.get(), name)
        os.makedirs(self.local_dir.get(), exist_ok=True)

        # Get remote size
        try:
            size = try_size(self.ftp, remote)
        except all_errors:
            size = None
        if size is None:
            if not messagebox.askyesno("Unknown size", "Server did not return file size. Proceed anyway?"):
                return

        # Start download in background thread
        self.stop_flag = False
        self.progbar['value'] = 0
        self.progbar['maximum'] = size or 1
        self.stat_var.set("Starting download…")

        def progress(transferred, total, speed, eta):
            total_nonzero = total if total else max(transferred, 1)
            self.progbar['maximum'] = total_nonzero
            self.progbar['value'] = min(transferred, total_nonzero)
            sp = human_bytes(int(speed)) + "/s" if speed else "?"
            if total:
                pct = 100.0 * transferred / total
                eta_str = time.strftime('%H:%M:%S', time.gmtime(max(0, int(eta)))) if eta else "--:--:--"
                self.stat_var.set(f"{pct:5.1f}%  {human_bytes(transferred)} / {human_bytes(total)}  |  {sp}  ETA {eta_str}")
            else:
                self.stat_var.set(f"{human_bytes(transferred)}  |  {sp}")
            self.update_idletasks()

        def turbo_progress(seg_id, received, length, speed):
            # aggregate per segment -> naive: sum temp part sizes
            total_recv = 0
            for i in range(self.turbo_threads.get()):
                part = f"{local}.part{i}"
                if os.path.exists(part):
                    total_recv += os.path.getsize(part)
            self.progbar['maximum'] = size or max(total_recv, 1)
            self.progbar['value'] = min(total_recv, self.progbar['maximum'])
            self.stat_var.set(f"Turbo DL… {human_bytes(total_recv)} / {human_bytes(size)}")
            self.update_idletasks()

        def worker():
            try:
                if self.turbo_var.get() and size and size > BLOCKSIZE*10:
                    self.log_msg(f"Turbo download with {self.turbo_threads.get()} threads…")
                    download_turbo(self.server, remote, local, size, self.turbo_threads.get(),
                                   log_fn=self.log_msg, progress_fn=turbo_progress)
                else:
                    self.log_msg("Single-stream resumable download…")
                    download_single(self.ftp, remote, local, on_progress=progress)
                self.stat_var.set("Download complete ✔")
                self.log_msg(f"Saved to: {local}")
            except Exception as e:
                self.stat_var.set("Download failed ✖")
                self.log_msg(f"Error: {e}")

        threading.Thread(target=worker, daemon=True).start()


if __name__ == '__main__':
    App().mainloop()
