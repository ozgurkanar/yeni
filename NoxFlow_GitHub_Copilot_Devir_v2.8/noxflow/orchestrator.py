from __future__ import annotations

from pathlib import Path
import ctypes
import threading
import time

from . import __version__
from .config import RuntimeConfig, DEFAULT_CONFIG
from .adb import AdbClient
from .nox import NoxConsole, NoxError
from .proxy import CharlesManager, SelectiveGate
from .certificate import CertificateInstaller
from .flow import load_flow, FlowRunner
from .util import EventLog, StopRequested


def minimize_nox_windows() -> int:
    if not hasattr(ctypes, "windll"):
        return 0

    user32 = ctypes.windll.user32
    count = 0
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )

    @callback_type
    def callback(hwnd, _):
        nonlocal count
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if buffer.value.lower().startswith("noxplayer"):
            user32.ShowWindow(hwnd, 6)
            count += 1
        return True

    user32.EnumWindows(callback, 0)
    return count


class Orchestrator:
    def __init__(
        self,
        config_path: Path = DEFAULT_CONFIG,
        log_callback=None,
    ):
        self.config_path = config_path.resolve()
        self.base_dir = self.config_path.parent.parent
        self.config = RuntimeConfig.load(self.config_path)

        self.stop_event = threading.Event()
        # Final shutdown must not be cancelled by the normal stop request.
        self.cleanup_event = threading.Event()

        self.log = EventLog(
            self.base_dir / "logs" / "runtime.log",
            log_callback,
        )
        self.nox = NoxConsole(self.config.noxconsole_path, self.log)
        self.charles = CharlesManager(
            self.config.charles_exe,
            self.config.charles_port,
            self.log,
        )
        self.gate = SelectiveGate(
            self.config.gate_port,
            self.config.charles_port,
            self.config.target_hosts,
            self.log,
        )

        self.current_clone = ""
        self.current_serial = ""
        self.shutdown_lock = threading.RLock()
        self.shutdown_complete = threading.Event()
        self.shutdown_started = False

    def stop(self) -> None:
        if not self.stop_event.is_set():
            self.log(
                "Durdurma istendi. Mevcut işlem kesilecek; "
                "Nox'a ait tüm Windows görevleri kapatılacak."
            )
        self.stop_event.set()

    def source_instance(self, event: threading.Event):
        source = self.nox.find_internal(self.config.source_vm_name)
        if source is None:
            raise NoxError(
                "Kaynak nox NoxConsole listesinde bulunamadı: "
                f"{self.config.source_vm_name!r}"
            )

        if source.running:
            self.log(
                f"Kaynak {source.name} açık bulundu; güvenlik için kapatılıyor. "
                "Program kaynak nox'u hiçbir zaman çalıştırmaz."
            )
            self.nox.quit(source.name)
            self.nox.wait_running(
                source.name,
                False,
                self.config.close_timeout_s,
                event,
            )
        return source

    def prepare_services(self) -> None:
        if not self.config.template_high_fps_confirmed:
            raise RuntimeError(
                "Kaynak nox üzerinde High FPS Mode 120 onaylanmadı."
            )
        self.nox.enforce_120_fps()
        self.charles.ensure(stop_event=self.stop_event)
        self.gate.start()

    def ensure_clone_exists(self, source, event: threading.Event):
        """Always build a fresh Nox_1 through NoxConsole/Multi-Drive."""
        clone_name = self.config.working_clone_name
        self.log(
            f"{clone_name}: temiz çalışma klonu hazırlanıyor; "
            "klasör kopyalama kullanılmayacak"
        )
        clone = self.nox.recreate_clone(
            vm_root=Path(self.config.vm_root),
            source=source,
            clone_name=clone_name,
            copy_timeout_s=self.config.copy_timeout_s,
            folder_timeout_s=self.config.folder_cleanup_timeout_s,
            stop_event=event,
            progress_interval_s=self.config.copy_progress_interval_s,
        )
        if self.config.apply_basic_settings:
            self.nox.modify_basic(
                clone_name, self.config.resolution,
                self.config.cpu, self.config.memory_mb,
            )
        return clone, True

    def _ready_adb_clients(self) -> list[tuple[str, AdbClient]]:
        base = AdbClient(self.config.adb_path)
        try:
            serials = base.devices()
        except Exception:
            return []

        ready: list[tuple[str, AdbClient]] = []
        for serial in serials:
            client = AdbClient(self.config.adb_path, serial)
            if client.boot_completed():
                ready.append((serial, client))
        return ready

    def wait_clone_adb(
        self,
        before: set[str],
        already_running: bool,
    ) -> tuple[str, AdbClient]:
        deadline = time.monotonic() + self.config.boot_timeout_s
        last: list[str] = []

        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                raise StopRequested()
            ready = self._ready_adb_clients()
            last = [serial for serial, _ in ready]

            new_ready = [item for item in ready if item[0] not in before]
            if len(new_ready) == 1:
                return new_ready[0]

            # The documented starting state has no other emulator open. This
            # also lets us attach when Nox_1 was already running at Start.
            if already_running and len(ready) == 1:
                return ready[0]

            # Some Nox versions expose the ADB endpoint before the list/PID
            # state settles. A single ready device is safe in this runtime.
            if not before and len(ready) == 1:
                return ready[0]
            time.sleep(2)

        raise RuntimeError(
            "Nox_1 ADB cihazı hazır olmadı. Görülen hazır cihazlar: "
            f"{last}"
        )

    def open_clone(self, clone) -> tuple[str, AdbClient]:
        clone_name = self.config.working_clone_name
        before = set(AdbClient(self.config.adb_path).devices())
        already_running = bool(clone.running)

        if already_running:
            self.log(f"{clone_name}: zaten açık; mevcut oturumla devam ediliyor")
        else:
            self.log(f"{clone_name}: kapalı bulundu; NoxConsole ile açılıyor")
            self.nox.launch(clone_name)

        serial, client = self.wait_clone_adb(before, already_running)
        self.current_serial = serial
        client.wait_boot(
            self.config.boot_timeout_s,
            self.stop_event,
            self.log,
        )
        return serial, client

    def prepare_clone_android(self, client: AdbClient, certificate) -> None:
        client.set_120hz_android(self.log)
        client.reverse(self.config.gate_port, self.config.gate_port)
        client.set_proxy("127.0.0.1", self.config.gate_port)
        self.log(
            f"{self.config.working_clone_name}: Charles ve seçici proxy hazır"
        )
        certificate.install(client)

        if self.config.minimize_nox:
            count = minimize_nox_windows()
            self.log(
                f"{self.config.working_clone_name}: "
                f"{count} Nox penceresi küçültüldü"
            )

    def play_flow(self, client, flow, clone_name: str) -> None:
        runner = FlowRunner(flow, self.stop_event, self.log)
        if self.config.clone_use_mode == "minutes":
            self.log(
                f"{clone_name}: kullanım sınırı "
                f"{self.config.minutes_per_clone} dakika"
            )
            runner.run(
                client,
                repeats=0,
                label=clone_name,
                max_seconds=self.config.minutes_per_clone * 60,
            )
        else:
            self.log(
                f"{clone_name}: kullanım sınırı "
                f"{self.config.runs_per_clone} akış turu"
            )
            runner.run(
                client,
                repeats=self.config.runs_per_clone,
                label=clone_name,
            )

    def wait_adb_gone(self, serial: str, event: threading.Event) -> None:
        if not serial:
            return
        base = AdbClient(self.config.adb_path)
        deadline = time.monotonic() + self.config.close_timeout_s
        while time.monotonic() < deadline:
            if event.is_set():
                raise StopRequested()
            try:
                devices = base.devices()
            except Exception:
                devices = []
            if serial not in devices:
                return
            time.sleep(1.5)
        raise RuntimeError(f"ADB kapanmadı: {serial}")

    def close_current_clone(self, event: threading.Event) -> None:
        serial = self.current_serial
        if serial:
            try:
                AdbClient(self.config.adb_path, serial).clear_proxy()
            except Exception:
                pass

        self.nox.close_clone(
            self.config.working_clone_name,
            self.config.close_timeout_s,
            event,
        )
        if serial:
            self.wait_adb_gone(serial, event)
        self.current_serial = ""

    def rotate_clone(self, event: threading.Event) -> None:
        clone_name = self.config.working_clone_name
        self.log(
            f"{clone_name}: seçilen kullanım sınırı tamamlandı; "
            "temiz klon yenilemesi başlıyor"
        )
        self.close_current_clone(event)
        self.nox.remove_working_clone(
            vm_root=Path(self.config.vm_root),
            source_name=self.config.source_vm_name,
            clone_name=clone_name,
            close_timeout_s=self.config.close_timeout_s,
            folder_timeout_s=self.config.folder_cleanup_timeout_s,
            stop_event=event,
        )
        self.current_clone = ""
        self.log(
            f"{clone_name}: eski çalışma klonu kaldırıldı; "
            "sonraki turda NoxConsole copy ile yeniden oluşturulacak"
        )

    def should_run_another_cycle(self, completed: int) -> bool:
        if self.stop_event.is_set():
            return False
        return self.config.clone_count == 0 or completed < self.config.clone_count

    def orderly_shutdown(self) -> None:
        with self.shutdown_lock:
            if self.shutdown_started:
                return
            self.shutdown_started = True

        self.log("Düzenli kapanış başladı.")
        errors: list[str] = []
        try:
            try:
                self.nox.kill_all_nox_processes()
                self.current_serial = ""
                self.log("Nox'a ait tüm Windows görevleri kapatıldı.")
            except Exception as exc:
                errors.append(f"Nox görevleri: {exc}")
                self.log(f"Kapanış uyarısı — Nox görevleri: {exc}")
        finally:
            try:
                self.gate.stop()
            except Exception as exc:
                errors.append(f"Seçici geçit: {exc}")
                self.log(f"Kapanış uyarısı — seçici geçit: {exc}")

            if self.config.close_charles_on_exit:
                try:
                    self.charles.stop_if_owned()
                except Exception as exc:
                    errors.append(f"Charles: {exc}")
                    self.log(f"Kapanış uyarısı — Charles: {exc}")

        if errors:
            self.log(
                f"Düzenli kapanış tamamlandı ancak {len(errors)} uyarı var."
            )
        else:
            self.log(
                "Düzenli kapanış tamamlandı: tüm Nox görevleri ve seçici "
                "geçit kapalı; runtime'ın açtığı Charles kapalı."
            )
        self.shutdown_complete.set()

    def run(self) -> None:
        self.log(f"NoxFlow Modular Runtime {__version__} başladı.")
        self.log(
            "Başlatma zinciri: tüm Nox görevlerini kapat → NoxConsole remove → "
            "NoxConsole copy → NoxConsole launch → Android boot → Charles/proxy → "
            "sertifika → gömülü uygulama akışı."
        )
        self.log(
            f"Kaynak: {Path(self.config.vm_root) / self.config.source_vm_name}"
        )
        self.log(
            f"Çalışma klonu: "
            f"{Path(self.config.vm_root) / self.config.working_clone_name}"
        )
        self.log(f"Klon kullanım politikası: {self.config.usage_description}")

        flow = load_flow(self.config.flow_path(self.base_dir))
        self.log(
            f"Gömülü akış hazır: {flow.name} ({len(flow.steps)} uygulama adımı)"
        )
        certificate = CertificateInstaller(
            self.config.certificate_path(self.base_dir),
            self.config.certificate_android_path,
            self.config.certificate_importer_package,
            self.log,
            self.stop_event,
        )

        completed = 0
        try:
            while self.should_run_another_cycle(completed):
                completed += 1
                clone_name = self.config.working_clone_name
                self.current_clone = clone_name
                self.current_serial = ""
                self.log(f"=== Temiz klon turu {completed} başlıyor ===")

                # 1) Kaynak yalnızca NoxConsole copy için kullanılır; açılmaz.
                source = self.source_instance(self.stop_event)
                # 2) Tüm görevler kapatılır ve gerçek Multi-Drive klonu yaratılır.
                clone, _ = self.ensure_clone_exists(source, self.stop_event)
                self.log(f"{clone_name}: yeni NoxConsole klonu hazır")
                # 3) Yeni klon açılır ve Android beklenir.
                _, client = self.open_clone(clone)
                # 4) Klon açıldıktan sonra Charles/geçit ve sertifika hazırlanır.
                self.prepare_services()
                self.prepare_clone_android(client, certificate)
                # 5) Kullanıcının gömülü uygulama akışı çalışır.
                self.play_flow(client, flow, clone_name)
                self.log(f"=== Temiz klon turu {completed} tamamlandı ===")

                # Her turun sonunda tüm Nox görevleri kapanır. Sonraki tur varsa
                # baştan remove/copy/launch zinciri yürür.
                self.nox.kill_all_nox_processes()
                self.current_serial = ""

        except StopRequested:
            self.log("Normal çalışma durduruldu; tüm Nox görevleri kapatılıyor.")
        finally:
            self.orderly_shutdown()
