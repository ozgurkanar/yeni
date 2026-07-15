from __future__ import annotations

from pathlib import Path
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

from noxflow import __version__
from noxflow.config import (
    DEFAULT_CONFIG,
    EMBEDDED_CERTIFICATE,
    EMBEDDED_FLOW,
)
from noxflow.orchestrator import Orchestrator


MODE_LABELS = {
    "Akış turu": "runs",
    "Dakika": "minutes",
}
MODE_VALUES = {
    value: label for label, value in MODE_LABELS.items()
}


class Panel:
    def __init__(self, root):
        self.root = root
        root.title(
            f"NoxFlow Runtime {__version__} — Hafif Mod"
        )
        root.geometry("970x650")

        self.messages = queue.Queue()
        self.worker = None
        self.engine = None
        self.closing_after_cleanup = False

        self.config_path = DEFAULT_CONFIG.resolve()
        self.raw_config = self.load_config()

        outer = ttk.Frame(root, padding=10)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text=f"Uzun Süreli Modüler Çalıştırıcı {__version__}",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor="w")

        ttk.Label(
            outer,
            text=(
                "Güvenli yaşam döngüsü: kaynak nox kullanılmaz → mevcut "
                "Nox_1 açıksa devam edilir, kapalıysa açılır, yoksa NoxConsole "
                "ile klonlanır → kullanım sınırında temiz klon yenilenir."
            ),
            wraplength=930,
        ).pack(anchor="w", pady=(3, 8))

        paths = ttk.LabelFrame(
            outer,
            text="Sabit kaynaklar",
            padding=8,
        )
        paths.pack(fill="x")
        paths.columnconfigure(1, weight=1)

        ttk.Label(paths, text="Orijinal").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            paths,
            text=str(
                Path(self.raw_config["vm_root"])
                / self.raw_config["source_vm_name"]
            ),
        ).grid(row=0, column=1, sticky="w", padx=8)

        ttk.Label(paths, text="Çalışma klonu").grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )
        ttk.Label(
            paths,
            text=str(
                Path(self.raw_config["vm_root"])
                / self.raw_config["working_clone_name"]
            ),
        ).grid(
            row=1,
            column=1,
            sticky="w",
            padx=8,
            pady=(4, 0),
        )

        ttk.Label(paths, text="Gömülü akış").grid(
            row=2, column=0, sticky="w", pady=(4, 0)
        )
        ttk.Label(
            paths,
            text=str(EMBEDDED_FLOW),
        ).grid(
            row=2,
            column=1,
            sticky="w",
            padx=8,
            pady=(4, 0),
        )

        controls = ttk.LabelFrame(
            outer,
            text="Klon kullanım süresi",
            padding=8,
        )
        controls.pack(fill="x", pady=(8, 0))

        mode_value = self.raw_config.get(
            "clone_use_mode",
            "runs",
        )
        self.mode = tk.StringVar(
            value=MODE_VALUES.get(
                mode_value,
                "Akış turu",
            )
        )
        initial_value = (
            self.raw_config.get("minutes_per_clone", 60)
            if mode_value == "minutes"
            else self.raw_config.get("runs_per_clone", 10)
        )
        self.usage_value = tk.IntVar(
            value=int(initial_value)
        )
        self.clone_count = tk.IntVar(
            value=int(
                self.raw_config.get("clone_count", 0)
            )
        )

        ttk.Label(controls, text="Ölçüt").pack(side="left")
        ttk.Combobox(
            controls,
            textvariable=self.mode,
            values=list(MODE_LABELS),
            state="readonly",
            width=14,
        ).pack(side="left", padx=(5, 14))

        ttk.Label(controls, text="Değer").pack(side="left")
        ttk.Spinbox(
            controls,
            from_=1,
            to=100000,
            textvariable=self.usage_value,
            width=9,
        ).pack(side="left", padx=(5, 14))

        ttk.Label(
            controls,
            text="Toplam klon",
        ).pack(side="left")
        ttk.Spinbox(
            controls,
            from_=0,
            to=100000,
            textvariable=self.clone_count,
            width=9,
        ).pack(side="left", padx=(5, 4))
        ttk.Label(
            controls,
            text="0 = durdurulana kadar",
        ).pack(side="left")

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(8, 0))

        self.config = tk.StringVar(
            value=str(self.config_path)
        )
        ttk.Label(
            actions,
            text="Aktif sürüm ayarı:",
        ).pack(side="left")
        ttk.Entry(
            actions,
            textvariable=self.config,
            state="readonly",
        ).pack(
            side="left",
            fill="x",
            expand=True,
            padx=6,
        )
        ttk.Button(
            actions,
            text="Ayarı Aç",
            command=self.open_config,
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            actions,
            text="Akış Editörü",
            command=self.open_editor,
        ).pack(side="left", padx=(0, 6))

        self.start = ttk.Button(
            actions,
            text="Başlat",
            command=self.start_run,
        )
        self.start.pack(side="left")
        self.stop = ttk.Button(
            actions,
            text="Durdur",
            command=self.stop_run,
            state="disabled",
        )
        self.stop.pack(side="left", padx=(6, 0))

        ttk.Label(
            outer,
            text=(
                "Durdur veya pencereyi kapat: mevcut işlem kesilir → "
                "Nox_1 yalnızca kapatılır ve klasörü korunur → geçit kapanır → "
                "runtime tarafından açılan Charles kapanır."
            ),
            wraplength=930,
        ).pack(anchor="w", pady=(6, 0))
        ttk.Label(
            outer,
            text=f"Paket içi sertifika: {EMBEDDED_CERTIFICATE}",
            wraplength=930,
        ).pack(anchor="w", pady=(3, 0))

        self.status = tk.StringVar(
            value="Hazır — mevcut Nox_1 kullanılacak; yoksa NoxConsole ile oluşturulacak"
        )
        ttk.Label(
            outer,
            textvariable=self.status,
        ).pack(anchor="w", pady=6)

        self.log = ScrolledText(
            outer,
            font=("Consolas", 9),
            state="disabled",
        )
        self.log.pack(fill="both", expand=True)

        root.after(250, self.pump)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def load_config(self):
        return json.loads(
            self.config_path.read_text(encoding="utf-8")
        )

    def save_runtime_choices(self):
        value = int(self.usage_value.get())
        clones = int(self.clone_count.get())
        if value < 1:
            raise ValueError(
                "Klon kullanım değeri en az 1 olmalı."
            )
        if clones < 0:
            raise ValueError(
                "Toplam klon negatif olamaz."
            )

        raw = self.load_config()
        mode = MODE_LABELS[self.mode.get()]
        raw["clone_use_mode"] = mode
        if mode == "minutes":
            raw["minutes_per_clone"] = value
        else:
            raw["runs_per_clone"] = value
        raw["clone_count"] = clones

        temp = self.config_path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(
                raw,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temp.replace(self.config_path)
        self.raw_config = raw

    def emit(self, line):
        self.messages.put(("log", line))

    def open_config(self):
        try:
            if os.name == "nt":
                os.startfile(str(self.config_path))
            else:
                messagebox.showinfo(
                    "Ayar dosyası",
                    str(self.config_path),
                )
        except Exception as exc:
            messagebox.showerror(
                "Ayar açılamadı",
                str(exc),
            )

    def open_editor(self):
        try:
            if os.name == "nt":
                subprocess.Popen(
                    [
                        "cmd",
                        "/c",
                        "start_flow_editor.bat",
                    ],
                    cwd=str(self.config_path.parent.parent),
                )
            else:
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "tools.flow_editor",
                    ],
                    cwd=str(self.config_path.parent.parent),
                )
        except Exception as exc:
            messagebox.showerror(
                "Editör açılamadı",
                str(exc),
            )

    def start_run(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            self.save_runtime_choices()
            self.engine = Orchestrator(
                self.config_path,
                self.emit,
            )
        except Exception as exc:
            messagebox.showerror(
                "Ayar hatası",
                str(exc),
            )
            return

        self.start.config(state="disabled")
        self.stop.config(state="normal")
        self.status.set("Çalışıyor")

        def work():
            try:
                self.engine.run()
                self.messages.put(
                    ("done", "Tamamlandı")
                )
            except Exception as exc:
                self.messages.put(
                    ("done", f"Hata: {exc}")
                )

        self.worker = threading.Thread(
            target=work,
            daemon=False,
            name="NoxFlowRuntimeWorker",
        )
        self.worker.start()

    def stop_run(self):
        if self.engine and self.worker and self.worker.is_alive():
            self.engine.stop()
            self.start.config(state="disabled")
            self.stop.config(state="disabled")
            self.status.set(
                "Düzenli kapanış yapılıyor — Nox_1 kapatılıp korunuyor, "
                "servisler kapatılıyor…"
            )

    def pump(self):
        try:
            while True:
                kind, text = self.messages.get_nowait()
                if kind == "log":
                    self.log.config(state="normal")
                    self.log.insert("end", text + "\n")
                    self.log.see("end")
                    self.log.config(state="disabled")
                else:
                    self.engine = None
                    self.status.set(text)
                    self.stop.config(state="disabled")
                    if self.closing_after_cleanup:
                        self.root.after(100, self.root.destroy)
                    else:
                        self.start.config(state="normal")
        except queue.Empty:
            pass
        self.root.after(250, self.pump)

    def close(self):
        if self.worker and self.worker.is_alive():
            self.closing_after_cleanup = True
            self.engine.stop()
            self.start.config(state="disabled")
            self.stop.config(state="disabled")
            self.status.set(
                "Pencere düzenli kapanıştan sonra otomatik kapanacak — "
                "Nox_1 kapatılıp korunuyor, servisler kapatılıyor…"
            )
            return
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    Panel(root)
    root.mainloop()
