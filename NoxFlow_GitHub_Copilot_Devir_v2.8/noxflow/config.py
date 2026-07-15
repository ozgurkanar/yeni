from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

from .util import load_json

BASE_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent)
DEFAULT_CONFIG = BASE_DIR / "config" / "runtime.json"
EMBEDDED_CERTIFICATE_RELATIVE = Path("certificate") / "downloadfile.crt"
EMBEDDED_CERTIFICATE = BASE_DIR / EMBEDDED_CERTIFICATE_RELATIVE
EMBEDDED_FLOW_RELATIVE = Path("flows") / "gomulu_akis.noxflow.json"
EMBEDDED_FLOW = BASE_DIR / EMBEDDED_FLOW_RELATIVE
DEFAULT_VM_ROOT = Path(r"C:\Program Files (x86)\Nox\bin\BignoxVMS")


@dataclass(slots=True)
class RuntimeConfig:
    adb_path: str
    noxconsole_path: str
    flow_file: str

    vm_root: str = str(DEFAULT_VM_ROOT)
    source_vm_name: str = "nox"
    working_clone_name: str = "Nox_1"

    certificate_source: str = EMBEDDED_CERTIFICATE_RELATIVE.as_posix()
    certificate_android_path: str = "/sdcard/Download/downloadfile.crt"
    certificate_importer_package: str = "net.jolivier.cert.Importer"

    charles_exe: str = ""
    charles_port: int = 8888
    gate_port: int = 8899
    target_hosts: tuple[str, ...] = ("outfox.api.zynga.com",)

    clone_use_mode: str = "runs"
    runs_per_clone: int = 10
    minutes_per_clone: int = 60
    clone_count: int = 0

    copy_timeout_s: int = 900
    copy_progress_interval_s: int = 15
    boot_timeout_s: int = 300
    close_timeout_s: int = 180
    folder_cleanup_timeout_s: int = 90

    cleanup_clone: bool = True
    force_remove_stale_clone_folder: bool = True
    cleanup_on_start: bool = True
    cleanup_on_stop: bool = True
    close_charles_on_exit: bool = True

    fps: int = 120
    template_high_fps_confirmed: bool = True
    apply_basic_settings: bool = False
    resolution: str = "1920,1080,240"
    cpu: int = 4
    memory_mb: int = 4096
    minimize_nox: bool = True
    visual_dependency_required: bool = True

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG) -> "RuntimeConfig":
        path = path.resolve()
        raw: dict[str, Any] = load_json(path, {})
        if not raw:
            raise RuntimeError(f"Runtime ayarı okunamadı: {path}")

        # All package resources are always resolved from the current version.
        raw["certificate_source"] = EMBEDDED_CERTIFICATE_RELATIVE.as_posix()
        # The working clone is persistent between runtime starts.
        # Legacy cleanup flags remain loadable but are not forced on.
        raw["close_charles_on_exit"] = bool(
            raw.get("close_charles_on_exit", True)
        )
        raw["target_hosts"] = tuple(
            raw.get("target_hosts") or ["outfox.api.zynga.com"]
        )

        # Backward compatibility with the old template_name field.
        if not raw.get("source_vm_name") and raw.get("template_name"):
            raw["source_vm_name"] = raw["template_name"]

        config = cls(
            **{
                key: value
                for key, value in raw.items()
                if key in cls.__dataclass_fields__
            }
        )
        config.validate(path.parent.parent)
        return config

    def validate(self, base_dir: Path) -> None:
        if self.fps != 120:
            raise ValueError(
                "Bu çalışma profili 120 FPS için sabittir; fps=120 olmalı."
            )
        if not self.template_high_fps_confirmed:
            raise ValueError(
                "Orijinal nox üzerinde High FPS Mode 120 açık olmalı."
            )

        source_name = self.source_vm_name.strip()
        clone_name = self.working_clone_name.strip()
        if not source_name:
            raise ValueError("source_vm_name boş olamaz.")
        if not clone_name:
            raise ValueError("working_clone_name boş olamaz.")
        if source_name.casefold() == clone_name.casefold():
            raise ValueError(
                "Orijinal Nox ile çalışma klonu aynı ada sahip olamaz."
            )

        mode = self.clone_use_mode.strip().lower()
        if mode not in {"runs", "minutes"}:
            raise ValueError(
                "clone_use_mode yalnızca 'runs' veya 'minutes' olabilir."
            )
        if self.runs_per_clone < 1:
            raise ValueError("runs_per_clone en az 1 olmalı.")
        if self.minutes_per_clone < 1:
            raise ValueError("minutes_per_clone en az 1 olmalı.")
        if self.clone_count < 0:
            raise ValueError("clone_count negatif olamaz.")
        if self.copy_progress_interval_s < 5:
            raise ValueError("copy_progress_interval_s en az 5 olmalı.")

        for attr in ("adb_path", "noxconsole_path"):
            value = Path(getattr(self, attr))
            if not value.is_file():
                raise FileNotFoundError(f"{attr} bulunamadı: {value}")

        vm_root = Path(self.vm_root)
        if not vm_root.is_dir():
            raise FileNotFoundError(
                f"BignoxVMS klasörü bulunamadı: {vm_root}"
            )
        source_folder = vm_root / source_name
        if not source_folder.is_dir():
            raise FileNotFoundError(
                f"Orijinal Nox klasörü bulunamadı: {source_folder}"
            )

        flow = self.resolve(self.flow_file, base_dir)
        if not flow.is_file():
            raise FileNotFoundError(f"Akış dosyası bulunamadı: {flow}")

        cert = self.certificate_path(base_dir)
        if not cert.is_file():
            raise FileNotFoundError(
                "Paket içine gömülü sertifika bulunamadı: "
                f"{cert}. ZIP dosyasını eksiksiz çıkarın."
            )

    @property
    def usage_description(self) -> str:
        if self.clone_use_mode == "minutes":
            return f"{self.minutes_per_clone} dakika"
        return f"{self.runs_per_clone} akış turu"

    def certificate_path(self, base_dir: Path) -> Path:
        return (base_dir / EMBEDDED_CERTIFICATE_RELATIVE).resolve()

    def flow_path(self, base_dir: Path) -> Path:
        return self.resolve(self.flow_file, base_dir)

    def vm_path(self, name: str) -> Path:
        return Path(self.vm_root) / name

    @staticmethod
    def resolve(value: str, base_dir: Path) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else (base_dir / path).resolve()
