from __future__ import annotations

from pathlib import Path
import hashlib
import shlex
import tempfile
import time

from .adb import AdbClient, AdbError, parse_ui
from .util import StopRequested


class CertificateInstaller:
    def __init__(
        self,
        source: Path,
        android_path: str,
        package: str,
        log,
        stop_event,
    ):
        self.source = source
        self.android_path = android_path
        self.package = package
        self.log = log
        self.stop_event = stop_event
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:24]
        self.marker = f"/sdcard/.noxflow/certificate_{digest}.ok"

    @staticmethod
    def matches(node, ids=(), texts=(), descriptions=()):
        return node.enabled and (
            (node.resource_id and node.resource_id in ids)
            or (node.text and node.text.casefold() in {x.casefold() for x in texts})
            or (
                node.content_desc
                and node.content_desc.casefold()
                in {x.casefold() for x in descriptions}
            )
        )

    def wait_tap(
        self,
        client: AdbClient,
        label: str,
        ids=(),
        texts=(),
        descriptions=(),
        timeout=15,
        optional=False,
    ):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                raise StopRequested()
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "ui.xml"
                try:
                    client.dump_ui(path)
                    nodes = parse_ui(path)
                except Exception:
                    nodes = []
            found = [
                node
                for node in nodes
                if self.matches(node, ids, texts, descriptions)
            ]
            if found:
                found.sort(key=lambda node: (not node.clickable, node.area))
                x, y = found[0].target_center
                client.tap(x, y)
                self.log(f"Sertifika: {label} tıklandı")
                time.sleep(0.8)
                return True
            time.sleep(0.65)
        if optional:
            return False
        raise AdbError(f"Sertifika öğesi bulunamadı: {label}")

    def _already_prepared(self, client: AdbClient) -> bool:
        try:
            result = client.shell(
                f"if [ -f {shlex.quote(self.marker)} ]; then echo 1; else echo 0; fi",
                10,
            )
            return result.strip().endswith("1")
        except Exception:
            return False

    def _write_marker(self, client: AdbClient) -> None:
        client.shell(
            "mkdir -p /sdcard/.noxflow && "
            f"echo ok > {shlex.quote(self.marker)}",
            15,
        )

    def install(self, client: AdbClient):
        if not self.source.is_file():
            raise FileNotFoundError(self.source)
        if self._already_prepared(client):
            self.log(
                "Sertifika bu kalıcı Nox_1 klonunda daha önce hazırlandı; "
                "yeniden kurulum atlandı."
            )
            return
        if not client.package_installed(self.package):
            raise AdbError(
                f"Sertifika yöneticisi kurulu değil: {self.package}"
            )

        self.log(f"Sertifika Nox içine gönderiliyor: {self.android_path}")
        client.push(self.source, self.android_path)
        client.force_stop(self.package)
        client.launch_package(self.package)
        time.sleep(1.2)
        self.wait_tap(
            client,
            "İzin",
            ids=(
                "com.android.packageinstaller:id/permission_allow_button",
                "com.android.permissioncontroller:id/permission_allow_button",
            ),
            texts=("ALLOW", "Allow", "İZİN VER", "İzin ver"),
            timeout=3,
            optional=True,
        )
        self.wait_tap(
            client,
            "Import from SD Card",
            ids=("net.jolivier.cert.Importer:id/action_install_from_sd",),
            texts=(
                "Import from SD Card",
                "SD Card'dan içe aktar",
                "SD karttan içe aktar",
            ),
            timeout=20,
        )
        filename = Path(self.android_path).name
        if not self.wait_tap(
            client,
            "Sertifika dosyası",
            texts=(filename,),
            timeout=5,
            optional=True,
        ):
            self.wait_tap(
                client,
                "Downloads",
                texts=("Downloads", "Download", "İndirilenler"),
                timeout=8,
                optional=True,
            )
            self.wait_tap(
                client,
                "Sertifika dosyası",
                texts=(filename,),
                timeout=20,
            )
        self.wait_tap(
            client,
            "Import",
            ids=("android:id/button1",),
            texts=("Import", "IMPORT", "İçe aktar", "İÇE AKTAR"),
            timeout=15,
        )
        self.wait_tap(
            client,
            "OK",
            ids=("android:id/button1",),
            texts=("OK", "Tamam", "TAMAM"),
            timeout=8,
            optional=True,
        )
        client.keyevent("HOME")
        self._write_marker(client)
        self.log("Sertifika kurulumu tamamlandı ve klonda işaretlendi.")
