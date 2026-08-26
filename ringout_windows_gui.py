#!/usr/bin/env python3
"""
RingOut Windows Builder - GUI
=============================
A graphical interface for ringout_windows_builder.py.

  - File picker for your game disc image (.iso / .wbfs)
  - Folder picker for the output location
  - Build options (auto-download deps, clean rebuild, launcher, parallel jobs)
  - Real-time, colour-coded log output that stays visible (never closes)
  - Automatic log file (logs/build_TIMESTAMP.log + logs/latest.log)
  - Build runs in a background thread so the GUI stays responsive
  - Cancel button, Open-Log button, Open-Output-Folder button
  - Error dialog with the log file path on failure

Requirements:
  - Python 3.8+ with Tkinter (included with the standard Windows installer)
  - ringout_windows_builder.py in the same directory as this script

Usage:
  python ringout_windows_gui.py
"""

import os
import re
import sys
import shutil
import subprocess
import threading
import queue
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Tkinter import — handle gracefully if not installed
# ---------------------------------------------------------------------------
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
except ImportError:
    sys.stderr.write(
        "Tkinter is not available.\n"
        "Reinstall Python from https://www.python.org/downloads/ and make\n"
        "sure 'tcl/tk and IDLE' is checked in the installer.\n"
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
APP_TITLE = "Soul Calibur II - Windows Builder"
APP_VERSION = "1.1"
WINDOW_SIZE = "860x680"
MIN_SIZE = "700x520"

ANSI_RE = re.compile(r'\033\[[0-9;]*m')

# ANSI colour code → Tkinter text tag
ANSI_TAG_MAP = [
    ('\033[92m', 'ok'),       # GREEN
    ('\033[91m', 'error'),    # RED
    ('\033[93m', 'warning'),  # YELLOW
    ('\033[96m', 'step'),     # CYAN
    ('\033[1m',  'bold'),     # BOLD
]

# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def strip_ansi(text):
    """Remove ANSI escape codes, return (clean_text, tag)."""
    tag = 'normal'
    for code, name in ANSI_TAG_MAP:
        if code in text:
            tag = name
            break
    clean = ANSI_RE.sub('', text)
    return clean, tag


# ---------------------------------------------------------------------------
# GUI class
# ---------------------------------------------------------------------------

class BuilderGUI:
    """Main application window."""

    def __init__(self, root):
        self.root = root
        self.process = None
        self.is_building = False
        self.build_thread = None
        self.log_queue = queue.Queue()
        self.log_file = None
        self.log_file_path = None
        self.latest_log_path = None
        self.builder_script = None
        self.output_dir = None

        self._find_builder()
        self._setup_logging()
        self._setup_vars()
        self._setup_ui()
        self._setup_close_handler()

        # Start polling the log queue
        self.root.after(100, self._poll_queue)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _find_builder(self):
        script_dir = Path(__file__).resolve().parent
        self.builder_script = script_dir / "ringout_windows_builder.py"
        if not self.builder_script.exists():
            messagebox.showerror(
                "Builder script not found",
                f"Could not find ringout_windows_builder.py in:\n"
                f"{script_dir}\n\n"
                f"Please make sure both files are in the same folder."
            )
            sys.exit(1)

    def _setup_logging(self):
        script_dir = Path(__file__).resolve().parent
        logs_dir = script_dir / "logs"
        logs_dir.mkdir(exist_ok=True)

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.log_file_path = logs_dir / f"build_{ts}.log"
        self.latest_log_path = logs_dir / "latest.log"

        self.log_file = open(self.log_file_path, 'w', encoding='utf-8')
        self._write_log(f"=== RingOut Windows Builder GUI v{APP_VERSION} ===")
        self._write_log(f"Log started : {datetime.now().isoformat()}")
        self._write_log(f"Builder     : {self.builder_script}")
        self._write_log(f"Python      : {sys.version.split()[0]}")
        self._write_log(f"Platform    : {sys.platform}")
        self._write_log("")

    def _setup_vars(self):
        self.iso_path = tk.StringVar()
        self.out_dir = tk.StringVar()
        self.auto_deps = tk.BooleanVar(value=True)
        self.rebuild = tk.BooleanVar(value=False)
        self.build_launcher = tk.BooleanVar(value=True)
        self.jobs = tk.IntVar(value=os.cpu_count() or 8)
        self.status_var = tk.StringVar(value="Ready")

    def _setup_ui(self):
        self.root.title(APP_TITLE)
        self.root.geometry(WINDOW_SIZE)
        self.root.minsize(700, 520)

        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # --- Input section ---
        inp = ttk.LabelFrame(main, text="Build Configuration", padding=10)
        inp.pack(fill=tk.X, pady=(0, 8))
        inp.columnconfigure(1, weight=1)

        ttk.Label(inp, text="Disc Image:").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 6), pady=3)
        ttk.Entry(inp, textvariable=self.iso_path).grid(
            row=0, column=1, sticky=tk.EW, padx=(0, 6), pady=3)
        ttk.Button(inp, text="Browse...", command=self._browse_iso).grid(
            row=0, column=2, pady=3)
        ttk.Label(inp, text="(leave blank to pick on first launch)",
                  foreground="gray").grid(row=1, column=1, sticky=tk.W)

        ttk.Label(inp, text="Output Folder:").grid(
            row=2, column=0, sticky=tk.W, padx=(0, 6), pady=3)
        ttk.Entry(inp, textvariable=self.out_dir).grid(
            row=2, column=1, sticky=tk.EW, padx=(0, 6), pady=3)
        ttk.Button(inp, text="Browse...", command=self._browse_output).grid(
            row=2, column=2, pady=3)
        ttk.Label(inp, text="(leave blank for default: RingOut-windows)",
                  foreground="gray").grid(row=3, column=1, sticky=tk.W)

        # --- Options ---
        opt = ttk.LabelFrame(main, text="Options", padding=10)
        opt.pack(fill=tk.X, pady=(0, 8))

        ttk.Checkbutton(opt, text="Auto-download build tools (Git, CMake, Ninja, clang, Python)",
                        variable=self.auto_deps).pack(anchor=tk.W)
        ttk.Checkbutton(opt, text="Rebuild from scratch (clean build)",
                        variable=self.rebuild).pack(anchor=tk.W)
        ttk.Checkbutton(opt, text="Build native launcher (RingOut.exe)",
                        variable=self.build_launcher).pack(anchor=tk.W)

        jf = ttk.Frame(opt)
        jf.pack(anchor=tk.W, pady=(6, 0))
        ttk.Label(jf, text="Parallel jobs:").pack(side=tk.LEFT)
        ttk.Spinbox(jf, from_=1, to=64, textvariable=self.jobs,
                    width=5).pack(side=tk.LEFT, padx=(6, 0))

        # --- Buttons ---
        bf = ttk.Frame(main)
        bf.pack(fill=tk.X, pady=(0, 8))

        self.build_btn = ttk.Button(bf, text="Start Build", command=self._start_build)
        self.build_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.cancel_btn = ttk.Button(bf, text="Cancel",
                                     command=self._cancel_build, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(bf, text="Open Log File", command=self._open_log).pack(
            side=tk.LEFT, padx=(0, 6))
        ttk.Button(bf, text="Open Log Folder", command=self._open_log_folder).pack(
            side=tk.LEFT, padx=(0, 6))
        ttk.Button(bf, text="Open Output", command=self._open_output).pack(
            side=tk.LEFT, padx=(0, 6))
        ttk.Button(bf, text="Save Log As...", command=self._save_log_as).pack(
            side=tk.LEFT)

        # --- Progress + status ---
        pf = ttk.Frame(main)
        pf.pack(fill=tk.X, pady=(0, 4))
        self.progress = ttk.Progressbar(pf, mode='indeterminate')
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        ttk.Label(pf, textvariable=self.status_var, width=24, anchor=tk.W).pack(
            side=tk.RIGHT)

        # --- Log output ---
        lf = ttk.LabelFrame(main, text="Build Log", padding=4)
        lf.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(
            lf, wrap=tk.WORD, state=tk.DISABLED,
            font=('Consolas', 9), bg='#1e1e1e', fg='#cccccc',
            insertbackground='#cccccc', selectbackground='#264f78')
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self.log_text.tag_config('normal', foreground='#cccccc')
        self.log_text.tag_config('ok', foreground='#6ab04c')
        self.log_text.tag_config('error', foreground='#e06c75')
        self.log_text.tag_config('warning', foreground='#d19a66')
        self.log_text.tag_config('step', foreground='#56b6c2')
        self.log_text.tag_config('bold', foreground='#ffffff',
                                 font=('Consolas', 9, 'bold'))
        self.log_text.tag_config('gui', foreground='#c678dd',
                                 font=('Consolas', 9, 'italic'))

        self._append_display("[GUI] Ready. Click 'Start Build' to begin.\n", 'gui')

    def _setup_close_handler(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Browse helpers
    # ------------------------------------------------------------------

    def _browse_iso(self):
        path = filedialog.askopenfilename(
            title="Select your SoulCalibur II disc image",
            filetypes=[("GameCube disc images", "*.iso *.wbfs *.gcm *.rvz *.gcz"),
                       ("All files", "*.*")])
        if path:
            self.iso_path.set(path)

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.out_dir.set(path)

    # ------------------------------------------------------------------
    # Build control
    # ------------------------------------------------------------------

    def _start_build(self):
        cmd = [sys.executable, str(self.builder_script)]
        iso = self.iso_path.get().strip()
        if iso:
            cmd.extend(["--iso", iso])
        out = self.out_dir.get().strip()
        if out:
            cmd.extend(["--out", out])
        if not self.auto_deps.get():
            cmd.append("--skip-deps")
        if self.rebuild.get():
            cmd.append("--rebuild")
        if not self.build_launcher.get():
            cmd.append("--no-launcher")
        cmd.extend(["--jobs", str(self.jobs.get())])

        # Also pass --log to the builder so the builder itself writes a log
        # (double safety: the GUI captures output AND the builder writes its own)
        builder_log = self.log_file_path.with_suffix('.builder.log')
        cmd.extend(["--log", str(builder_log)])

        cmd_str = ' '.join(f'"{a}"' if ' ' in a else a for a in cmd)
        self._write_log(f"Command: {cmd_str}")
        self._write_log("")
        self._append_display(f"[GUI] Starting build...\n", 'gui')
        self._append_display(f"[GUI] {cmd_str}\n\n", 'gui')

        self.is_building = True
        self.build_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.status_var.set("Building...")
        self.progress.start(15)

        if out:
            self.output_dir = Path(out)
        else:
            self.output_dir = Path(__file__).resolve().parent / "RingOut-windows"

        creationflags = 0
        if sys.platform == 'win32':
            creationflags = subprocess.CREATE_NO_WINDOW

        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                bufsize=1, creationflags=creationflags)
        except Exception as exc:
            self._on_build_error(f"Failed to start builder: {exc}")
            return

        self.build_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.build_thread.start()

    def _cancel_build(self):
        if self.process and self.process.poll() is None:
            self._append_display("\n[GUI] Cancelling build...\n", 'gui')
            self._write_log("[GUI] Build cancelled by user")
            try:
                self.process.terminate()
            except Exception:
                pass
            self.status_var.set("Cancelled")
        self._finish_build(cancelled=True)

    def _read_loop(self):
        """Background thread: read subprocess output line by line."""
        try:
            for raw_line in self.process.stdout:
                # Handle \r-separated progress bars — take the last segment
                line = raw_line.split('\r')[-1].rstrip('\n')
                if line.strip():
                    self.log_queue.put(('line', line))
        except Exception as exc:
            self.log_queue.put(('line', f"[GUI] Error reading output: {exc}"))
        self.process.wait()
        self.log_queue.put(('done', self.process.returncode))

    # ------------------------------------------------------------------
    # Queue polling (runs on the GUI thread)
    # ------------------------------------------------------------------

    def _poll_queue(self):
        try:
            while True:
                msg_type, data = self.log_queue.get_nowait()
                if msg_type == 'line':
                    clean, tag = strip_ansi(data)
                    self._write_log(clean)
                    self._append_display(clean + '\n', tag)
                elif msg_type == 'done':
                    self._on_build_done(data)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _on_build_done(self, returncode):
        self._write_log("")
        self._write_log(f"Build exit code: {returncode}")
        self._write_log(f"Finished: {datetime.now().isoformat()}")
        try:
            shutil.copy2(self.log_file_path, self.latest_log_path)
        except Exception:
            pass
        if returncode == 0:
            self._append_display("\n[GUI] Build completed successfully!\n", 'ok')
            self.status_var.set("Done - OK")
            messagebox.showinfo(
                "Build Complete",
                "The build completed successfully!\n\n"
                f"Output folder:\n  {self.output_dir}\n\n"
                f"Log saved to:\n  {self.log_file_path}")
        else:
            self._on_build_error(
                f"Build failed (exit code {returncode}).\n\n"
                f"Log file:\n  {self.log_file_path}")
        self._finish_build()

    def _on_build_error(self, msg):
        self._append_display(f"\n[GUI] {msg}\n", 'error')
        self.status_var.set("Failed")
        messagebox.showerror("Build Failed", msg + "\n\nCheck the log for details.")
        self._finish_build()

    def _finish_build(self, cancelled=False):
        self.is_building = False
        self.build_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)
        self.progress.stop()
        if cancelled:
            self.status_var.set("Cancelled")

    # ------------------------------------------------------------------
    # Utility buttons
    # ------------------------------------------------------------------

    def _open_log(self):
        """Open the current log file in the system text editor."""
        if self.log_file_path and self.log_file_path.exists():
            try:
                os.startfile(str(self.log_file_path))
            except Exception as exc:
                messagebox.showerror("Error", f"Could not open log: {exc}")
        else:
            messagebox.showinfo("No Log", "No log file has been created yet.")

    def _open_log_folder(self):
        """Open the logs folder in Explorer."""
        if self.log_file_path and self.log_file_path.parent.exists():
            try:
                os.startfile(str(self.log_file_path.parent))
            except Exception as exc:
                messagebox.showerror("Error", f"Could not open folder: {exc}")

    def _open_output(self):
        """Open the output folder in Explorer (if it exists)."""
        if self.output_dir and self.output_dir.exists():
            try:
                os.startfile(str(self.output_dir))
            except Exception as exc:
                messagebox.showerror("Error", f"Could not open folder: {exc}")
        else:
            messagebox.showinfo(
                "Not Built Yet",
                "The output folder does not exist yet.\n"
                "Run a build first.")

    def _save_log_as(self):
        """Save the current log to a user-chosen location."""
        path = filedialog.asksaveasfilename(
            title="Save log as...",
            defaultextension=".log",
            initialfile=f"ringout_build_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"),
                       ("All files", "*.*")])
        if path:
            try:
                shutil.copy2(self.log_file_path, path)
                messagebox.showinfo("Saved", f"Log saved to:\n{path}")
            except Exception as exc:
                messagebox.showerror("Error", f"Could not save: {exc}")

    # ------------------------------------------------------------------
    # Display + log file writers
    # ------------------------------------------------------------------

    def _write_log(self, text):
        """Write a line to the log file (no GUI update)."""
        if self.log_file:
            self.log_file.write(text + '\n')
            self.log_file.flush()

    def _append_display(self, text, tag='normal'):
        """Append coloured text to the on-screen log widget."""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, text, tag)
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # Window close
    # ------------------------------------------------------------------

    def _on_close(self):
        if self.is_building:
            answer = messagebox.askyesno(
                "Build in progress",
                "A build is still running. Stop it and quit?")
            if not answer:
                return
            self._cancel_build()
        # Finalise log
        if self.log_file:
            self._write_log(f"GUI closed: {datetime.now().isoformat()}")
            self.log_file.close()
        try:
            shutil.copy2(self.log_file_path, self.latest_log_path)
        except Exception:
            pass
        self.root.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    root = tk.Tk()
    try:
        # On Windows, set a taskbar icon hint (Python 3.7+)
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                'ringout.windows.builder.gui')
    except Exception:
        pass

    app = BuilderGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()