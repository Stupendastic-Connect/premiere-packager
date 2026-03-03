#!/usr/bin/env python3
"""
gui.py - Interfaz grafica para Premiere Packager.

Flujo:  Carpeta base  ->  Configurar  ->  Empaquetar
Atajos: Ctrl+Enter = Empaquetar | Escape = Cancelar | Ctrl+L = Limpiar log | Ctrl+F = Buscar
"""

import json
import logging
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / ".packager_gui.json"

sys.path.insert(0, str(SCRIPT_DIR))
from empaquetar_premiere import package_project, parse_path_mappings

LIMIT_N = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import re

# Carpetas que se consideran "archivo" y se ignoran al buscar .prproj
_ARCHIVE_NAMES = {"antic", "old", "backup", "archive", "antiguo", "bak", "prev"}

# Patron para backups de Premiere: nombre_1.prproj, nombre_22.prproj
_BACKUP_SUFFIX = re.compile(r"_\d+$")

# Patron para detectar carpetas numeradas: "1. Material", "2. Projects", "10- Renders"
_NUMBERED_DIR = re.compile(r"^\d+[\.\-\s]")


def _pick_best_prproj(group: list[Path]) -> Path:
    """De una lista de .prproj del mismo proyecto, elige el mas reciente."""
    try:
        return max(group, key=lambda p: p.stat().st_mtime)
    except OSError:
        return group[0]


def _find_project_root(prproj: Path) -> Path:
    """Sube desde el .prproj hasta encontrar la carpeta del proyecto.

    Busca la carpeta numerada mas alta en un grupo contiguo de carpetas
    numeradas y devuelve su padre.  Estructura tipica:
        Villa Rosita / 2. Projects / 2. Premiere / archivo.prproj
        Villa Rosita / 2. Projects / archivo.prproj
    En ambos casos devuelve Villa Rosita.

    Se detiene al encontrar una carpeta no numerada despues de haber visto
    una numerada, para no subir mas alla de la raiz del proyecto
    (ej. evitar que '3. Trabajo/Clientes/Proyecto/2. Projects/' suba hasta
    '3. Trabajo' y devuelva la raiz del disco).
    """
    topmost_numbered = None
    found_numbered = False
    current = prproj.parent
    for _ in range(5):
        if current == current.parent:
            break
        if _NUMBERED_DIR.match(current.name):
            topmost_numbered = current
            found_numbered = True
        elif found_numbered:
            # Carpeta no numerada despues de una numerada: la raiz esta aqui
            break
        current = current.parent

    if topmost_numbered is not None:
        return topmost_numbered.parent
    return prproj.parent


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------

def _load_cfg() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _save_cfg(cfg: dict):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Dialogo selector de secuencia
# ---------------------------------------------------------------------------

class SequencePickerDialog(tk.Toplevel):
    """Popup para elegir secuencia de un proyecto."""

    def __init__(self, parent, prproj_name: str,
                 ranked: list[tuple[dict, float]]):
        super().__init__(parent)
        self.result: dict | None = None
        self.ranked = ranked
        self.transient(parent)
        self.grab_set()
        self.title(f"Elegir secuencia - {prproj_name}")
        self.resizable(True, True)

        # ── UI ──
        f = ttk.Frame(self, padding=(12, 10))
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="Selecciona la secuencia a empaquetar:",
                  font=("Segoe UI", 10)).pack(anchor=tk.W, pady=(0, 6))

        # Treeview con columnas
        cols = ("name", "clips", "tracks", "nested", "score")
        tf = ttk.Frame(f)
        tf.pack(fill=tk.BOTH, expand=True)

        self.tree = ttk.Treeview(
            tf, columns=cols, show="headings",
            height=min(len(ranked), 12), selectmode="browse",
        )
        self.tree.heading("name", text="Nombre", anchor=tk.W)
        self.tree.heading("clips", text="Clips", anchor=tk.E)
        self.tree.heading("tracks", text="Pistas", anchor=tk.E)
        self.tree.heading("nested", text="Anidadas", anchor=tk.E)
        self.tree.heading("score", text="Score", anchor=tk.E)
        self.tree.column("name", width=280, minwidth=120)
        self.tree.column("clips", width=60, minwidth=40, anchor=tk.E)
        self.tree.column("tracks", width=60, minwidth=40, anchor=tk.E)
        self.tree.column("nested", width=70, minwidth=40, anchor=tk.E)
        self.tree.column("score", width=60, minwidth=40, anchor=tk.E)

        sb = ttk.Scrollbar(tf, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        for i, (info, sc) in enumerate(ranked):
            total_tracks = info["video_tracks"] + info["audio_tracks"]
            self.tree.insert(
                "", tk.END, iid=str(i),
                values=(info["name"], info["clip_count"], total_tracks,
                        info["nested_count"], f"{sc:.2f}"),
            )

        # Seleccionar la primera (mejor score)
        if ranked:
            self.tree.selection_set("0")
            self.tree.focus("0")

        # Botones
        bf = ttk.Frame(f)
        bf.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(bf, text="Seleccionar", command=self._ok).pack(
            side=tk.LEFT, padx=(0, 8))
        ttk.Button(bf, text="Saltar proyecto", command=self._skip).pack(
            side=tk.LEFT)

        # Hint
        ttk.Label(bf, text="Doble-click o Enter para seleccionar",
                  foreground="#777", font=("Segoe UI", 8)).pack(
            side=tk.RIGHT)

        # Bindings
        self.tree.bind("<Double-1>", lambda e: self._ok())
        self.tree.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._skip())

        # Centrar sobre parent
        self.update_idletasks()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        px = parent.winfo_x()
        py = parent.winfo_y()
        w = max(self.winfo_reqwidth(), 580)
        h = max(self.winfo_reqheight(), 300)
        x = px + (pw - w) // 2
        y = py + (ph - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

        self.tree.focus_set()

    def _ok(self):
        sel = self.tree.selection()
        if sel:
            idx = int(sel[0])
            self.result = self.ranked[idx][0]
        self.grab_release()
        self.destroy()

    def _skip(self):
        self.result = None
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------------------
# Log handler -> GUI
# ---------------------------------------------------------------------------

class _GuiHandler(logging.Handler):
    def __init__(self, cb):
        super().__init__()
        self.cb = cb

    def emit(self, record):
        self.cb(self.format(record))


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class App:

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Premiere Packager")
        root.minsize(720, 520)

        self.projects: list[tuple[str, Path]] = []
        self.running = False
        self._cancel_flag = False
        self._scan_id: str | None = None
        self._scanning = False
        self._scan_cancel = False
        self._timer_id: str | None = None
        self._start_time: float = 0

        # Checkboxes y filtro de busqueda
        self._checked: dict[str, bool] = {}
        self._detached: set[str] = set()
        self._all_iids: list[str] = []  # orden original de iids

        # Sincronizacion para dialogo de secuencia desde worker thread
        self._seq_event = threading.Event()
        self._seq_ranked: list = []
        self._seq_prproj_name: str = ""
        self._seq_result: dict | None = None

        self._style()
        self._ui()
        self._keys()
        self._load()
        self.root.after(100, self.src_entry.focus_set)

    # ── Styles ────────────────────────────────────────────

    def _style(self):
        s = ttk.Style()
        for t in ("vista", "clam"):
            if t in s.theme_names():
                s.theme_use(t)
                break

        s.configure("H1.TLabel", font=("Segoe UI", 14, "bold"))
        s.configure("Dim.TLabel", foreground="#777", font=("Segoe UI", 9))
        s.configure("Preview.TLabel", foreground="#555", font=("Consolas", 9))
        s.configure("Run.TButton", font=("Segoe UI", 10, "bold"), padding=(20, 8))
        s.configure("Small.TButton", padding=(6, 2))

    # ── UI ────────────────────────────────────────────────

    def _ui(self):
        m = ttk.Frame(self.root, padding=(16, 12, 16, 10))
        m.pack(fill=tk.BOTH, expand=True)

        # ── Header ──
        ttk.Label(m, text="Premiere Packager", style="H1.TLabel").pack(
            anchor=tk.W, pady=(0, 10))

        # ── Carpeta base ──
        r = ttk.Frame(m)
        r.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(r, text="Carpeta base:", width=12).pack(side=tk.LEFT)
        self.src_var = tk.StringVar()
        self.src_entry = ttk.Entry(r, textvariable=self.src_var,
                                    font=("Segoe UI", 10))
        self.src_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ttk.Button(r, text="Examinar\u2026", command=self._browse).pack(
            side=tk.LEFT)
        self.src_var.trace_add("write", lambda *_: self._schedule_scan())

        # ── Proyectos ──
        tree_frame = ttk.LabelFrame(m, text="  Proyectos encontrados  ",
                                     padding=(6, 4))
        tree_frame.pack(fill=tk.X, pady=(0, 6))

        # Barra de busqueda
        sf = ttk.Frame(tree_frame)
        sf.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(sf, text="\U0001f50d", font=("Segoe UI", 10)).pack(
            side=tk.LEFT, padx=(0, 4))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(
            sf, textvariable=self.search_var, font=("Segoe UI", 10))
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._search_clear_btn = ttk.Button(
            sf, text="\u2715", width=3, command=self._clear_search,
            style="Small.TButton")
        self._search_clear_btn.pack(side=tk.LEFT, padx=(4, 0))
        self.search_var.trace_add("write", lambda *_: self._apply_filter())

        tf = ttk.Frame(tree_frame)
        tf.pack(fill=tk.BOTH, expand=True)

        self.tree = ttk.Treeview(
            tf, columns=("check", "path", "status"), show="headings",
            height=4, selectmode="browse",
        )
        self.tree.heading("check", text="\u2611", anchor=tk.CENTER)
        self.tree.heading("path", text="Archivo", anchor=tk.W)
        self.tree.heading("status", text="Estado", anchor=tk.W)
        self.tree.column("check", width=30, minwidth=30, anchor=tk.CENTER,
                         stretch=False)
        self.tree.column("path", width=400, minwidth=180)
        self.tree.column("status", width=180, minwidth=80, anchor=tk.W)

        sb = ttk.Scrollbar(tf, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)

        self.tree.tag_configure("pending", foreground="#999")
        self.tree.tag_configure("active", foreground="#1565c0")
        self.tree.tag_configure("done", foreground="#2e7d32")
        self.tree.tag_configure("error", foreground="#c62828")
        self.tree.tag_configure("skip", foreground="#e65100")

        self.count_var = tk.StringVar()
        ttk.Label(tree_frame, textvariable=self.count_var,
                  style="Dim.TLabel").pack(anchor=tk.W, pady=(3, 0))

        # ── Nombre salida ──
        r = ttk.Frame(m)
        r.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(r, text="Carpeta salida:", width=12).pack(side=tk.LEFT)
        self.out_var = tk.StringVar(value="Empaquetado")
        self.out_combo = ttk.Combobox(
            r, textvariable=self.out_var,
            values=["Empaquetado", "ENTREGA", "Paquete", "Packaged"],
            width=22, font=("Segoe UI", 10),
        )
        self.out_combo.pack(side=tk.LEFT, padx=(0, 10))
        self.preview_var = tk.StringVar()
        ttk.Label(r, textvariable=self.preview_var,
                  style="Preview.TLabel").pack(side=tk.LEFT)
        self.out_var.trace_add("write", lambda *_: (self._update_preview(), self._schedule_scan()))

        # ── Opciones ──
        r = ttk.Frame(m)
        r.pack(fill=tk.X, pady=(2, 0))

        self.dry_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="Dry Run (no copia, solo muestra)",
                         variable=self.dry_var).pack(side=tk.LEFT, padx=(0, 16))

        self.limit_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text=f"Limite ({LIMIT_N} proyectos)",
                         variable=self.limit_var).pack(side=tk.LEFT, padx=(0, 16))
        self.limit_var.trace_add("write", lambda *_: self._update_btn())

        self.auto_seq_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text="Auto-seleccionar secuencia",
                         variable=self.auto_seq_var).pack(
            side=tk.LEFT, padx=(0, 16))

        # ── Opciones linea 2 ──
        r2 = ttk.Frame(m)
        r2.pack(fill=tk.X, pady=(2, 0))

        self.skip_done_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="Omitir ya empaquetados",
                         variable=self.skip_done_var).pack(
            side=tk.LEFT, padx=(0, 16))
        self.skip_done_var.trace_add("write", lambda *_: self._schedule_scan())

        self.autosave_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="Incluir Auto-Save",
                         variable=self.autosave_var).pack(side=tk.LEFT, padx=(0, 16))
        self.autosave_var.trace_add("write", lambda *_: self._schedule_scan())

        self._adv_open = False
        self._adv_btn = ttk.Button(
            r2, text="\u25b8 Mapeos Mac\u2192Win",
            command=self._toggle_adv, style="Small.TButton",
        )
        self._adv_btn.pack(side=tk.LEFT)

        # Mapeos (oculto)
        self._adv_frame = ttk.Frame(m, padding=(8, 4, 0, 0))
        r3 = ttk.Frame(self._adv_frame)
        r3.pack(fill=tk.X, pady=(0, 3))
        ttk.Label(r3, text="Mac:").pack(side=tk.LEFT)
        self.mac_var = tk.StringVar()
        ttk.Entry(r3, textvariable=self.mac_var, width=22).pack(
            side=tk.LEFT, padx=(4, 8))
        ttk.Label(r3, text="Win:").pack(side=tk.LEFT)
        self.win_var = tk.StringVar()
        self._win_entry = ttk.Entry(r3, textvariable=self.win_var, width=22)
        self._win_entry.pack(side=tk.LEFT, padx=(4, 8))
        ttk.Button(r3, text="+", width=3, command=self._add_map).pack(
            side=tk.LEFT)
        ttk.Button(r3, text="\u2212", width=3, command=self._rm_map).pack(
            side=tk.LEFT, padx=(2, 0))
        self.map_list = tk.Listbox(self._adv_frame, height=2,
                                    font=("Consolas", 9), activestyle="dotbox")
        self.map_list.pack(fill=tk.X)

        # ── Separador ──
        ttk.Separator(m, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(10, 0))

        # ── Botones + progreso ──
        bf = ttk.Frame(m)
        bf.pack(fill=tk.X, pady=(8, 0))

        self.run_btn = ttk.Button(
            bf, text="\u25b6  EMPAQUETAR", command=self._run,
            style="Run.TButton",
        )
        self.run_btn.pack(side=tk.LEFT)

        self.cancel_btn = ttk.Button(
            bf, text="\u25a0  Cancelar", command=self._do_cancel,
            state=tk.DISABLED,
        )
        self.cancel_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.timer_var = tk.StringVar()
        ttk.Label(bf, textvariable=self.timer_var,
                  style="Dim.TLabel").pack(side=tk.RIGHT, padx=(8, 0))
        self.progress_label = tk.StringVar()
        ttk.Label(bf, textvariable=self.progress_label,
                  font=("Segoe UI", 9, "bold"), foreground="#555").pack(
            side=tk.RIGHT)

        self.pbar = ttk.Progressbar(m, mode="determinate")
        self.pbar.pack(fill=tk.X, pady=(6, 0))

        # ── Log ──
        log_header = ttk.Frame(m)
        log_header.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(log_header, text="Log", font=("Segoe UI", 9, "bold"),
                  foreground="#444").pack(side=tk.LEFT)
        ttk.Button(log_header, text="Limpiar", command=self._log_clear,
                   style="Small.TButton").pack(side=tk.RIGHT)

        log_frame = ttk.Frame(m)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        self.log = tk.Text(
            log_frame, wrap=tk.WORD, font=("Consolas", 10),
            bg="#0d1117", fg="#c9d1d9",
            insertbackground="#c9d1d9",
            selectbackground="#264f78", selectforeground="#ffffff",
            padx=10, pady=8,
            state=tk.DISABLED, borderwidth=1, highlightthickness=0,
            relief=tk.SOLID,
        )
        lsb = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.pack(fill=tk.BOTH, expand=True)

        flog = ("Consolas", 10)
        self.log.tag_configure("head", foreground="#58a6ff", font=(*flog, "bold"))
        self.log.tag_configure("sep", foreground="#484f58")
        self.log.tag_configure("seq", foreground="#d2a8ff")
        self.log.tag_configure("info", foreground="#8b949e")
        self.log.tag_configure("ok", foreground="#7ee787")
        self.log.tag_configure("warn", foreground="#d29922")
        self.log.tag_configure("err", foreground="#f85149", font=(*flog, "bold"))
        self.log.tag_configure("file", foreground="#79c0ff")
        self.log.tag_configure("dim", foreground="#484f58")
        self.log.tag_configure("done", foreground="#3fb950", font=(*flog, "bold"))

    # ── Keys ──────────────────────────────────────────────

    def _keys(self):
        self.root.bind("<Control-Return>", lambda e: self._run())
        self.root.bind("<Escape>", lambda e: self._do_cancel())
        self.root.bind("<Control-l>", lambda e: self._log_clear())
        self.root.bind("<Control-L>", lambda e: self._log_clear())
        self.root.bind("<Control-f>", lambda e: self.search_entry.focus_set())
        self.root.bind("<Control-F>", lambda e: self.search_entry.focus_set())
        self._win_entry.bind("<Return>", lambda e: self._add_map())
        self.map_list.bind("<Delete>", lambda e: self._rm_map())

    # ── Config ────────────────────────────────────────────

    def _load(self):
        c = _load_cfg()
        if c.get("src"):
            self.src_var.set(c["src"])
        if c.get("out"):
            self.out_var.set(c["out"])
        if c.get("dry") is not None:
            self.dry_var.set(c["dry"])
        if c.get("limit") is not None:
            self.limit_var.set(c["limit"])
        if c.get("auto_seq") is not None:
            self.auto_seq_var.set(c["auto_seq"])
        if c.get("skip_done") is not None:
            self.skip_done_var.set(c["skip_done"])
        if c.get("autosave") is not None:
            self.autosave_var.set(c["autosave"])
        default_maps = [
            "/Volumes/SEGUIMIENTOS=V:",
            "/Volumes/NAS-Dropbox/DATA/SEGUIMIENTOS=V:",
            "/Volumes/NAS-Dropbox/DropboxStupendastic 2023=Z:",
            "/Volumes/NAS-Dropbox=Z:",
            "/Volumes/Dropbox-Stupendastic=Z:",
        ]
        saved_maps = c.get("maps", [])
        for mp in saved_maps:
            self.map_list.insert(tk.END, mp)
        for dm in default_maps:
            if dm not in saved_maps:
                self.map_list.insert(tk.END, dm)
        if c.get("geo"):
            try:
                self.root.geometry(c["geo"])
            except tk.TclError:
                pass

    def _save(self):
        maps = [self.map_list.get(i) for i in range(self.map_list.size())]
        _save_cfg({
            "src": self.src_var.get(), "out": self.out_var.get(),
            "dry": self.dry_var.get(), "limit": self.limit_var.get(),
            "auto_seq": self.auto_seq_var.get(), "skip_done": self.skip_done_var.get(),
            "autosave": self.autosave_var.get(),
            "maps": maps, "geo": self.root.geometry(),
        })

    # ── Browse ────────────────────────────────────────────

    def _browse(self):
        p = filedialog.askdirectory(
            title="Selecciona la carpeta raiz con los proyectos")
        if p:
            self.src_var.set(p)

    # ── Scan ──────────────────────────────────────────────

    def _schedule_scan(self):
        if self._scan_id:
            self.root.after_cancel(self._scan_id)
        self._scan_cancel = True  # Cancel any running scan thread
        self._scan_id = self.root.after(400, self._scan)

    def _scan(self):
        self._scan_id = None
        # Re-attach detached items before deleting
        for iid in list(self._detached):
            try:
                self.tree.reattach(iid, "", tk.END)
            except tk.TclError:
                pass
        self._detached.clear()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.projects.clear()
        self._checked.clear()
        self._all_iids.clear()
        self.search_var.set("")

        src = self.src_var.get().strip().strip('"').strip("'")
        if not src:
            self.count_var.set("")
            self._update_btn()
            self._update_preview()
            return

        base = Path(src)

        if base.is_file() and base.suffix.lower() == ".prproj":
            self.projects.append((base.name, base))
            self._checked["0"] = True
            self._all_iids = ["0"]
            self.tree.insert("", tk.END, iid="0",
                              values=("\u2611", base.name, "Listo"),
                              tags=("pending",))
            self.count_var.set("1 proyecto (archivo individual)")
            self._update_btn()
            self._update_preview()
            return

        if not base.is_dir():
            self.count_var.set("Ruta no encontrada" if src else "")
            self._update_btn()
            self._update_preview()
            return

        # Lanzar escaneo en hilo para no bloquear la UI
        self._scanning = True
        self._scan_cancel = False
        self.pbar.configure(mode="indeterminate")
        self.pbar.start(15)
        self.count_var.set("Buscando proyectos\u2026")
        self.progress_label.set("Escaneando\u2026")
        self._update_btn()
        include_autosave = self.autosave_var.get()
        out_name = self.out_var.get().strip()
        threading.Thread(
            target=self._scan_worker,
            args=(base, include_autosave, out_name),
            daemon=True,
        ).start()

    def _scan_worker(self, base: Path, include_autosave: bool, out_name: str):
        """Hilo que escanea .prproj con os.walk y poda de directorios."""
        found: list[Path] = []
        base_str = str(base)
        skip_dirs = _ARCHIVE_NAMES | {"node_modules", ".git", "__pycache__"}
        if out_name:
            skip_dirs.add(out_name.lower())
        try:
            for dirpath, dirnames, filenames in os.walk(base_str):
                if self._scan_cancel:
                    return
                dirnames[:] = [
                    d for d in dirnames
                    if d.lower() not in skip_dirs
                    and (include_autosave
                         or d != "Adobe Premiere Pro Auto-Save")
                ]
                for fname in filenames:
                    if fname.lower().endswith(".prproj"):
                        found.append(Path(dirpath, fname))
                        if len(found) % 5 == 0:
                            self.root.after(0, self._scan_progress, len(found))
        except OSError:
            self.root.after(0, self._scan_finish, base, [])
            return
        found.sort()
        self.root.after(0, self._scan_finish, base, found)

    def _scan_progress(self, n: int):
        """Actualiza UI con progreso del escaneo (llamado en hilo principal)."""
        if self._scan_cancel:
            return
        self.count_var.set(f"Buscando proyectos\u2026 {n} archivos .prproj")
        self.progress_label.set(f"{n} encontrados")

    def _scan_finish(self, base: Path, found: list[Path]):
        """Callback en hilo principal: agrupa, filtra y muestra resultados."""
        self.pbar.stop()
        self.pbar.configure(mode="determinate", value=0)
        self.progress_label.set("")
        self._scanning = False
        if self._scan_cancel:
            return

        self.count_var.set("Agrupando proyectos\u2026")

        # Agrupar por proyecto (project root) y elegir el mejor .prproj
        from collections import OrderedDict
        groups: OrderedDict[str, list[Path]] = OrderedDict()
        for p in found:
            root = _find_project_root(p)
            key = str(root)
            groups.setdefault(key, []).append(p)

        best: list[Path] = [_pick_best_prproj(g) for g in groups.values()]

        # Filtrar proyectos ya empaquetados
        skipped_done = 0
        if self.skip_done_var.get():
            out_name = self.out_var.get().strip()
            if out_name:
                filtered = []
                for p in best:
                    root = _find_project_root(p)
                    if (root / out_name).is_dir():
                        skipped_done += 1
                    else:
                        filtered.append(p)
                best = filtered

        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.projects.clear()
        self._checked.clear()
        self._all_iids.clear()

        for i, prproj in enumerate(best):
            try:
                rel = str(prproj.relative_to(base))
            except ValueError:
                rel = prproj.name
            iid = str(i)
            self.projects.append((rel, prproj))
            self._checked[iid] = True
            self._all_iids.append(iid)
            self.tree.insert("", tk.END, iid=iid,
                              values=("\u2611", rel, "Listo"),
                              tags=("pending",))

        n = len(best)
        total = len(found)
        parts = []
        if total != n + skipped_done:
            parts.append(f"de {total} archivos")
        if skipped_done:
            parts.append(f"{skipped_done} ya empaquetado{'s' if skipped_done != 1 else ''}")
        extra = f" ({', '.join(parts)})" if parts else ""
        self.count_var.set(
            f"{n} proyecto{'s' if n != 1 else ''}{extra}")
        self._update_btn()
        self._update_preview()

    # ── Preview ───────────────────────────────────────────

    def _update_preview(self):
        out = self.out_var.get().strip()
        if not self.projects or not out:
            self.preview_var.set("")
            return
        _, prproj = self.projects[0]
        project_root = _find_project_root(prproj)
        self.preview_var.set(f"Ej: .../{project_root.name}/{out}/")

    def _update_btn(self):
        if self._detached:
            # Con filtro activo: contar solo checked + visibles
            n = sum(1 for iid in self._all_iids
                    if self._checked.get(iid, True) and iid not in self._detached)
        else:
            n = sum(1 for iid in self._all_iids if self._checked.get(iid, True))
        if self.limit_var.get():
            n = min(n, LIMIT_N)
        if self._scanning:
            self.run_btn.configure(text="\u25b6  EMPAQUETAR")
            self.run_btn.state(["disabled"])
        elif n == 0:
            self.run_btn.configure(text="\u25b6  EMPAQUETAR")
            self.run_btn.state(["disabled"])
        elif n == 1:
            self.run_btn.configure(text="\u25b6  EMPAQUETAR 1 PROYECTO")
            self.run_btn.state(["!disabled"])
        else:
            self.run_btn.configure(text=f"\u25b6  EMPAQUETAR {n} PROYECTOS")
            self.run_btn.state(["!disabled"])

    # ── Sequence picker (thread-safe) ─────────────────────

    def _ask_sequence(self, ranked: list[tuple[dict, float]]) -> dict | None:
        """Llamado desde el worker thread. Muestra dialogo en el hilo principal
        y espera la respuesta."""
        self._seq_ranked = ranked
        self._seq_event.clear()

        # Pedir al hilo principal que muestre el dialogo
        self.root.after(0, self._show_seq_dialog)
        # Esperar a que el usuario elija
        self._seq_event.wait()

        return self._seq_result

    def _show_seq_dialog(self):
        """Se ejecuta en el hilo principal de tkinter."""
        dlg = SequencePickerDialog(
            self.root, self._seq_prproj_name, self._seq_ranked)
        self.root.wait_window(dlg)
        self._seq_result = dlg.result
        self._seq_event.set()

    # ── Advanced ──────────────────────────────────────────

    def _toggle_adv(self):
        if self._adv_open:
            self._adv_frame.pack_forget()
            self._adv_btn.configure(text="\u25b8 Mapeos Mac\u2192Win")
            self._adv_open = False
        else:
            self._adv_frame.pack(fill=tk.X, after=self._adv_btn.master)
            self._adv_btn.configure(text="\u25be Mapeos Mac\u2192Win")
            self._adv_open = True

    def _add_map(self):
        mac = self.mac_var.get().strip()
        win = self.win_var.get().strip()
        if mac and win:
            self.map_list.insert(tk.END, f"{mac}={win}")
            self.mac_var.set("")
            self.win_var.set("")

    def _rm_map(self):
        sel = self.map_list.curselection()
        if sel:
            self.map_list.delete(sel[0])

    # ── Checkboxes ─────────────────────────────────────────

    def _on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        col = self.tree.identify_column(event.x)
        if col != "#1":  # solo columna check
            return
        if region == "heading":
            self._toggle_all()
        elif region in ("cell", "tree"):
            iid = self.tree.identify_row(event.y)
            if iid:
                self._toggle_check(iid)

    def _toggle_check(self, iid: str):
        self._checked[iid] = not self._checked.get(iid, True)
        self._update_check_display(iid)
        self._update_count()
        self._update_btn()

    def _toggle_all(self):
        visible = [iid for iid in self._all_iids if iid not in self._detached]
        any_unchecked = any(not self._checked.get(iid, True) for iid in visible)
        if any_unchecked:
            # Marcar solo los visibles
            for iid in visible:
                self._checked[iid] = True
                self._update_check_display(iid)
        else:
            # Desmarcar TODOS (visibles + ocultos)
            for iid in self._all_iids:
                self._checked[iid] = False
                if iid not in self._detached:
                    self._update_check_display(iid)
        self._update_count()
        self._update_btn()

    def _update_check_display(self, iid: str):
        vals = list(self.tree.item(iid, "values"))
        if vals:
            vals[0] = "\u2611" if self._checked.get(iid, True) else "\u2610"
            self.tree.item(iid, values=vals)

    # ── Filtro de busqueda ────────────────────────────────

    def _apply_filter(self):
        query = self.search_var.get().strip().lower()
        for iid in self._all_iids:
            idx = int(iid)
            display = self.projects[idx][0].lower() if idx < len(self.projects) else ""
            if query and query not in display:
                if iid not in self._detached:
                    self.tree.detach(iid)
                    self._detached.add(iid)
            else:
                if iid in self._detached:
                    # Reattach en orden correcto
                    self._detached.discard(iid)
                    self._reattach_in_order(iid)
        self._update_count()
        self._update_btn()

    def _reattach_in_order(self, iid: str):
        """Reattach un iid en su posicion original relativa."""
        idx = self._all_iids.index(iid)
        # Buscar el iid anterior visible para insertar despues
        prev_visible = None
        for j in range(idx - 1, -1, -1):
            candidate = self._all_iids[j]
            if candidate not in self._detached:
                prev_visible = candidate
                break
        if prev_visible is None:
            self.tree.reattach(iid, "", 0)
        else:
            children = list(self.tree.get_children(""))
            if prev_visible in children:
                pos = children.index(prev_visible) + 1
            else:
                pos = tk.END
            self.tree.reattach(iid, "", pos)

    def _clear_search(self):
        self.search_var.set("")
        self.search_entry.focus_set()

    def _update_count(self):
        """Actualiza el contador reflejando seleccion y filtro."""
        total = len(self.projects)
        if total == 0:
            return
        checked = sum(1 for iid in self._all_iids if self._checked.get(iid, True))
        visible = total - len(self._detached)
        query = self.search_var.get().strip()
        if query:
            self.count_var.set(
                f"{visible} mostrados, {checked}/{total} seleccionados")
        else:
            self.count_var.set(f"{checked}/{total} seleccionados")

    # ── Log ───────────────────────────────────────────────

    def _log_write(self, text: str, tag: str = ""):
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text + "\n", (tag,) if tag else ())
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _log_auto(self, text: str):
        s = text.lstrip()
        tag = ""

        if "[COPIADO]" in text or "[COPIARIA]" in text:
            tag = "ok"
        elif "guardado" in text.lower():
            tag = "done"
        elif "[OFFLINE]" in text:
            tag = "warn"
        elif "Omitidos" in text or "no encontrad" in text:
            tag = "warn"
        elif "[ERROR]" in text or "ERROR" in text:
            tag = "err"
        elif "Auto-seleccionada:" in text or "Secuencia:" in text:
            tag = "seq"
        elif "Medios de esta" in text or "Medios del proyecto" in text:
            tag = "info"
        elif "medios totales" in text or "XML limpiado" in text:
            tag = "info"
        elif "Eliminaria" in text or "GUARDARIA" in text:
            tag = "info"
        elif s.startswith("Origen:") or s.startswith("Destino:"):
            tag = "dim"
        elif "Rutas traducidas" in text:
            tag = "dim"
        elif s.startswith("==="):
            tag = "head"
        elif s.startswith("---") or s.startswith("\u2500"):
            tag = "sep"
        elif s.startswith("[") and "/" in s and "]" in s:
            tag = "head"

        self._log_write(text, tag)

    def _log_clear(self):
        self.log.configure(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.configure(state=tk.DISABLED)

    # ── Timer ─────────────────────────────────────────────

    def _start_timer(self):
        self._start_time = time.time()
        self._tick_timer()

    def _tick_timer(self):
        if not self.running:
            return
        elapsed = int(time.time() - self._start_time)
        m, s = divmod(elapsed, 60)
        self.timer_var.set(f"{m:02d}:{s:02d}")
        self._timer_id = self.root.after(1000, self._tick_timer)

    def _stop_timer(self):
        if self._timer_id:
            self.root.after_cancel(self._timer_id)
            self._timer_id = None

    # ── Run ───────────────────────────────────────────────

    def _run(self):
        if self.running:
            return
        if not self.projects:
            messagebox.showinfo(
                "Sin proyectos",
                "No hay proyectos para empaquetar.\n"
                "Selecciona una carpeta con archivos .prproj.",
            )
            return

        out = self.out_var.get().strip()
        if not out:
            messagebox.showwarning(
                "Nombre vacio", "Escribe un nombre para la carpeta de salida.")
            self.out_combo.focus_set()
            return

        invalid = set('<>:"/\\|?*')
        if any(c in invalid for c in out):
            messagebox.showwarning(
                "Nombre invalido",
                f"El nombre no puede contener: {' '.join(invalid)}")
            self.out_combo.focus_set()
            return

        self._save()
        self._log_clear()

        # Capturar visibles antes de limpiar el filtro
        has_filter = bool(self.search_var.get().strip())
        visible_iids = set(self._all_iids) - self._detached if has_filter else None

        # Limpiar filtro para ver progreso de todos
        self.search_var.set("")

        # Filtrar por checked (y visibles si habia filtro activo)
        projects = [
            (i, d, p)
            for i, (d, p) in enumerate(self.projects)
            if self._checked.get(str(i), True)
            and (visible_iids is None or str(i) in visible_iids)
        ]
        if self.limit_var.get():
            projects = projects[:LIMIT_N]

        if not projects:
            messagebox.showinfo(
                "Sin seleccion",
                "No hay proyectos seleccionados para empaquetar.")
            return

        dry = self.dry_var.get()
        auto_seq = self.auto_seq_var.get()
        raw_maps = [self.map_list.get(i) for i in range(self.map_list.size())]

        try:
            mappings = parse_path_mappings(raw_maps) if raw_maps else []
        except ValueError as e:
            messagebox.showerror("Error en mapeos", str(e))
            return

        # UI
        self.running = True
        self._cancel_flag = False
        self.run_btn.configure(state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)
        self.pbar["maximum"] = len(projects)
        self.pbar["value"] = 0
        self.progress_label.set(f"0 / {len(projects)}")
        self._start_timer()

        # Reset status de todos los items
        selected_indices = {orig_idx for orig_idx, _, _ in projects}
        for i in range(len(self.projects)):
            iid = str(i)
            check = "\u2611" if self._checked.get(iid, True) else "\u2610"
            if i in selected_indices:
                tag = "pending"
                status = "Listo"
            elif not self._checked.get(iid, True):
                tag = "skip"
                status = "No seleccionado"
            else:
                tag = "skip"
                status = "Fuera del limite"
            self.tree.item(iid, values=(check, self.projects[i][0], status),
                           tags=(tag,))

        # Header
        mode_label = "DRY RUN (simulacion)" if dry else "EMPAQUETADO"
        seq_label = "auto" if auto_seq else "manual"
        n = len(projects)
        limit_label = f"  (limite: {LIMIT_N})" if self.limit_var.get() else ""
        self._log_write(
            f"  {mode_label}  |  {n} proyecto{'s' if n > 1 else ''}{limit_label}  |  Secuencia: {seq_label}  |  Salida: {out}/",
            "head",
        )
        self._log_write("")

        threading.Thread(
            target=self._worker,
            args=(projects, out, dry, auto_seq, mappings),
            daemon=True,
        ).start()

    def _worker(self, projects, out_name, dry_run, auto_seq, mappings):
        logger = logging.getLogger("premiere_gui")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        handler = _GuiHandler(
            lambda msg: self.root.after(0, self._log_auto, msg))
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

        total = len(projects)
        ok_count = 0
        err_count = 0

        for seq_i, (orig_idx, display, prproj) in enumerate(projects):
            if self._cancel_flag:
                for j in range(seq_i, total):
                    oi = projects[j][0]
                    self.root.after(0, self._set_status, oi, "Cancelado", "skip")
                break

            self.root.after(0, self._set_status, orig_idx,
                            "Empaquetando\u2026", "active")
            self.root.after(0, self._set_progress, seq_i + 1, total)

            label = (display if len(display) < 50
                     else f".../{prproj.parent.name}/{prproj.name}")
            bar = "\u2500" * max(1, 52 - len(f"[{seq_i+1}/{total}] {label}"))
            logger.info("[%d/%d] %s %s", seq_i + 1, total, label, bar)

            try:
                project_root = _find_project_root(prproj)

                # Callback para seleccion manual de secuencia
                seq_cb = None
                if not auto_seq:
                    self._seq_prproj_name = prproj.name
                    seq_cb = self._ask_sequence

                stats = package_project(
                    prproj_path=prproj,
                    dest_root=project_root,
                    folder_name=out_name,
                    dry_run=dry_run,
                    mode="auto",
                    sequence_pattern=None,
                    path_mappings=mappings,
                    log=logger,
                    sequence_callback=seq_cb,
                )

                copied = stats.get("copied", 0)
                missing = stats.get("missing", 0)
                has_errors = bool(stats.get("errors"))

                if has_errors:
                    status = f"\u2717 Errores ({len(stats['errors'])})"
                    self.root.after(0, self._set_status, orig_idx, status,
                                    "error")
                    err_count += 1
                else:
                    parts = []
                    if copied:
                        parts.append(
                            f"{copied} archivo{'s' if copied != 1 else ''}")
                    if missing:
                        parts.append(f"{missing} offline")
                    status = "\u2713 " + (", ".join(parts) if parts else "OK")
                    self.root.after(0, self._set_status, orig_idx, status,
                                    "done")
                    ok_count += 1

            except Exception as e:
                logger.error("  ERROR CRITICO: %s", e)
                self.root.after(0, self._set_status, orig_idx,
                                "\u2717 Error critico", "error")
                err_count += 1

            logger.info("")

        processed = ok_count + err_count
        self.root.after(0, self._set_progress, processed, total)

        def _summary():
            self._stop_timer()
            elapsed = int(time.time() - self._start_time)
            mn, s = divmod(elapsed, 60)

            self._log_write("\u2500" * 55, "sep")

            if err_count == 0 and not self._cancel_flag:
                self._log_write(
                    f"  \u2713  Todo listo  |  {ok_count} proyecto{'s' if ok_count != 1 else ''}  |  {mn:02d}:{s:02d}",
                    "done",
                )
            else:
                self._log_write(
                    f"  Completados: {ok_count}  |  Errores: {err_count}  |  {mn:02d}:{s:02d}",
                    "warn" if err_count else "head",
                )
                if self._cancel_flag:
                    cancelled = total - processed
                    self._log_write(f"  Cancelados: {cancelled}", "warn")

            if self.dry_var.get():
                self._log_write(
                    "  (Dry-run: no se copio ningun archivo)", "dim")

            self._log_write("")

        self.root.after(0, _summary)
        self.root.after(0, self._done)

    # ── Status ────────────────────────────────────────────

    def _set_status(self, idx: int, text: str, tag: str):
        iid = str(idx)
        display = self.projects[idx][0]
        check = "\u2611" if self._checked.get(iid, True) else "\u2610"
        self.tree.item(iid, values=(check, display, text), tags=(tag,))
        if iid not in self._detached:
            self.tree.see(iid)

    def _set_progress(self, current: int, total: int):
        self.pbar["value"] = current
        self.progress_label.set(f"{current} / {total}")

    def _do_cancel(self):
        if self.running:
            self._cancel_flag = True
            self._log_write(
                "  Cancelando\u2026 (terminando proyecto actual)", "warn")

    def _done(self):
        self.running = False
        self._cancel_flag = False
        self._stop_timer()
        self.run_btn.configure(state=tk.NORMAL)
        self.cancel_btn.configure(state=tk.DISABLED)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass

    root = tk.Tk()
    w, h = 820, 740
    root.geometry(f"{w}x{h}")
    root.update_idletasks()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")

    app = App(root)

    def on_close():
        app._save()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
