from __future__ import annotations

from pathlib import Path
import csv
import os
import shutil
import subprocess
import tempfile
import time

from .models import NoxInstance
from .util import safe_instance_name, StopRequested


def parse_nox_list_output(output: str) -> list[NoxInstance]:
    """Parse NoxConsole list output without depending on a single Nox version."""
    result: list[NoxInstance] = []
    for row in csv.reader(output.splitlines()):
        fields = [value.strip().strip("\ufeff") for value in row]
        if len(fields) < 2:
            continue

        index: int | None = None
        if fields[0].lstrip("-").isdigit() and len(fields) >= 3:
            index = int(fields[0])
            internal_name = fields[1]
            title = fields[2] or internal_name
            tail = fields[3:]
        else:
            internal_name = fields[0]
            title = fields[1] or internal_name
            tail = fields[2:]

        if not internal_name:
            continue

        pid: int | None = None
        for value in reversed(tail):
            try:
                candidate = int(value)
            except ValueError:
                continue
            # The final numeric status field is the process id on the
            # supported NoxConsole outputs. Zero means the instance is closed;
            # do not continue backwards into unrelated numeric settings.
            pid = candidate if candidate > 0 else None
            break

        result.append(
            NoxInstance(
                index=index,
                name=internal_name,
                title=title,
                running=pid is not None,
                pid=pid,
            )
        )
    return result


class NoxError(RuntimeError):
    pass


class NoxConsole:
    """Small wrapper around NoxConsole.exe.

    Clone creation deliberately uses Nox's own multi-instance command:
    NoxConsole.exe copy -name:<target> -from:<source>
    """

    def __init__(self, executable: str, log):
        self.executable = executable
        self.log = log


    NOX_PROCESS_NAMES = (
        "NoxPlayer.exe",
        "MultiPlayerManager.exe",
        "NoxVMHandle.exe",
        "NoxVMSVC.exe",
        "NoxHeadless.exe",
        "Nox.exe",
        "NoxService.exe",
        "NoxServer.exe",
        "NoxWebSocket.exe",
        "NoxAudio.exe",
        "NoxVideo.exe",
        "NoxAdb.exe",
        "nox_adb.exe",
        "adb.exe",
    )

    def kill_all_nox_processes(self, wait_s: float = 12.0) -> None:
        """Force-close every Nox-related Windows task before clone operations.

        This intentionally mirrors ending Nox from Task Manager. It is used
        before remove/copy and during final shutdown so Multi-Drive locks do
        not keep Nox_1 open.
        """
        if os.name != "nt":
            self.log("Nox görev temizliği Windows dışında atlandı")
            return

        self.log("Tüm Nox görevleri Windows üzerinden kapatılıyor")
        for image in self.NOX_PROCESS_NAMES:
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", image],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )

        deadline = time.monotonic() + max(2.0, wait_s)
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
            running = result.stdout.casefold()
            remaining = [
                name for name in self.NOX_PROCESS_NAMES
                if f'"{name.casefold()}"' in running
            ]
            if not remaining:
                self.log("Tüm Nox görevleri kapatıldı")
                return
            time.sleep(0.5)

        raise NoxError(
            "Kapanmayan Nox görevleri var: " + ", ".join(remaining)
        )

    def recreate_clone(
        self,
        *,
        vm_root: Path,
        source: NoxInstance,
        clone_name: str,
        copy_timeout_s: float,
        folder_timeout_s: float,
        stop_event,
        progress_interval_s: float = 15.0,
    ) -> NoxInstance:
        """Remove and recreate Nox_1 using NoxConsole/Multi-Drive only."""
        clone_name = safe_instance_name(clone_name)
        if source.name.casefold() == clone_name.casefold():
            raise NoxError("Kaynak nox çalışma klonu olarak kullanılamaz.")

        self.kill_all_nox_processes()
        existing = self.find_internal(clone_name)
        if existing is not None:
            self.log(f"{clone_name}: NoxConsole remove ile kaldırılıyor")
            self.remove(clone_name)
            self.wait_presence(
                clone_name, False, min(copy_timeout_s, 180), stop_event
            )

        folder = vm_root / clone_name
        if folder.exists():
            self.log(
                f"{clone_name}: NoxConsole remove sonrası kalan kilitsiz klasör temizleniyor"
            )
            self._remove_tree_with_retry(folder, folder_timeout_s, stop_event)

        self.copy(
            source, clone_name, copy_timeout_s, stop_event,
            progress_interval_s=progress_interval_s,
        )
        self.wait_presence(clone_name, True, copy_timeout_s, stop_event)
        clone = self.find_internal(clone_name)
        if clone is None:
            raise NoxError(f"{clone_name}: NoxConsole kopyası doğrulanamadı")
        return clone

    def run(
        self,
        args: list[str],
        timeout: float = 120,
        check: bool = True,
    ):
        result = subprocess.run(
            [self.executable, *args],
            cwd=str(Path(self.executable).parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        )
        if check and result.returncode != 0:
            raise NoxError(
                (result.stderr or result.stdout or "NoxConsole başarısız").strip()
            )
        return result

    def list(self) -> list[NoxInstance]:
        return parse_nox_list_output(self.run(["list"], 30).stdout)

    def find_internal(self, internal_name: str) -> NoxInstance | None:
        wanted = safe_instance_name(internal_name).casefold()
        matches = [item for item in self.list() if item.name.casefold() == wanted]
        if len(matches) > 1:
            raise NoxError(
                f"Aynı dahili ada sahip birden fazla Nox bulundu: {internal_name!r}"
            )
        return matches[0] if matches else None

    def find(self, reference: str) -> NoxInstance | None:
        items = self.list()
        wanted = (reference or "").casefold().strip()
        if not wanted:
            return None

        internal = [item for item in items if item.name.casefold() == wanted]
        if len(internal) == 1:
            return internal[0]

        titles = [item for item in items if item.title.casefold() == wanted]
        if len(titles) == 1:
            return titles[0]
        if len(titles) > 1:
            raise NoxError(
                f"Aynı görünen ada sahip birden fazla Nox var: {reference!r}."
            )
        return None

    def quit(self, name: str):
        self.run(["quit", f"-name:{safe_instance_name(name)}"], 60, False)

    def launch(self, name: str):
        self.run(["launch", f"-name:{safe_instance_name(name)}"], 90)

    def remove(self, name: str):
        self.run(["remove", f"-name:{safe_instance_name(name)}"], 300)

    def copy(
        self,
        source: NoxInstance,
        target: str,
        timeout_s: float,
        stop_event,
        progress_interval_s: float = 15.0,
    ):
        target = safe_instance_name(target)
        source_ref = safe_instance_name(source.name)
        command = [
            self.executable,
            "copy",
            f"-name:{target}",
            f"-from:{source_ref}",
        ]
        started = time.monotonic()
        next_progress = started + max(5.0, progress_interval_s)

        self.log(
            f"{target}: Nox Multi-Drive yöntemiyle klonlama başladı — "
            f"NoxConsole copy -name:{target} -from:{source_ref}"
        )

        with tempfile.TemporaryFile(mode="w+b") as output:
            process = subprocess.Popen(
                command,
                cwd=str(Path(self.executable).parent),
                stdout=output,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
            )
            try:
                while process.poll() is None:
                    if stop_event.is_set():
                        process.terminate()
                        try:
                            process.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        raise StopRequested("Klonlama sırasında durdurma istendi.")

                    elapsed = time.monotonic() - started
                    if elapsed >= timeout_s:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        raise NoxError(
                            f"Klonlama {timeout_s:g} saniyede tamamlanmadı: "
                            f"{source_ref} -> {target}"
                        )

                    if time.monotonic() >= next_progress:
                        self.log(
                            f"{target}: Nox klonlaması sürüyor — "
                            f"{int(elapsed)} saniye geçti"
                        )
                        next_progress = time.monotonic() + max(
                            5.0, progress_interval_s
                        )
                    time.sleep(0.5)
            finally:
                if process.poll() is None:
                    process.kill()

            return_code = process.returncode
            output.seek(0)
            text = output.read(64_000).decode(errors="replace").strip()

        elapsed = time.monotonic() - started
        if return_code == 0:
            self.log(
                f"{target}: Nox klonu {elapsed:.1f} saniyede oluşturuldu"
            )
            return

        brief = text[-2000:] if text else f"çıkış kodu {return_code}"
        raise NoxError(
            f"Nox klonlama başarısız: {source_ref} -> {target}: {brief}"
        )

    def modify_basic(
        self,
        name: str,
        resolution: str,
        cpu: int,
        memory: int,
    ):
        args = ["modify", f"-name:{safe_instance_name(name)}"]
        if resolution:
            args.append(f"-resolution:{resolution}")
        if cpu:
            args.append(f"-cpu:{cpu}")
        if memory:
            args.append(f"-memory:{memory}")
        self.run(args, 120)

    def enforce_120_fps(self):
        self.log(
            "120 FPS: desteklenmeyen globalsetting komutu gönderilmiyor; "
            "ayarlar kaynak nox'tan Nox'un kendi klonlama işlemiyle miras alınır."
        )

    def wait_running(
        self,
        name: str,
        running: bool,
        timeout_s: float,
        stop_event,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if stop_event.is_set():
                raise StopRequested()
            instance = self.find_internal(name)
            actual = bool(instance and instance.running)
            if actual == running:
                return
            time.sleep(1.5)
        raise NoxError(
            f"Nox çalışma durumu zamanında değişmedi: {name}, running={running}"
        )

    def wait_presence(
        self,
        name: str,
        present: bool,
        timeout_s: float,
        stop_event,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if stop_event.is_set():
                raise StopRequested()
            if (self.find_internal(name) is not None) == present:
                return
            time.sleep(1.5)
        raise NoxError(
            f"Nox kopyası beklenen duruma gelmedi: {name}, present={present}"
        )

    @staticmethod
    def _remove_tree_with_retry(
        folder: Path,
        timeout_s: float,
        stop_event,
    ) -> None:
        deadline = time.monotonic() + max(2.0, timeout_s)
        last_error: Exception | None = None

        def make_writable(function, path, _exc_info):
            try:
                os.chmod(path, 0o700)
                function(path)
            except Exception:
                pass

        while time.monotonic() < deadline:
            if stop_event.is_set():
                raise StopRequested()
            if not folder.exists():
                return
            try:
                shutil.rmtree(folder, onerror=make_writable)
                if not folder.exists():
                    return
            except Exception as exc:
                last_error = exc
            time.sleep(1.0)

        raise NoxError(
            f"Çalışma klonu klasörü silinemedi: {folder}: {last_error}"
        )

    def close_clone(
        self,
        name: str,
        timeout_s: float,
        stop_event,
    ) -> bool:
        """Close the working clone but preserve its registration and folder."""
        instance = self.find_internal(name)
        if instance is None:
            return False
        if not instance.running:
            self.log(f"{name}: zaten kapalı; klon klasörü korunuyor")
            return True

        self.log(f"{name}: kapatılıyor; klon kaydı ve klasörü korunacak")
        self.quit(name)
        self.wait_running(name, False, timeout_s, stop_event)
        return True

    def remove_working_clone(
        self,
        *,
        vm_root: Path,
        source_name: str,
        clone_name: str,
        close_timeout_s: float,
        folder_timeout_s: float,
        stop_event,
    ) -> None:
        """Remove only the working clone during an intentional rotation."""
        source_name = safe_instance_name(source_name)
        clone_name = safe_instance_name(clone_name)
        if source_name.casefold() == clone_name.casefold():
            raise NoxError(
                "Koruma: kaynak nox çalışma klonu olarak silinemez."
            )

        instance = self.find_internal(clone_name)
        if instance is not None:
            if instance.running:
                self.close_clone(
                    clone_name,
                    close_timeout_s,
                    stop_event,
                )
            self.log(
                f"{clone_name}: kullanım sınırı dolduğu için "
                "NoxConsole remove ile yenileniyor"
            )
            self.remove(clone_name)
            self.wait_presence(
                clone_name,
                False,
                close_timeout_s,
                stop_event,
            )

        folder = vm_root / clone_name
        if not folder.exists():
            return

        # NoxConsole removes the folder asynchronously on some versions.
        deadline = time.monotonic() + min(folder_timeout_s, 15.0)
        while time.monotonic() < deadline:
            if stop_event.is_set():
                raise StopRequested()
            if not folder.exists():
                return
            time.sleep(1.0)

        resolved_root = vm_root.resolve()
        resolved_folder = folder.resolve()
        resolved_source = (vm_root / source_name).resolve()
        if (
            resolved_folder.parent != resolved_root
            or resolved_folder == resolved_source
            or resolved_folder.name.casefold() != clone_name.casefold()
        ):
            raise NoxError(
                f"Güvenlik nedeniyle kalan klon klasörü silinmedi: {resolved_folder}"
            )

        self.log(
            f"{clone_name}: NoxConsole'dan kalan çalışma klasörü temizleniyor"
        )
        self._remove_tree_with_retry(
            resolved_folder,
            folder_timeout_s,
            stop_event,
        )

    def backup_orphan_folder(
        self,
        vm_root: Path,
        clone_name: str,
    ) -> Path:
        """Keep an unregistered Nox_1 folder instead of deleting it."""
        clone_name = safe_instance_name(clone_name)
        folder = vm_root / clone_name
        if not folder.exists():
            return folder

        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = vm_root / f"{clone_name}_yedek_{stamp}"
        counter = 1
        while backup.exists():
            backup = vm_root / f"{clone_name}_yedek_{stamp}_{counter}"
            counter += 1

        try:
            folder.rename(backup)
        except OSError as exc:
            raise NoxError(
                f"Kayıtsız {clone_name} klasörü korunarak yeniden "
                f"adlandırılamadı: {folder}: {exc}"
            ) from exc

        self.log(
            f"{clone_name}: NoxConsole kaydı olmayan klasör silinmedi; "
            f"yedeklendi: {backup}"
        )
        return backup
