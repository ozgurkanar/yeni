
from __future__ import annotations

import base64
import csv
import hashlib
import ctypes
import io
import json
import os
import queue
import re
import shutil
import select
import shlex
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tkinter import (
    BOTH, END, HORIZONTAL, LEFT, RIGHT, VERTICAL, BooleanVar, Canvas, DoubleVar,
    IntVar, StringVar, Tk, Toplevel, filedialog, messagebox, simpledialog
)
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable
from urllib.parse import urlsplit

from PIL import Image, ImageChops, ImageStat, ImageTk

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noxflow import __version__


APP_NAME = "NoxFlow Lite"
BASE_DIR = Path(__file__).resolve().parent.parent
FLOWS_DIR = BASE_DIR / "flows"
DATA_DIR = BASE_DIR / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"
UI_COMMANDS_FILE = DATA_DIR / "ui_commands.json"
STEP_STATE_FILE = DATA_DIR / "step_run_state.json"
CERTIFICATE_STATE_FILE = DATA_DIR / "certificate_state.json"
CERTIFICATE_DIR = BASE_DIR / "certificate"
DEFAULT_CERTIFICATE_FILE = CERTIFICATE_DIR / "downloadfile.crt"

# Sertifika her zaman çalışan mevcut sürümün paket içi klasöründen okunur.
FIXED_CERTIFICATE_SOURCE = CERTIFICATE_DIR / "downloadfile.crt"
DEFAULT_ANDROID_CERTIFICATE_PATH = "/sdcard/Download/downloadfile.crt"

RUN_CONDITION_LABELS = {
    "always": "Her zaman",
    "once_per_nox_session": "Bu Nox açılışında yalnızca bir kez",
    "once_per_flow_run": "Her akış başlatmada yalnızca bir kez",
}
RUN_CONDITION_VALUES = {
    label: value for value, label in RUN_CONDITION_LABELS.items()
}

FLOWS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)


@dataclass
class UiNode:
    package: str = ""
    text: str = ""
    resource_id: str = ""
    class_name: str = ""
    content_desc: str = ""
    clickable: bool = False
    enabled: bool = True
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    # Metin düğümünün kendisi tıklanabilir olmayabilir. En yakın
    # tıklanabilir üst satırın sınırlarını ayrıca saklarız.
    click_bounds: tuple[int, int, int, int] | None = None

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    @property
    def target_center(self) -> tuple[int, int]:
        bounds = self.click_bounds or self.bounds
        x1, y1, x2, y2 = bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    @property
    def area(self) -> int:
        x1, y1, x2, y2 = self.bounds
        return max(0, x2 - x1) * max(0, y2 - y1)

    def contains(self, x: int, y: int) -> bool:
        x1, y1, x2, y2 = self.bounds
        return x1 <= x <= x2 and y1 <= y <= y2


@dataclass
class FlowStep:
    action: str
    step_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    package: str = ""
    resource_id: str = ""
    text: str = ""
    class_name: str = ""
    content_desc: str = ""
    x: int | None = None
    y: int | None = None
    x2: int | None = None
    y2: int | None = None
    duration_ms: int = 800
    wait_after: float = 1.0
    fallback_to_coordinate: bool = True
    enabled: bool = True
    run_condition: str = "always"

    # Arka planda hazır olmayı bekleyen adımlar.
    timeout_s: float = 30.0
    poll_interval: float = 0.8

    # Özel çizilmiş butonlar için sabit bölge görseli.
    template_png_base64: str = ""
    region_x: int | None = None
    region_y: int | None = None
    region_w: int | None = None
    region_h: int | None = None
    similarity: float = 0.90

    # Ekransız Android komut adımları.
    component: str = ""
    intent_action: str = ""
    data_uri: str = ""


@dataclass
class UiCommandDefinition:
    key: str
    name: str
    package: str
    component: str = ""
    resource_id: str = ""
    text: str = ""
    class_name: str = ""
    content_desc: str = ""
    x: int | None = None
    y: int | None = None
    learned_at: str = ""
    seen_count: int = 1


@dataclass
class NoxInstanceInfo:
    index: int | None
    name: str
    title: str
    running: bool = False
    pid: int | None = None
    raw_fields: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        state = "çalışıyor" if self.running else "kapalı"
        return f"{self.name} | {self.title} | {state}"


@dataclass
class PackageCommandCandidate:
    kind: str
    label: str
    package: str
    component: str = ""
    intent_action: str = ""
    uri_scheme: str = ""
    detail: str = ""


def parse_noxconsole_list(output: str) -> list[NoxInstanceInfo]:
    """NoxConsole list çıktısının eski ve yeni biçimlerini destekler."""
    instances: list[NoxInstanceInfo] = []
    for raw_line in output.splitlines():
        line = raw_line.strip().strip("\ufeff")
        if not line or "," not in line:
            continue
        fields = tuple(part.strip() for part in line.split(","))
        try:
            if fields[0].lstrip("-").isdigit() and len(fields) >= 3:
                index = int(fields[0])
                name = fields[1]
                title = fields[2]
                tail = fields[3:]
            elif len(fields) >= 2:
                index = None
                name = fields[0]
                title = fields[1]
                tail = fields[2:]
            else:
                continue
        except Exception:
            continue
        if not name:
            continue
        numeric_tail: list[int] = []
        for value in tail:
            try:
                numeric_tail.append(int(value, 10))
            except Exception:
                continue
        pid = None
        if numeric_tail:
            # Nox sürümüne göre PID son veya sondan bir önceki alanda olabilir.
            for candidate in reversed(numeric_tail[-3:]):
                if candidate > 0:
                    pid = candidate
                    break
        running = pid is not None
        instances.append(
            NoxInstanceInfo(
                index=index,
                name=name,
                title=title or name,
                running=running,
                pid=pid,
                raw_fields=fields,
            )
        )
    return instances


def safe_nox_instance_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", (value or "").strip())
    value = value.strip("_")
    if not value:
        raise ValueError("Nox kopya adı boş olamaz.")
    return value[:48]


def find_noxconsole(adb_path: str = "") -> str | None:
    candidates: list[Path] = []
    if adb_path:
        try:
            candidates.append(Path(adb_path).expanduser().resolve().parent / "NoxConsole.exe")
        except Exception:
            pass
    for raw in (
        r"C:\Program Files\Nox\bin\NoxConsole.exe",
        r"C:\Program Files (x86)\Nox\bin\NoxConsole.exe",
        r"C:\Program Files\Bignox\BigNoxVM\RT\Nox\bin\NoxConsole.exe",
        r"C:\Program Files (x86)\Bignox\BigNoxVM\RT\Nox\bin\NoxConsole.exe",
    ):
        candidates.append(Path(raw))
    located = shutil.which("NoxConsole.exe") or shutil.which("NoxConsole")
    if located:
        candidates.insert(0, Path(located))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def normalize_component(package: str, component: str) -> str:
    package = package.strip()
    component = component.strip()
    if not component:
        return ""
    if "/" in component:
        left, right = component.split("/", 1)
        left = left or package
        if right.startswith("."):
            return f"{left}/{right}"
        return f"{left}/{right}"
    if component.startswith("."):
        return f"{package}/{component}"
    if component.startswith(package + "."):
        return f"{package}/{component}"
    return f"{package}/{component}"


def valid_component(component: str) -> bool:
    return bool(
        re.fullmatch(
            r"[A-Za-z0-9._]+/[A-Za-z0-9._$]+",
            component.strip(),
        )
    )


def valid_intent_action(action: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._:-]+", action.strip()))


def parse_package_resolvers(
    dump_text: str,
    package: str,
) -> list[PackageCommandCandidate]:
    """
    dumpsys package çıktısındaki dışarıdan çözümlenebilen activity/receiver
    filtrelerini çıkarır. Bulunan komutlar ayrıca gerçek cihazda test edilmelidir.
    """
    section = ""
    current: dict[str, Any] | None = None
    parsed: list[PackageCommandCandidate] = []

    component_pattern = re.compile(
        r"(?<![A-Za-z0-9._])"
        + re.escape(package)
        + r"/(?:\.[A-Za-z0-9_$.-]+|[A-Za-z0-9_$.-]+)"
    )
    action_pattern = re.compile(r'Action:\s*"([^"]+)"')
    scheme_pattern = re.compile(r'Scheme:\s*"([^"]+)"')

    def flush() -> None:
        nonlocal current
        if not current:
            return
        component = current["component"]
        actions = sorted(current["actions"])
        schemes = sorted(current["schemes"])
        current_section = current["section"]

        if current_section == "activity":
            if not actions and not schemes:
                parsed.append(
                    PackageCommandCandidate(
                        kind="launch_activity",
                        label=f"Activity aç: {component}",
                        package=package,
                        component=component,
                        detail="Dışarıdan çözümlenen activity",
                    )
                )
            for action in actions:
                parsed.append(
                    PackageCommandCandidate(
                        kind="launch_activity",
                        label=f"Activity: {action}",
                        package=package,
                        component=component,
                        intent_action=action,
                        detail=component,
                    )
                )
            for scheme in schemes:
                parsed.append(
                    PackageCommandCandidate(
                        kind="open_uri",
                        label=f"Deep link şeması: {scheme}://",
                        package=package,
                        component=component,
                        uri_scheme=scheme,
                        detail=component,
                    )
                )
        elif current_section == "receiver":
            for action in actions:
                parsed.append(
                    PackageCommandCandidate(
                        kind="send_broadcast",
                        label=f"Broadcast: {action}",
                        package=package,
                        component=component,
                        intent_action=action,
                        detail=component,
                    )
                )
        current = None

    for raw_line in dump_text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("Activity Resolver Table:"):
            flush()
            section = "activity"
            continue
        if stripped.startswith("Receiver Resolver Table:"):
            flush()
            section = "receiver"
            continue
        if stripped.startswith("Service Resolver Table:") or stripped.startswith(
            "Registered ContentProviders:"
        ):
            flush()
            section = ""
            continue

        match = component_pattern.search(raw_line)
        if match and section in {"activity", "receiver"}:
            flush()
            current = {
                "section": section,
                "component": normalize_component(package, match.group(0)),
                "actions": set(),
                "schemes": set(),
            }
            continue

        if current:
            for action in action_pattern.findall(raw_line):
                if valid_intent_action(action):
                    current["actions"].add(action)
            for scheme in scheme_pattern.findall(raw_line):
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*", scheme):
                    current["schemes"].add(scheme)

    flush()

    unique: dict[tuple[str, str, str, str], PackageCommandCandidate] = {}
    for item in parsed:
        key = (
            item.kind,
            item.component,
            item.intent_action,
            item.uri_scheme,
        )
        unique[key] = item
    return list(unique.values())


def parse_resolved_activity(output: str, package: str) -> str:
    for raw_line in reversed(output.splitlines()):
        line = raw_line.strip()
        match = re.search(
            re.escape(package) + r"/(?:\.[A-Za-z0-9_$.-]+|[A-Za-z0-9_$.-]+)",
            line,
        )
        if match:
            return normalize_component(package, match.group(0))
    return ""


def parse_bounds(value: str) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value or "")
    if not match:
        return None
    return tuple(int(v) for v in match.groups())  # type: ignore[return-value]


def parse_ui_xml(path: Path) -> list[UiNode]:
    """UI ağacını, en yakın tıklanabilir üst öğeyi koruyarak okur."""
    nodes: list[UiNode] = []
    tree = ET.parse(path)

    def walk(elem: ET.Element, clickable_ancestor: tuple[int, int, int, int] | None = None) -> None:
        if elem.tag == "node":
            bounds = parse_bounds(elem.attrib.get("bounds", ""))
            if bounds is not None:
                clickable = elem.attrib.get("clickable", "false").lower() == "true"
                current_click_bounds = bounds if clickable else clickable_ancestor
                nodes.append(
                    UiNode(
                        package=elem.attrib.get("package", ""),
                        text=elem.attrib.get("text", ""),
                        resource_id=elem.attrib.get("resource-id", ""),
                        class_name=elem.attrib.get("class", ""),
                        content_desc=elem.attrib.get("content-desc", ""),
                        clickable=clickable,
                        enabled=elem.attrib.get("enabled", "true").lower() == "true",
                        bounds=bounds,
                        click_bounds=current_click_bounds,
                    )
                )
                clickable_ancestor = current_click_bounds

        for child in elem:
            walk(child, clickable_ancestor)

    walk(tree.getroot())
    return nodes


def find_best_node(nodes: list[UiNode], x: int, y: int) -> UiNode | None:
    candidates = [node for node in nodes if node.contains(x, y) and node.enabled]
    if not candidates:
        return None
    # Metin/id taşıyan küçük düğümü seç; tıklama anında tıklanabilir
    # üst satırın merkezini kullanırız.
    candidates.sort(
        key=lambda n: (
            not bool(n.resource_id or n.text or n.content_desc),
            n.click_bounds is None,
            n.area,
        )
    )
    return candidates[0]


def ui_command_key(
    package: str,
    resource_id: str,
    text: str,
    content_desc: str,
    class_name: str,
) -> str:
    """Aynı UI komutunu farklı taramalarda tek kayıtta birleştiren kararlı anahtar."""
    parts = [
        package.strip(),
        resource_id.strip(),
        text.strip(),
        content_desc.strip(),
        class_name.strip(),
    ]
    return "\x1f".join(parts)


def actionable_ui_node(node: UiNode, screen_size: tuple[int, int]) -> bool:
    if not node.enabled or node.area <= 0:
        return False
    if generic_fullscreen_node(node, screen_size):
        return False
    if node.resource_id == "android:id/content":
        return False
    has_selector = bool(node.resource_id or node.text or node.content_desc)
    has_click_target = bool(node.clickable or node.click_bounds is not None)
    return has_selector and has_click_target


def collect_actionable_ui_commands(
    nodes: list[UiNode],
    package: str,
    component: str,
    screen_size: tuple[int, int],
) -> list[UiCommandDefinition]:
    """
    Açık ekrandaki bütün kullanılabilir UI öğelerini tek taramada komutlaştırır.
    Kullanıcının her düğmeye tek tek tıklaması gerekmez.
    """
    commands: dict[str, UiCommandDefinition] = {}
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    for node in nodes:
        if package and node.package and node.package != package:
            continue
        if not actionable_ui_node(node, screen_size):
            continue

        key = ui_command_key(
            node.package or package,
            node.resource_id,
            node.text,
            node.content_desc,
            node.class_name,
        )
        target_x, target_y = node.target_center
        short_id = node.resource_id.rsplit("/", 1)[-1] if node.resource_id else ""
        label = node.text or node.content_desc or short_id or node.class_name or "UI öğesi"

        commands[key] = UiCommandDefinition(
            key=key,
            name=label,
            package=node.package or package,
            component=component,
            resource_id=node.resource_id,
            text=node.text,
            class_name=node.class_name,
            content_desc=node.content_desc,
            x=target_x,
            y=target_y,
            learned_at=now,
            seen_count=1,
        )

    return sorted(
        commands.values(),
        key=lambda item: (
            item.package.casefold(),
            item.component.casefold(),
            item.name.casefold(),
            item.resource_id.casefold(),
        ),
    )


def parse_current_component(output: str) -> str:
    patterns = [
        re.compile(
            r"mCurrentFocus=Window\{[^}]*? ([A-Za-z0-9._]+)/"
            r"(\.?[A-Za-z0-9._$]+)"
        ),
        re.compile(
            r"mResumedActivity:.*? ([A-Za-z0-9._]+)/"
            r"(\.?[A-Za-z0-9._$]+)"
        ),
        re.compile(
            r"ACTIVITY ([A-Za-z0-9._]+)/(\.?[A-Za-z0-9._$]+)"
        ),
    ]
    for pattern in patterns:
        match = pattern.search(output)
        if match:
            return normalize_component(
                match.group(1),
                f"{match.group(1)}/{match.group(2)}",
            )
    return ""


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_write_json(path: Path, value: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def detect_host_ip() -> str:
    """Windows'un Nox'a erişebilen varsayılan IPv4 adresini bulmaya çalışır."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Bağlantı kurulmaz; yalnızca işletim sisteminin seçtiği çıkış arayüzü öğrenilir.
            sock.connect(("8.8.8.8", 80))
            address = sock.getsockname()[0]
            if address and not address.startswith("127."):
                return address
        finally:
            sock.close()
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address and not address.startswith(("127.", "169.254.")):
                return address
    except OSError:
        pass
    return "127.0.0.1"


def validate_proxy_host(host: str) -> str:
    host = host.strip()
    if not host or not re.fullmatch(r"[A-Za-z0-9._-]+", host):
        raise ValueError("Proxy IP/host değeri geçersiz.")
    return host


def validate_android_file_path(value: str) -> str:
    value = (value or "").strip().replace("\\", "/")
    if not value.startswith("/sdcard/"):
        raise ValueError(
            "Android sertifika yolu /sdcard/ ile başlamalı."
        )
    if value.endswith("/"):
        raise ValueError("Android sertifika yolu dosya adı içermeli.")
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise ValueError("Android sertifika yolu geçersiz.")
    return value


def certificate_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_proxy_port(port: int | str) -> int:
    value = int(port)
    if not 1 <= value <= 65535:
        raise ValueError("Proxy portu 1–65535 arasında olmalı.")
    return value


def probable_nox_serial(serial: str) -> bool:
    lowered = serial.lower()
    return (
        lowered.startswith("127.0.0.1:")
        or lowered.startswith("localhost:")
        or lowered.startswith("emulator-")
    )


def parse_tasklist_charles_pids(output: str) -> set[int]:
    """Windows tasklist CSV çıktısından Charles PID'lerini çıkarır."""
    pids: set[int] = set()
    for row in csv.reader(io.StringIO(output)):
        if len(row) < 2:
            continue
        image_name = row[0].strip().lower()
        if not (
            image_name == "charles.exe"
            or image_name == "charles64.exe"
            or image_name.startswith("charles")
        ):
            continue
        try:
            pids.add(int(row[1].replace(",", "").strip()))
        except ValueError:
            continue
    return pids


def parse_netstat_listeners(output: str) -> list[dict[str, Any]]:
    """netstat -ano TCP LISTENING satırlarını ayrıştırır."""
    listeners: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.upper().startswith("TCP"):
            continue
        parts = line.split()
        if len(parts) < 5 or parts[-2].upper() not in {"LISTENING", "DİNLENİYOR"}:
            continue
        local = parts[1]
        try:
            pid = int(parts[-1])
        except ValueError:
            continue

        if local.startswith("[") and "]:" in local:
            host, port_text = local.rsplit("]:", 1)
            host = host[1:]
        else:
            if ":" not in local:
                continue
            host, port_text = local.rsplit(":", 1)
        try:
            port = int(port_text)
        except ValueError:
            continue
        listeners.append({"host": host, "port": port, "pid": pid})
    return listeners


def windows_charles_pids() -> set[int]:
    if os.name != "nt":
        return set()
    result = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        return set()
    return parse_tasklist_charles_pids(result.stdout)


def windows_tcp_listeners() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    result = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=12,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        return []
    return parse_netstat_listeners(result.stdout)


def probe_http_proxy(port: int, timeout: float = 0.8) -> bool:
    """
    Yerel portun HTTP proxy gibi cevap verip vermediğini kontrol eder.
    Dışarıya hassas veri göndermez; ulaşılamayan yerel bir hedef ister.
    """
    port = validate_proxy_port(port)
    request = (
        b"GET http://127.0.0.1:1/ HTTP/1.1\r\n"
        b"Host: 127.0.0.1:1\r\n"
        b"Connection: close\r\n"
        b"Proxy-Connection: close\r\n\r\n"
    )
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            data = sock.recv(128)
            return data.startswith(b"HTTP/")
    except OSError:
        return False


def find_charles_executable() -> str | None:
    candidates = [
        shutil.which("Charles"),
        shutil.which("Charles.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Charles", "Charles.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Charles", "Charles.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Charles", "Charles.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return None


def start_charles_if_available() -> str:
    executable = find_charles_executable()
    if not executable:
        raise RuntimeError(
            "Charles.exe bulunamadı. Charles'ı bir kez elle açın veya kurulum yolunu PATH'e ekleyin."
        )
    subprocess.Popen(
        [executable],
        cwd=str(Path(executable).parent),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    return executable


def detect_charles_proxy(preferred_port: int = 8888) -> dict[str, Any]:
    """
    Charles sürecine ait dinleyen TCP portlarını bulur ve HTTP proxy olarak
    cevap veren portu seçer. Varsayılan 8888'e öncelik verir.
    """
    preferred_port = validate_proxy_port(preferred_port)
    pids = windows_charles_pids()
    listeners = windows_tcp_listeners()

    owned = [item for item in listeners if item["pid"] in pids]
    candidate_ports: list[int] = []

    def add_port(port: int) -> None:
        if port not in candidate_ports:
            candidate_ports.append(port)

    # Charles'ın normal HTTP proxy portu öncelikli.
    if any(item["port"] == preferred_port for item in owned):
        add_port(preferred_port)
    if any(item["port"] == 8888 for item in owned):
        add_port(8888)

    # Düşük sistem portlarını ele; Charles dynamic port kullanıyorsa kalanları dene.
    for item in sorted(owned, key=lambda value: value["port"]):
        if item["port"] >= 1024:
            add_port(item["port"])

    # Charles süreci görülüyor ama socket-PID eşlemesi alınamadıysa varsayılanı dene.
    if pids:
        add_port(preferred_port)
        add_port(8888)

    for port in candidate_ports:
        if probe_http_proxy(port):
            matching = [item for item in owned if item["port"] == port]
            return {
                "running": True,
                "port": port,
                "pids": sorted(pids),
                "listen_hosts": sorted({item["host"] for item in matching}),
                "detected_by": "Charles process + HTTP proxy probe",
            }

    if not pids:
        raise RuntimeError("Charles çalışmıyor veya Charles.exe süreci bulunamadı.")
    raise RuntimeError(
        "Charles çalışıyor ancak HTTP proxy portu algılanamadı. "
        "Charles > Proxy Settings bölümünde HTTP Proxy açık olmalı."
    )


def normalize_proxy_hostname(host: str) -> str:
    host = (host or "").strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host


def parse_host_port(authority: str, default_port: int) -> tuple[str, int]:
    authority = authority.strip()
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            raise ValueError(f"Geçersiz IPv6 adresi: {authority}")
        host = authority[1:end]
        port = default_port
        remainder = authority[end + 1:]
        if remainder.startswith(":"):
            port = int(remainder[1:])
        return normalize_proxy_hostname(host), validate_proxy_port(port)

    if authority.count(":") == 1:
        host, possible_port = authority.rsplit(":", 1)
        if possible_port.isdigit():
            return normalize_proxy_hostname(host), validate_proxy_port(possible_port)
    return normalize_proxy_hostname(authority), validate_proxy_port(default_port)


def parse_proxy_request_head(head: bytes) -> tuple[str, str, str, dict[str, str]]:
    text = head.decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n")
    first = lines[0].split(" ", 2)
    if len(first) != 3:
        raise ValueError("Geçersiz HTTP proxy istek satırı.")
    method, target, version = first
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return method.upper(), target, version, headers


def request_destination(
    method: str,
    target: str,
    headers: dict[str, str],
) -> tuple[str, int]:
    if method == "CONNECT":
        return parse_host_port(target, 443)

    parsed = urlsplit(target)
    if parsed.hostname:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        return normalize_proxy_hostname(parsed.hostname), validate_proxy_port(port)

    host_header = headers.get("host", "")
    return parse_host_port(host_header, 80)


def rewrite_request_for_origin(head: bytes, target: str) -> bytes:
    """Proxy-form isteği origin-form'a çevirir; diğer header'ları korur."""
    parsed = urlsplit(target)
    if not parsed.scheme or not parsed.netloc:
        return head
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    text = head.decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n")
    first = lines[0].split(" ", 2)
    if len(first) == 3:
        lines[0] = f"{first[0]} {path} {first[2]}"
    # Doğrudan origin bağlantısında Proxy-Connection header'ı kullanılmaz.
    lines = [
        line for line in lines
        if not line.lower().startswith("proxy-connection:")
    ]
    return "\r\n".join(lines).encode("iso-8859-1", errors="replace")


def host_matches_rules(host: str, rules: list[str]) -> bool:
    host = normalize_proxy_hostname(host)
    for raw_rule in rules:
        rule = normalize_proxy_hostname(raw_rule)
        if not rule:
            continue
        if rule.startswith("*."):
            suffix = rule[1:]  # ".example.com"
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == rule:
            return True
    return False


def read_http_head(sock: socket.socket, max_bytes: int = 131072) -> tuple[bytes, bytes]:
    data = bytearray()
    marker = b"\r\n\r\n"
    while marker not in data:
        chunk = sock.recv(8192)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ValueError("HTTP header sınırı aşıldı.")
    position = data.find(marker)
    if position < 0:
        raise ValueError("Eksik HTTP proxy header'ı.")
    end = position + len(marker)
    return bytes(data[:end]), bytes(data[end:])


class SelectiveProxyGate:
    """
    Nox'un tüm HTTP proxy trafiğini alır:
      - hedef host -> Charles
      - diğer hostlar -> doğrudan internet

    TLS çözmez ve içerik kaydetmez; CONNECT host'una göre yönlendirir.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self.listen_host = "127.0.0.1"
        self.listen_port = 8899
        self.upstream_host = "127.0.0.1"
        self.upstream_port = 8888
        self.allowed_hosts = ["outfox.api.zynga.com"]
        self.target_connections = 0
        self.bypassed_connections = 0
        self.errors = 0
        self.active_connections = 0

    def configure(
        self,
        *,
        listen_port: int,
        upstream_port: int,
        allowed_hosts: list[str],
    ) -> None:
        listen_port = validate_proxy_port(listen_port)
        upstream_port = validate_proxy_port(upstream_port)
        normalized = [
            normalize_proxy_hostname(item)
            for item in allowed_hosts
            if normalize_proxy_hostname(item)
        ]
        if not normalized:
            raise ValueError("En az bir hedef host gerekli.")

        with self._lock:
            restart = self._server is not None and listen_port != self.listen_port
            self.listen_port = listen_port
            self.upstream_port = upstream_port
            self.allowed_hosts = normalized
        if restart:
            self.stop()
            self.start()

    def start(self) -> None:
        with self._lock:
            if self._server is not None:
                return

            gate = self

            class GateServer(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True

            class GateHandler(socketserver.BaseRequestHandler):
                def handle(self) -> None:
                    gate.handle_client(self.request, self.client_address)

            server = GateServer((self.listen_host, self.listen_port), GateHandler)
            self._server = server
            self._thread = threading.Thread(
                target=server.serve_forever,
                name="NoxFlowSelectiveProxyGate",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._server is not None,
                "listen_port": self.listen_port,
                "upstream_port": self.upstream_port,
                "allowed_hosts": list(self.allowed_hosts),
                "target_connections": self.target_connections,
                "bypassed_connections": self.bypassed_connections,
                "errors": self.errors,
                "active_connections": self.active_connections,
            }

    def _increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, name, int(getattr(self, name)) + amount)

    def _route_is_target(self, host: str) -> bool:
        with self._lock:
            rules = list(self.allowed_hosts)
        return host_matches_rules(host, rules)

    def _connect_upstream(self) -> socket.socket:
        with self._lock:
            host = self.upstream_host
            port = self.upstream_port
        return socket.create_connection((host, port), timeout=15)

    @staticmethod
    def _tunnel(left: socket.socket, right: socket.socket) -> None:
        sockets = [left, right]
        for sock in sockets:
            sock.settimeout(None)
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, 60)
            if exceptional:
                return
            if not readable:
                continue
            for source in readable:
                destination = right if source is left else left
                data = source.recv(65536)
                if not data:
                    return
                destination.sendall(data)

    @staticmethod
    def _send_bad_gateway(client: socket.socket, detail: str) -> None:
        body = ("NoxFlow selective proxy error: " + detail).encode(
            "utf-8", errors="replace"
        )[:1024]
        response = (
            b"HTTP/1.1 502 Bad Gateway\r\n"
            b"Connection: close\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            + body
        )
        try:
            client.sendall(response)
        except OSError:
            pass

    def handle_client(
        self,
        client: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        del client_address
        remote: socket.socket | None = None
        self._increment("active_connections", 1)
        try:
            client.settimeout(15)
            head, buffered = read_http_head(client)
            method, target, version, headers = parse_proxy_request_head(head)
            host, port = request_destination(method, target, headers)
            target_route = self._route_is_target(host)

            if target_route:
                self._increment("target_connections", 1)
                remote = self._connect_upstream()
                # Charles normal bir forward proxy olarak orijinal isteği alır.
                remote.sendall(head)
                if buffered:
                    remote.sendall(buffered)
                self._tunnel(client, remote)
                return

            self._increment("bypassed_connections", 1)
            remote = socket.create_connection((host, port), timeout=15)

            if method == "CONNECT":
                client.sendall(
                    f"{version} 200 Connection Established\r\n"
                    "Proxy-Agent: NoxFlow-Selective-Gate\r\n\r\n".encode("ascii")
                )
                if buffered:
                    remote.sendall(buffered)
            else:
                remote.sendall(rewrite_request_for_origin(head, target))
                if buffered:
                    remote.sendall(buffered)

            self._tunnel(client, remote)
        except Exception as exc:
            self._increment("errors", 1)
            self._send_bad_gateway(client, str(exc))
        finally:
            self._increment("active_connections", -1)
            try:
                client.close()
            except OSError:
                pass
            if remote is not None:
                try:
                    remote.close()
                except OSError:
                    pass


def minimize_noxplayer_windows() -> int:
    """Yalnızca NoxPlayer emülatör pencerelerini küçültür; Nox Asst'ı etkilemez."""
    if os.name != "nt":
        return 0

    user32 = ctypes.windll.user32
    SW_MINIMIZE = 6
    minimized = 0

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )

    @EnumWindowsProc
    def callback(hwnd, _lparam):
        nonlocal minimized
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip().lower()
        # Örnek: NoxPlayer3(1) 7.0.6.2
        if title.startswith("noxplayer"):
            user32.ShowWindow(hwnd, SW_MINIMIZE)
            minimized += 1
        return True

    user32.EnumWindows(callback, 0)
    return minimized


def generic_fullscreen_node(
    node: UiNode | None,
    screen_size: tuple[int, int],
) -> bool:
    if node is None:
        return True
    if node.resource_id == "android:id/content":
        return True
    sw, sh = screen_size
    screen_area = max(1, sw * sh)
    return (
        node.class_name.endswith("FrameLayout")
        and node.area >= screen_area * 0.70
        and not (node.text or node.content_desc)
    )


class NoxConsoleError(RuntimeError):
    pass


class NoxConsoleClient:
    def __init__(self, executable: str):
        self.executable = executable

    def run(
        self,
        args: list[str],
        timeout: float = 120.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        if not Path(self.executable).is_file():
            raise NoxConsoleError(f"NoxConsole.exe bulunamadı: {self.executable}")
        result = subprocess.run(
            [self.executable, *args],
            cwd=str(Path(self.executable).parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if check and result.returncode != 0:
            raise NoxConsoleError(
                (result.stderr or result.stdout or "NoxConsole komutu başarısız").strip()
            )
        return result

    def list_instances(self) -> list[NoxInstanceInfo]:
        result = self.run(["list"], timeout=30)
        return parse_noxconsole_list(result.stdout)

    def find_instance(self, name: str) -> NoxInstanceInfo | None:
        for item in self.list_instances():
            if item.name == name or item.title == name:
                return item
        return None

    def copy_instance(
        self,
        source: NoxInstanceInfo,
        new_name: str,
        timeout_s: float = 900.0,
    ) -> None:
        new_name = safe_nox_instance_name(new_name)
        attempts = [source.name]
        if source.title and source.title not in attempts:
            attempts.append(source.title)
        errors: list[str] = []
        for source_name in attempts:
            result = self.run(
                ["copy", f"-name:{new_name}", f"-from:{source_name}"],
                timeout=max(60.0, timeout_s),
                check=False,
            )
            if result.returncode == 0:
                return
            errors.append((result.stderr or result.stdout or source_name).strip())
        raise NoxConsoleError("Klonlama başarısız: " + " | ".join(errors))

    def launch(self, name: str) -> None:
        self.run(["launch", f"-name:{safe_nox_instance_name(name)}"], timeout=60)

    def quit(self, name: str) -> None:
        self.run(["quit", f"-name:{safe_nox_instance_name(name)}"], timeout=60, check=False)

    def remove(self, name: str) -> None:
        self.run(["remove", f"-name:{safe_nox_instance_name(name)}"], timeout=300)

    def modify_basic(self, name: str, resolution: str, cpu: int, memory: int) -> None:
        args = ["modify", f"-name:{safe_nox_instance_name(name)}"]
        if resolution:
            args.append(f"-resolution:{resolution}")
        if cpu:
            args.append(f"-cpu:{int(cpu)}")
        if memory:
            args.append(f"-memory:{int(memory)}")
        self.run(args, timeout=120)


class AdbError(RuntimeError):
    pass


class AdbClient:
    def __init__(self, adb_path: str, serial: str = ""):
        self.adb_path = adb_path
        self.serial = serial

    def _base(self) -> list[str]:
        cmd = [self.adb_path]
        if self.serial:
            cmd.extend(["-s", self.serial])
        return cmd

    def run(
        self,
        args: list[str],
        timeout: float = 30,
        binary: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        cmd = self._base() + args
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=not binary,
            creationflags=creationflags,
        )
        if check and result.returncode != 0:
            stderr = result.stderr.decode(errors="replace") if binary else result.stderr
            stdout = result.stdout.decode(errors="replace") if binary else result.stdout
            raise AdbError((stderr or stdout or "ADB komutu başarısız").strip())
        return result

    def devices(self) -> list[str]:
        result = subprocess.run(
            [self.adb_path, "devices"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            raise AdbError((result.stderr or result.stdout).strip())
        devices = []
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])
        return devices

    def shell(self, command: str, timeout: float = 30) -> str:
        result = self.run(["shell", command], timeout=timeout)
        return result.stdout.strip()

    def reverse_port(self, device_port: int, host_port: int) -> None:
        device_port = validate_proxy_port(device_port)
        host_port = validate_proxy_port(host_port)
        self.run(
            ["reverse", f"tcp:{device_port}", f"tcp:{host_port}"],
            timeout=20,
        )

    def remove_reverse_port(self, device_port: int) -> None:
        device_port = validate_proxy_port(device_port)
        self.run(
            ["reverse", "--remove", f"tcp:{device_port}"],
            timeout=15,
            check=False,
        )

    def set_http_proxy(self, host: str, port: int) -> str:
        host = validate_proxy_host(host)
        port = validate_proxy_port(port)
        self.shell(f"settings put global http_proxy {host}:{port}", timeout=20)
        return self.get_http_proxy()

    def get_http_proxy(self) -> str:
        return self.shell("settings get global http_proxy", timeout=15).strip()

    def clear_http_proxy(self, reverse_port: int | None = None) -> str:
        # Nox/Android sürümlerine göre iki temizleme şeklinin ikisini de uygula.
        self.shell("settings put global http_proxy :0", timeout=15)
        self.shell("settings delete global http_proxy", timeout=15)
        if reverse_port is not None:
            self.remove_reverse_port(reverse_port)
        return self.get_http_proxy()

    def boot_completed(self) -> bool:
        values = []
        for prop in ("sys.boot_completed", "dev.bootcomplete"):
            try:
                values.append(self.shell(f"getprop {prop}", timeout=8).strip())
            except Exception:
                continue
        return "1" in values

    def wait_for_boot(self, timeout_s: float = 180.0) -> None:
        deadline = time.monotonic() + max(1.0, timeout_s)
        last_value = ""
        while time.monotonic() < deadline:
            try:
                last_value = self.shell(
                    "getprop sys.boot_completed",
                    timeout=8,
                ).strip()
                if last_value == "1":
                    return
            except Exception:
                pass
            time.sleep(2.0)
        raise AdbError(
            f"Android açılışı {timeout_s:g} saniyede tamamlanmadı "
            f"(sys.boot_completed={last_value!r})."
        )

    def package_installed(self, package: str) -> bool:
        package = self.validate_package(package)
        result = self.run(
            ["shell", "pm", "path", package],
            timeout=15,
            check=False,
        )
        text = (result.stdout or "").strip()
        return result.returncode == 0 and text.startswith("package:")

    def ensure_android_directory(self, directory: str) -> None:
        directory = directory.strip().replace("\\", "/")
        if not directory.startswith("/sdcard/"):
            raise AdbError("Yalnızca /sdcard/ altındaki klasörler destekleniyor.")
        self.shell(f"mkdir -p {shlex.quote(directory)}", timeout=15)

    def push_file(self, source: Path, destination: str) -> None:
        if not source.is_file():
            raise AdbError(f"Yerel dosya bulunamadı: {source}")
        destination = validate_android_file_path(destination)
        parent = destination.rsplit("/", 1)[0]
        self.ensure_android_directory(parent)
        result = self.run(
            ["push", str(source), destination],
            timeout=90,
            check=False,
        )
        if result.returncode != 0:
            raise AdbError(
                (result.stderr or result.stdout or "ADB push başarısız").strip()
            )

    def capture_screen(self, output: Path) -> None:
        remote = "/sdcard/noxflow_screen.png"
        self.shell(f"screencap -p {remote}", timeout=20)
        self.run(["pull", remote, str(output)], timeout=30)
        self.shell(f"rm -f {remote}", timeout=10)

    def dump_ui(self, output: Path) -> None:
        remote = "/sdcard/noxflow_window.xml"
        # Bazı Android sürümlerinde ilk deneme boş dönebilir.
        self.shell(f"uiautomator dump {remote}", timeout=20)
        self.run(["pull", remote, str(output)], timeout=30)
        self.shell(f"rm -f {remote}", timeout=10)

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {x} {y}", timeout=10)

    def long_press(self, x: int, y: int, duration_ms: int) -> None:
        self.shell(f"input swipe {x} {y} {x} {y} {duration_ms}", timeout=15)

    def double_tap(self, x: int, y: int) -> None:
        self.tap(x, y)
        time.sleep(0.12)
        self.tap(x, y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {duration_ms}", timeout=15)

    def keyevent(self, key: str) -> None:
        self.shell(f"input keyevent {key}", timeout=10)

    def current_package(self) -> str:
        probes = [
            "dumpsys window windows",
            "dumpsys activity activities",
            "dumpsys activity top",
        ]
        patterns = [
            re.compile(r"mCurrentFocus=Window\{[^ ]+ [^ ]+ ([A-Za-z0-9._]+)/"),
            re.compile(r"mResumedActivity:.*? ([A-Za-z0-9._]+)/"),
            re.compile(r"ACTIVITY ([A-Za-z0-9._]+)/"),
        ]
        for command in probes:
            try:
                text = self.shell(command, timeout=15)
            except Exception:
                continue
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    return match.group(1)
        return ""

    def boot_session_id(self) -> str:
        """
        Aynı Nox açık kaldığı sürece sabit, yeniden başlatıldığında değişen kimlik.
        Linux boot_id birinci tercihtir.
        """
        try:
            boot_id = self.shell(
                "cat /proc/sys/kernel/random/boot_id",
                timeout=10,
            ).strip().lower()
            if re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                r"[0-9a-f]{4}-[0-9a-f]{12}",
                boot_id,
            ):
                return f"boot:{boot_id}"
        except Exception:
            pass

        try:
            first_boot = self.shell(
                "getprop ro.runtime.firstboot",
                timeout=10,
            ).strip()
            if first_boot.isdigit() and int(first_boot) > 0:
                return f"firstboot:{first_boot}"
        except Exception:
            pass

        try:
            uptime_text = self.shell("cat /proc/uptime", timeout=10)
            uptime_seconds = float(uptime_text.split()[0])
            # Yaklaşık açılış zamanı 30 saniyelik kovaya alınır; aynı boot için
            # küçük zamanlama farkları ayrı oturum üretmez.
            boot_epoch = int((time.time() - uptime_seconds) // 30) * 30
            return f"uptime:{boot_epoch}"
        except Exception as exc:
            raise AdbError(f"Nox açılış kimliği alınamadı: {exc}")

    def current_component(self) -> str:
        probes = [
            "dumpsys window windows",
            "dumpsys activity activities",
            "dumpsys activity top",
        ]
        for command in probes:
            try:
                component = parse_current_component(
                    self.shell(command, timeout=15)
                )
            except Exception:
                continue
            if component:
                return component
        return ""

    def list_packages(self, third_party_only: bool = True) -> list[str]:
        flag = "-3" if third_party_only else ""
        text = self.shell(f"pm list packages {flag}".strip(), timeout=20)
        packages = []
        for line in text.splitlines():
            if line.startswith("package:"):
                packages.append(line.split(":", 1)[1].strip())
        return sorted(packages)

    def package_dump(self, package: str) -> str:
        package = self.validate_package(package)
        return self.shell(f"dumpsys package {shlex.quote(package)}", timeout=45)

    def resolve_launcher_activity(self, package: str) -> str:
        package = self.validate_package(package)
        commands = [
            (
                "cmd package resolve-activity --brief "
                "-a android.intent.action.MAIN "
                "-c android.intent.category.LAUNCHER "
                f"{shlex.quote(package)}"
            ),
            (
                "pm resolve-activity --brief "
                "-a android.intent.action.MAIN "
                "-c android.intent.category.LAUNCHER "
                f"{shlex.quote(package)}"
            ),
        ]
        for command in commands:
            try:
                result = self.shell(command, timeout=20)
            except Exception:
                continue
            component = parse_resolved_activity(result, package)
            if component:
                return component
        return ""

    def start_activity(
        self,
        component: str,
        intent_action: str = "",
        data_uri: str = "",
    ) -> None:
        component = component.strip()
        if not valid_component(component):
            raise AdbError(f"Geçersiz activity component: {component!r}")
        parts = ["am", "start", "-W", "-n", shlex.quote(component)]
        if intent_action:
            if not valid_intent_action(intent_action):
                raise AdbError(f"Geçersiz intent action: {intent_action!r}")
            parts.extend(["-a", shlex.quote(intent_action)])
        if data_uri:
            parts.extend(["-d", shlex.quote(data_uri)])
        result = self.shell(" ".join(parts), timeout=30)
        lowered = result.lower()
        if "error:" in lowered or "exception" in lowered or "securityexception" in lowered:
            raise AdbError(result)

    def send_broadcast(
        self,
        intent_action: str,
        component: str = "",
    ) -> None:
        if not valid_intent_action(intent_action):
            raise AdbError(f"Geçersiz broadcast action: {intent_action!r}")
        parts = ["am", "broadcast", "-a", shlex.quote(intent_action)]
        if component:
            if not valid_component(component):
                raise AdbError(f"Geçersiz receiver component: {component!r}")
            parts.extend(["-n", shlex.quote(component)])
        result = self.shell(" ".join(parts), timeout=30)
        lowered = result.lower()
        if "securityexception" in lowered or "permission denial" in lowered:
            raise AdbError(result)

    def open_uri(self, uri: str, package: str = "") -> None:
        uri = uri.strip()
        if not uri or ":" not in uri:
            raise AdbError("Geçerli bir URI gerekli. Örnek: uygulama://sayfa")
        parts = [
            "am", "start", "-W",
            "-a", "android.intent.action.VIEW",
            "-d", shlex.quote(uri),
        ]
        if package:
            package = self.validate_package(package)
            parts.extend(["-p", shlex.quote(package)])
        result = self.shell(" ".join(parts), timeout=30)
        lowered = result.lower()
        if "error:" in lowered or "unable to resolve" in lowered or "exception" in lowered:
            raise AdbError(result)

    def launch_package(self, package: str) -> None:
        if not package:
            raise AdbError("Paket adı boş.")
        # Launcher activity bilinmese de çoğu uygulamada çalışır.
        result = self.shell(
            f"monkey -p {package} -c android.intent.category.LAUNCHER 1",
            timeout=20,
        )
        if "No activities found" in result:
            raise AdbError(f"Başlatılabilir activity bulunamadı: {package}")

    @staticmethod
    def validate_package(package: str) -> str:
        package = package.strip()
        if not re.fullmatch(r"[A-Za-z0-9._]+", package):
            raise AdbError(f"Geçersiz paket adı: {package!r}")
        return package

    def force_stop(self, package: str) -> None:
        package = self.validate_package(package)
        self.shell(f"am force-stop {package}", timeout=15)

    def clear_app_data(self, package: str) -> None:
        """Android Ayarlarındaki 'Clear storage' ile aynı kullanıcı verisi temizliği."""
        package = self.validate_package(package)
        result = self.shell(f"pm clear {package}", timeout=60)
        if "Success" not in result:
            raise AdbError(result or f"Uygulama verisi temizlenemedi: {package}")

    def open_app_details(self, package: str) -> None:
        package = self.validate_package(package)
        result = self.shell(
            f"am start -W -a android.settings.APPLICATION_DETAILS_SETTINGS "
            f"-d package:{package}",
            timeout=30,
        )
        if "Error:" in result or "Exception" in result:
            raise AdbError(result)

    def open_app_storage(self, package: str) -> None:
        """
        Kaydırma yapmadan uygulamanın Storage & cache ekranını açmayı dener.
        AOSP/Nox Android 12'de özel Settings action'ı bulunur.
        """
        package = self.validate_package(package)
        attempts = [
            (
                "com.android.settings.APP_STORAGE_SETTINGS",
                f"am start -W -a com.android.settings.APP_STORAGE_SETTINGS "
                f"-d package:{package}",
            ),
            (
                "AppStorageSettingsActivity",
                "am start -W -n "
                "'com.android.settings/.Settings$AppStorageSettingsActivity' "
                f"-d package:{package}",
            ),
        ]
        errors: list[str] = []
        for label, command in attempts:
            try:
                result = self.shell(command, timeout=30)
                if "Error:" not in result and "Exception" not in result and "unable to resolve" not in result.lower():
                    return
                errors.append(f"{label}: {result}")
            except Exception as exc:
                errors.append(f"{label}: {exc}")

        # Son güvenli geri dönüş: uygulama bilgi sayfası.
        try:
            self.open_app_details(package)
        except Exception as exc:
            errors.append(f"App details: {exc}")
        raise AdbError(
            "Doğrudan Storage & cache ekranı bu Android sürümünde açılamadı. "
            "Uygulama bilgi sayfası açıldı. Ayrıntı: " + " | ".join(errors)
        )


class NodePickerDialog(Toplevel):
    def __init__(
        self,
        master: Tk,
        title: str,
        action: str,
        node: UiNode | None,
        x: int,
        y: int,
        package: str,
        force_coordinate: bool = False,
    ):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self.result: FlowStep | None = None
        self.transient(master)
        self.grab_set()

        self.name_var = StringVar(value=self.default_name(action, node))
        self.wait_var = DoubleVar(value=1.0)
        self.duration_var = IntVar(value=800)
        self.timeout_var = DoubleVar(value=30.0)
        self.poll_var = DoubleVar(value=0.8)
        self.force_coordinate = force_coordinate
        self.fallback_var = BooleanVar(value=not force_coordinate)

        body = ttk.Frame(self, padding=12)
        body.pack(fill=BOTH, expand=True)

        ttk.Label(body, text="Adım adı").grid(row=0, column=0, sticky="w")
        ttk.Entry(body, textvariable=self.name_var, width=48).grid(
            row=0, column=1, sticky="ew", padx=(8, 0)
        )

        ttk.Label(body, text="İşlem").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(body, text=action).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))

        ttk.Label(body, text="Paket").grid(row=2, column=0, sticky="w")
        ttk.Label(body, text=(node.package if node and node.package else package) or "—").grid(
            row=2, column=1, sticky="w", padx=(8, 0)
        )

        ttk.Label(body, text="resource-id").grid(row=3, column=0, sticky="w")
        resource_text = "KULLANILMAYACAK — kesin koordinat" if force_coordinate else ((node.resource_id if node else "") or "—")
        ttk.Label(body, text=resource_text, wraplength=420).grid(
            row=3, column=1, sticky="w", padx=(8, 0)
        )

        ttk.Label(body, text="Text / Açıklama").grid(row=4, column=0, sticky="w")
        shown_text = ""
        if node:
            shown_text = node.text or node.content_desc
        ttk.Label(body, text=shown_text or "—", wraplength=420).grid(
            row=4, column=1, sticky="w", padx=(8, 0)
        )

        ttk.Label(body, text="Koordinat").grid(row=5, column=0, sticky="w")
        ttk.Label(body, text=f"{x}, {y}").grid(row=5, column=1, sticky="w", padx=(8, 0))

        ttk.Label(body, text="Sonra bekle (sn)").grid(row=6, column=0, sticky="w", pady=(8, 0))
        ttk.Spinbox(body, from_=0, to=120, increment=0.25, textvariable=self.wait_var, width=10).grid(
            row=6, column=1, sticky="w", padx=(8, 0), pady=(8, 0)
        )

        next_row = 7
        if action in {"long_press", "swipe"}:
            ttk.Label(body, text="Süre (ms)").grid(row=next_row, column=0, sticky="w")
            ttk.Spinbox(body, from_=100, to=10000, increment=100, textvariable=self.duration_var, width=10).grid(
                row=next_row, column=1, sticky="w", padx=(8, 0)
            )
            next_row += 1

        if action == "wait_ui_tap":
            ttk.Label(body, text="En fazla bekle (sn)").grid(row=next_row, column=0, sticky="w")
            ttk.Spinbox(body, from_=1, to=3600, increment=1, textvariable=self.timeout_var, width=10).grid(
                row=next_row, column=1, sticky="w", padx=(8, 0)
            )
            next_row += 1
            ttk.Label(body, text="Kontrol aralığı (sn)").grid(row=next_row, column=0, sticky="w")
            ttk.Spinbox(body, from_=0.2, to=30, increment=0.1, textvariable=self.poll_var, width=10).grid(
                row=next_row, column=1, sticky="w", padx=(8, 0)
            )
            next_row += 1

        fallback_row = next_row

        if force_coordinate:
            ttk.Label(
                body,
                text="Bu adım UI öğesi aramaz; seçtiğiniz Nox pikseline doğrudan basar.",
            ).grid(row=fallback_row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        else:
            ttk.Checkbutton(
                body,
                text="Öğe bulunamazsa kayıtlı koordinatı kullan",
                variable=self.fallback_var,
            ).grid(row=fallback_row, column=0, columnspan=2, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=fallback_row + 1, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="İptal", command=self.destroy).pack(side=RIGHT)
        ttk.Button(buttons, text="Adımı Ekle", command=lambda: self.accept(action, node, x, y, package)).pack(
            side=RIGHT, padx=(0, 8)
        )

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.wait_window(self)

    @staticmethod
    def default_name(action: str, node: UiNode | None) -> str:
        label = {
            "tap": "Tıkla",
            "wait_ui_tap": "Öğeyi bekle ve tıkla",
            "long_press": "Uzun bas",
            "double_tap": "Çift tıkla",
        }.get(action, action)
        if node:
            target = node.text or node.content_desc or node.resource_id.rsplit("/", 1)[-1]
            if target:
                return f"{label}: {target}"
        return label

    def accept(self, action: str, node: UiNode | None, x: int, y: int, package: str) -> None:
        try:
            wait_after = float(self.wait_var.get())
            duration_ms = int(self.duration_var.get())
            timeout_s = float(self.timeout_var.get())
            poll_interval = float(self.poll_var.get())
            if timeout_s <= 0 or poll_interval <= 0:
                raise ValueError
        except Exception:
            messagebox.showerror(
                "Geçersiz değer",
                "Bekleme, süre, zaman aşımı ve kontrol aralığı geçerli sayı olmalı.",
                parent=self,
            )
            return

        self.result = FlowStep(
            action=action,
            name=self.name_var.get().strip() or action,
            package=(package if self.force_coordinate else (node.package if node and node.package else package)),
            resource_id=("" if self.force_coordinate else (node.resource_id if node else "")),
            text=("" if self.force_coordinate else (node.text if node else "")),
            class_name=("" if self.force_coordinate else (node.class_name if node else "")),
            content_desc=("" if self.force_coordinate else (node.content_desc if node else "")),
            x=x,
            y=y,
            duration_ms=duration_ms,
            wait_after=max(0.0, wait_after),
            fallback_to_coordinate=True,
            timeout_s=timeout_s,
            poll_interval=poll_interval,
        )
        self.destroy()


class VisualWaitDialog(Toplevel):
    def __init__(
        self,
        master: Tk,
        screen: Image.Image,
        x: int,
        y: int,
        package: str,
    ):
        super().__init__(master)
        self.title("Görsel butonu öğret")
        self.resizable(False, False)
        self.result: FlowStep | None = None
        self.transient(master)
        self.grab_set()

        self.name_var = StringVar(value="Görseli bekle ve tıkla")
        self.width_var = IntVar(value=220)
        self.height_var = IntVar(value=100)
        self.similarity_var = DoubleVar(value=0.90)
        self.timeout_var = DoubleVar(value=45.0)
        self.poll_var = DoubleVar(value=0.8)
        self.wait_var = DoubleVar(value=1.0)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=BOTH, expand=True)

        ttk.Label(frame, text=f"Tıklama merkezi: {x}, {y}").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        fields = [
            ("Adım adı", self.name_var),
            ("Kayıt bölgesi genişliği", self.width_var),
            ("Kayıt bölgesi yüksekliği", self.height_var),
            ("Benzerlik (0–1)", self.similarity_var),
            ("En fazla bekle (sn)", self.timeout_var),
            ("Kontrol aralığı (sn)", self.poll_var),
            ("Tıkladıktan sonra bekle (sn)", self.wait_var),
        ]
        for row, (label, variable) in enumerate(fields, 1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(frame, textvariable=variable, width=22).grid(
                row=row, column=1, sticky="ew", padx=(8, 0), pady=2
            )

        ttk.Label(
            frame,
            text=(
                "Program yalnızca bu küçük sabit bölgeyi kontrol eder. "
                "Nox çözünürlüğü ve ekran yönü aynı kalmalıdır."
            ),
            wraplength=440,
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=9, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="İptal", command=self.destroy).pack(side=RIGHT)
        ttk.Button(
            buttons,
            text="Görsel Adımı Kaydet",
            command=lambda: self.accept(screen, x, y, package),
        ).pack(side=RIGHT, padx=(0, 8))

        self.wait_window(self)

    def accept(self, screen: Image.Image, x: int, y: int, package: str) -> None:
        try:
            width = int(self.width_var.get())
            height = int(self.height_var.get())
            similarity = float(self.similarity_var.get())
            timeout_s = float(self.timeout_var.get())
            poll_interval = float(self.poll_var.get())
            wait_after = float(self.wait_var.get())
            if width < 20 or height < 20:
                raise ValueError("Kayıt bölgesi en az 20×20 olmalı.")
            if not 0.5 <= similarity <= 0.999:
                raise ValueError("Benzerlik 0.5 ile 0.999 arasında olmalı.")
            if timeout_s <= 0 or poll_interval <= 0 or wait_after < 0:
                raise ValueError("Süreler geçersiz.")
        except Exception as exc:
            messagebox.showerror("Geçersiz değer", str(exc), parent=self)
            return

        sw, sh = screen.size
        region_x = max(0, min(sw - width, x - width // 2))
        region_y = max(0, min(sh - height, y - height // 2))
        width = min(width, sw - region_x)
        height = min(height, sh - region_y)
        crop = screen.crop((region_x, region_y, region_x + width, region_y + height))

        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

        self.result = FlowStep(
            action="wait_image_tap",
            name=self.name_var.get().strip() or "Görseli bekle ve tıkla",
            package=package,
            x=x,
            y=y,
            wait_after=wait_after,
            fallback_to_coordinate=True,
            timeout_s=timeout_s,
            poll_interval=poll_interval,
            template_png_base64=encoded,
            region_x=region_x,
            region_y=region_y,
            region_w=width,
            region_h=height,
            similarity=similarity,
        )
        self.destroy()


class SwipeDialog(Toplevel):
    def __init__(self, master: Tk, start: tuple[int, int], end: tuple[int, int], package: str):
        super().__init__(master)
        self.title("Kaydırma adımı")
        self.resizable(False, False)
        self.result: FlowStep | None = None
        self.transient(master)
        self.grab_set()

        self.name_var = StringVar(value="Kaydır")
        self.duration_var = IntVar(value=500)
        self.wait_var = DoubleVar(value=1.0)

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=BOTH, expand=True)
        ttk.Label(frame, text=f"Başlangıç: {start[0]}, {start[1]}").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text=f"Bitiş: {end[0]}, {end[1]}").grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="Adım adı").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.name_var, width=38).grid(row=2, column=1, padx=(8, 0), pady=(8, 0))
        ttk.Label(frame, text="Süre (ms)").grid(row=3, column=0, sticky="w")
        ttk.Spinbox(frame, from_=100, to=10000, increment=100, textvariable=self.duration_var, width=10).grid(
            row=3, column=1, sticky="w", padx=(8, 0)
        )
        ttk.Label(frame, text="Sonra bekle (sn)").grid(row=4, column=0, sticky="w")
        ttk.Spinbox(frame, from_=0, to=120, increment=0.25, textvariable=self.wait_var, width=10).grid(
            row=4, column=1, sticky="w", padx=(8, 0)
        )
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="İptal", command=self.destroy).pack(side=RIGHT)
        ttk.Button(buttons, text="Adımı Ekle", command=lambda: self.accept(start, end, package)).pack(
            side=RIGHT, padx=(0, 8)
        )
        self.wait_window(self)

    def accept(self, start: tuple[int, int], end: tuple[int, int], package: str) -> None:
        self.result = FlowStep(
            action="swipe",
            name=self.name_var.get().strip() or "Kaydır",
            package=package,
            x=start[0],
            y=start[1],
            x2=end[0],
            y2=end[1],
            duration_ms=max(100, int(self.duration_var.get())),
            wait_after=max(0.0, float(self.wait_var.get())),
        )
        self.destroy()


class NoxFlowApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title(f"NoxFlow Akış Editörü {__version__}")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        window_width = min(1500, max(1000, int(screen_width * 0.92)))
        window_height = min(940, max(680, int(screen_height * 0.88)))
        offset_x = max(0, (screen_width - window_width) // 2)
        offset_y = max(0, (screen_height - window_height) // 3)
        self.root.geometry(
            f"{window_width}x{window_height}+{offset_x}+{offset_y}"
        )
        self.root.minsize(960, 640)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        settings = load_json(SETTINGS_FILE, {})
        self.adb_path_var = StringVar(value=settings.get("adb_path", self.find_adb() or "adb"))
        self.device_var = StringVar(value=settings.get("device", ""))
        self.flow_name_var = StringVar(value="Yeni Akış")
        self.flow_id = uuid.uuid4().hex
        self.repeat_var = IntVar(value=1)
        self.status_var = StringVar(value="Hazır")
        self.current_package_var = StringVar(value="—")
        self.current_activity_var = StringVar(value="—")
        self.light_mode_var = BooleanVar(value=True)

        # Bir kez tanıtılan UI öğelerinin kalıcı komut kütüphanesi.
        self.ui_library: list[UiCommandDefinition] = self.load_ui_command_library()
        self.ui_library_search_var = StringVar()
        self.ui_library_package_var = StringVar(value="Tümü")
        self.ui_teach_interval_var = DoubleVar(
            value=float(settings.get("ui_teach_interval", 1.2))
        )
        self.ui_teach_status_var = StringVar(
            value=f"{len(self.ui_library)} hazır UI komutu"
        )
        self.auto_launch_package_var = BooleanVar(value=True)

        # Toplu öğretme ve arka plan oynatma.
        self.record_auto_wait_var = BooleanVar(
            value=settings.get("record_auto_wait", True)
        )
        self.record_visual_var = BooleanVar(
            value=settings.get("record_visual", True)
        )
        self.record_refresh_var = DoubleVar(
            value=float(settings.get("record_refresh", 1.2))
        )
        self.record_wait_threshold_var = DoubleVar(
            value=float(settings.get("record_wait_threshold", 0.35))
        )
        self.record_long_press_ms_var = IntVar(
            value=int(settings.get("record_long_press_ms", 650))
        )
        self.background_run_var = BooleanVar(
            value=settings.get("background_run", True)
        )
        self.minimize_nox_var = BooleanVar(
            value=settings.get("minimize_nox", True)
        )

        # Proxy ayarını klonun içinde kalıcı varsaymıyoruz. Her yeni Nox
        # ADB cihazı bağlandığında istenen durumu yeniden uygularız.
        self.proxy_enabled_var = BooleanVar(value=settings.get("proxy_enabled", True))
        self.proxy_mode_var = StringVar(value=settings.get("proxy_mode", "Charles otomatik"))
        self.proxy_host_var = StringVar(value=settings.get("proxy_host", detect_host_ip()))
        self.proxy_port_var = IntVar(value=int(settings.get("proxy_port", 8888)))
        self.auto_proxy_var = BooleanVar(value=settings.get("auto_proxy", True))
        self.auto_start_charles_var = BooleanVar(value=settings.get("auto_start_charles", True))
        self.proxy_status_var = StringVar(value="Charles ve Nox bekleniyor")
        self.charles_status_var = StringVar(value="Charles algılanmadı")

        # Nox açılışında otomatik Charles CA kurulumu.
        # Açılış sertifikası kullanıcı isteği gereği daima otomatik.
        # Eski settings.json içindeki false değeri özellikle yok sayılır.
        self.auto_certificate_var = BooleanVar(value=True)
        self.certificate_source_var = StringVar(
            value=str(FIXED_CERTIFICATE_SOURCE)
        )
        self.certificate_android_path_var = StringVar(
            value=settings.get(
                "certificate_android_path",
                DEFAULT_ANDROID_CERTIFICATE_PATH,
            )
        )
        self.certificate_importer_package_var = StringVar(
            value=settings.get(
                "certificate_importer_package",
                "net.jolivier.cert.Importer",
            )
        )
        self.certificate_status_var = StringVar(
            value="Sertifika açılış görevi bekliyor"
        )

        # Hedef-only geçit: yalnızca seçili host Charles'a gider.
        self.target_only_var = BooleanVar(value=settings.get("target_only", True))
        self.target_host_var = StringVar(
            value=settings.get("target_host", "outfox.api.zynga.com")
        )
        self.target_path_var = StringVar(
            value=settings.get(
                "target_path",
                "/outfox/v1/auth/authenticate*",
            )
        )
        self.gate_port_var = IntVar(value=int(settings.get("gate_port", 8899)))
        self.gate_status_var = StringVar(value="Seçici geçit bekliyor")

        self.steps: list[FlowStep] = []
        self.nodes: list[UiNode] = []
        self.selected_node: UiNode | None = None
        self.screen_image: Image.Image | None = None
        self.screen_photo: ImageTk.PhotoImage | None = None
        self.screen_size = (1, 1)
        self.display_scale = 1.0
        self.display_offset = (0, 0)
        self.pending_action: str | None = None
        self.swipe_start: tuple[int, int] | None = None

        self.recording_active = False
        self.recording_press: dict[str, Any] | None = None
        self.recording_last_release: float | None = None
        self.recording_initial_package_added = False
        self.recording_refresh_job: str | None = None
        self.capture_inflight = False

        self.ui_teach_active = False
        self.ui_teach_job: str | None = None
        self.ui_teach_scan_inflight = False

        self.runner_stop = threading.Event()
        self.runner_thread: threading.Thread | None = None
        self.step_state_lock = threading.RLock()
        self.step_run_state = self.load_step_run_state()

        self.certificate_state_lock = threading.RLock()
        self.certificate_state = self.load_certificate_state()
        self.certificate_install_lock = threading.RLock()
        self.certificate_watch_lock = threading.RLock()
        self.certificate_inflight_devices: set[str] = set()
        self.certificate_probe_inflight_devices: set[str] = set()
        self.certificate_retry_after: dict[str, float] = {}
        # Aynı serial yeniden boot ettiğinde session key değişir ve görev yeniden tetiklenir.
        self.certificate_known_session_by_device: dict[str, str] = {}
        self.certificate_hash_cache: tuple[str, int, int, str] | None = None

        # Altın şablondan geçici çalışma klonu üretme döngüsü.
        detected_console = find_noxconsole(self.adb_path_var.get().strip()) or ""
        self.noxconsole_path_var = StringVar(
            value=settings.get("noxconsole_path", detected_console)
        )
        self.clone_template_var = StringVar(value=settings.get("clone_template", ""))
        self.clone_prefix_var = StringVar(value=settings.get("clone_prefix", "NoxFlow_Work"))
        self.clone_runs_var = IntVar(value=int(settings.get("clone_runs", 10)))
        self.clone_count_var = IntVar(value=int(settings.get("clone_count", 0)))
        self.clone_copy_timeout_var = IntVar(value=int(settings.get("clone_copy_timeout", 900)))
        self.clone_boot_timeout_var = IntVar(value=int(settings.get("clone_boot_timeout", 240)))
        self.clone_cleanup_var = BooleanVar(value=settings.get("clone_cleanup", True))
        self.clone_force_basic_var = BooleanVar(value=settings.get("clone_force_basic", False))
        self.clone_resolution_var = StringVar(value=settings.get("clone_resolution", "1920,1080,240"))
        self.clone_cpu_var = IntVar(value=int(settings.get("clone_cpu", 4)))
        self.clone_memory_var = IntVar(value=int(settings.get("clone_memory", 4096)))
        self.clone_status_var = StringVar(value="Klon yöneticisi hazır")
        self.clone_instances: dict[str, NoxInstanceInfo] = {}
        self.clone_cycle_stop = threading.Event()
        self.clone_cycle_thread: threading.Thread | None = None
        self.managed_working_clone: str = ""
        self.managed_working_serial: str = ""

        self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.proxy_applied_devices: set[str] = set()
        self.proxy_inflight_devices: set[str] = set()
        self.last_charles_signature: tuple[Any, ...] | None = None
        self.last_charles_check = 0.0
        self.charles_start_attempted = False
        self.selective_gate = SelectiveProxyGate()

        self._build_ui()
        self.root.after(250, self.process_ui_queue)
        self.refresh_devices(silent=True)
        # Editör modu: uzun süreli proxy/sertifika/klon izleyicileri başlatılmaz.
        self.set_status("Akış editörü hazır — runtime servisleri kapalı")

    def noxconsole(self) -> NoxConsoleClient:
        path = self.noxconsole_path_var.get().strip()
        if not path:
            detected = find_noxconsole(self.adb_path_var.get().strip())
            if detected:
                path = detected
                self.noxconsole_path_var.set(path)
        if not path:
            raise NoxConsoleError("NoxConsole.exe bulunamadı.")
        return NoxConsoleClient(path)

    def browse_noxconsole(self) -> None:
        selected = filedialog.askopenfilename(
            title="NoxConsole.exe seç",
            filetypes=[("NoxConsole", "NoxConsole.exe"), ("EXE", "*.exe")],
        )
        if selected:
            self.noxconsole_path_var.set(selected)
            self.refresh_nox_instances()

    def refresh_nox_instances(self) -> None:
        try:
            items = self.noxconsole().list_instances()
            self.clone_instances = {item.display_name: item for item in items}
            values = list(self.clone_instances)
            self.clone_template_combo["values"] = values
            current = self.clone_template_var.get()
            if current not in values:
                # Eski ayarda yalnızca instance adı varsa eşleştir.
                matched = next(
                    (item.display_name for item in items if item.name == current or item.title == current),
                    values[0] if values else "",
                )
                self.clone_template_var.set(matched)
            self.clone_status_var.set(f"{len(items)} Nox kopyası bulundu")
        except Exception as exc:
            self.clone_status_var.set(f"Kopyalar okunamadı: {exc}")

    def selected_template_instance(self) -> NoxInstanceInfo:
        display = self.clone_template_var.get().strip()
        item = self.clone_instances.get(display)
        if item is not None:
            return item
        # Liste yenilenmemişse gerçek ad veya başlığa göre tekrar ara.
        items = self.noxconsole().list_instances()
        for candidate in items:
            if display in {candidate.name, candidate.title, candidate.display_name}:
                return candidate
        raise NoxConsoleError("Örnek / şablon Nox seçilmedi.")

    def launch_selected_template(self) -> None:
        try:
            item = self.selected_template_instance()
            self.noxconsole().launch(item.name)
            self.clone_status_var.set(f"Şablon açılıyor: {item.name}")
        except Exception as exc:
            messagebox.showerror("Nox açma", str(exc))

    def quit_selected_template(self) -> None:
        try:
            item = self.selected_template_instance()
            self.noxconsole().quit(item.name)
            self.clone_status_var.set(f"Şablon kapatılıyor: {item.name}")
        except Exception as exc:
            messagebox.showerror("Nox kapatma", str(exc))

    def certificate_fingerprint(self, source: Path) -> str:
        stat = source.stat()
        cache_key = (str(source), int(stat.st_mtime_ns), int(stat.st_size))
        cached = self.certificate_hash_cache
        if cached and cached[:3] == cache_key:
            return cached[3]
        value = certificate_sha256(source)
        self.certificate_hash_cache = (*cache_key, value)
        return value

    def wait_for_instance_presence(
        self,
        console: NoxConsoleClient,
        name: str,
        present: bool,
        timeout_s: float,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.clone_cycle_stop.is_set() or self.runner_stop.is_set():
                raise InterruptedError
            exists = console.find_instance(name) is not None
            if exists == present:
                return
            time.sleep(2.0)
        wanted = "oluşmadı" if present else "silinmedi"
        raise NoxConsoleError(f"Nox kopyası zamanında {wanted}: {name}")

    def wait_for_new_adb_device(
        self,
        before: set[str],
        timeout_s: float,
    ) -> tuple[str, AdbClient]:
        deadline = time.monotonic() + timeout_s
        base = AdbClient(self.adb_path_var.get().strip())
        last_devices: list[str] = []
        while time.monotonic() < deadline:
            if self.clone_cycle_stop.is_set() or self.runner_stop.is_set():
                raise InterruptedError
            try:
                last_devices = base.devices()
            except Exception:
                time.sleep(2.0)
                continue
            candidates = [serial for serial in last_devices if serial not in before]
            for serial in candidates:
                client = AdbClient(self.adb_path_var.get().strip(), serial)
                try:
                    if client.boot_completed():
                        return serial, client
                except Exception:
                    pass
            time.sleep(2.0)
        raise NoxConsoleError(
            "Yeni çalışma klonunun ADB cihazı/Android açılışı zamanında hazır olmadı. "
            f"Görülen cihazlar: {last_devices}"
        )

    def wait_for_adb_disappear(self, serial: str, timeout_s: float = 120.0) -> None:
        deadline = time.monotonic() + timeout_s
        base = AdbClient(self.adb_path_var.get().strip())
        while time.monotonic() < deadline:
            try:
                if serial not in base.devices():
                    return
            except Exception:
                return
            time.sleep(2.0)
        raise NoxConsoleError(f"Nox ADB bağlantısı kapanmadı: {serial}")

    def wait_for_certificate_worker(self, serial: str, timeout_s: float = 45.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            with self.certificate_watch_lock:
                active = serial in self.certificate_inflight_devices
            if not active:
                return
            if self.runner_stop.is_set() or self.clone_cycle_stop.is_set():
                raise InterruptedError
            time.sleep(0.25)
        self.queue_log(
            f"{serial}: arka plan sertifika görevi {timeout_s:g} saniyede bitmedi; "
            "akışın kilitlenmemesi için devam ediliyor."
        )

    def execute_flow_steps_for_client(
        self,
        client: AdbClient,
        *,
        start_index: int,
        repeat: int,
        apply_proxy: dict[str, Any] | None,
        label: str,
    ) -> str:
        iteration = 0
        run_completed: set[str] = set()
        nox_session_key = ""
        if any(
            step.run_condition == "once_per_nox_session"
            for step in self.steps[start_index:]
        ):
            nox_session_key = self.nox_session_key(client)
            self.queue_log(f"{label} — Nox açılış oturumu: {nox_session_key}")
        if apply_proxy and apply_proxy.get("enabled", False):
            applied = self.apply_proxy_profile(client, apply_proxy)
            self.queue_log(f"{label} — proxy hazır: {applied}")
        while not self.runner_stop.is_set() and (repeat == 0 or iteration < repeat):
            iteration += 1
            self.queue_log(f"{label} — döngü {iteration} başladı")
            for absolute_index in range(start_index, len(self.steps)):
                if self.runner_stop.is_set() or self.clone_cycle_stop.is_set():
                    raise InterruptedError
                step = self.steps[absolute_index]
                display_index = absolute_index + 1
                if not step.enabled:
                    continue
                state_key = self.persistent_step_key(step)
                if step.run_condition == "once_per_flow_run" and state_key in run_completed:
                    continue
                if (
                    step.run_condition == "once_per_nox_session"
                    and self.step_completed_for_nox_session(nox_session_key, step)
                ):
                    self.queue_log(
                        f"{label} — {display_index}/{len(self.steps)} bu Nox açılışında "
                        f"zaten çalıştı; atlandı: {step.name}"
                    )
                    continue
                self.queue_status(
                    f"{label} — {display_index}/{len(self.steps)}: {step.name}"
                )
                self.execute_step(step, client, allow_wait=True)
                if step.run_condition == "once_per_flow_run":
                    run_completed.add(state_key)
                elif step.run_condition == "once_per_nox_session":
                    self.mark_step_completed_for_nox_session(nox_session_key, step)
            self.queue_log(f"{label} — döngü {iteration} tamamlandı")
        return "durduruldu" if self.runner_stop.is_set() else "tamamlandı"

    def start_clone_cycle(self) -> None:
        if self.clone_cycle_thread and self.clone_cycle_thread.is_alive():
            return
        if not self.steps:
            messagebox.showwarning("Akış boş", "Klon döngüsünden önce bir akış aç veya kaydet.")
            return
        try:
            template = self.selected_template_instance()
            prefix = safe_nox_instance_name(self.clone_prefix_var.get())
            runs = max(1, int(self.clone_runs_var.get()))
            total = max(0, int(self.clone_count_var.get()))
            copy_timeout = max(60, int(self.clone_copy_timeout_var.get()))
            boot_timeout = max(60, int(self.clone_boot_timeout_var.get()))
            proxy_profile = self.proxy_profile()
        except Exception as exc:
            messagebox.showerror("Klon döngüsü", str(exc))
            return
        self.runner_stop.clear()
        self.clone_cycle_stop.clear()
        self.clone_start_btn.configure(state="disabled")
        self.clone_stop_btn.configure(state="normal")
        self.run_btn.configure(state="disabled")
        self.start_selected_btn.configure(state="disabled")
        config = {
            "template": template,
            "prefix": prefix,
            "runs": runs,
            "total": total,
            "copy_timeout": copy_timeout,
            "boot_timeout": boot_timeout,
            "proxy_profile": proxy_profile,
            "cleanup": bool(self.clone_cleanup_var.get()),
            "force_basic": bool(self.clone_force_basic_var.get()),
            "resolution": self.clone_resolution_var.get().strip(),
            "cpu": int(self.clone_cpu_var.get()),
            "memory": int(self.clone_memory_var.get()),
        }
        self.clone_cycle_thread = threading.Thread(
            target=self._clone_cycle_worker,
            args=(config,),
            daemon=True,
        )
        self.clone_cycle_thread.start()

    def stop_clone_cycle(self) -> None:
        self.clone_cycle_stop.set()
        self.runner_stop.set()
        self.clone_status_var.set("Klon döngüsü durduruluyor…")

    def _clone_cycle_worker(self, config: dict[str, Any]) -> None:
        console = self.noxconsole()
        created = 0
        message = "Klon döngüsü tamamlandı."
        try:
            while not self.clone_cycle_stop.is_set() and (
                config["total"] == 0 or created < config["total"]
            ):
                created += 1
                stamp = time.strftime("%Y%m%d_%H%M%S")
                working_name = safe_nox_instance_name(
                    f"{config['prefix']}_{stamp}_{created:03d}"
                )
                if working_name in {config["template"].name, config["template"].title}:
                    raise NoxConsoleError("Geçici kopya adı şablonla aynı olamaz.")
                self.managed_working_clone = working_name
                self.managed_working_serial = ""
                self.ui_queue.put(("clone_status", f"{working_name}: şablon kopyalanıyor…"))
                # Altın şablon veri bütünlüğü için kaynak kopyayı kapalı tut.
                console.quit(config["template"].name)
                time.sleep(3.0)
                before_devices = set(AdbClient(self.adb_path_var.get().strip()).devices())
                console.copy_instance(
                    config["template"],
                    working_name,
                    timeout_s=float(config["copy_timeout"]),
                )
                self.wait_for_instance_presence(
                    console, working_name, True, float(config["copy_timeout"])
                )
                if config["force_basic"]:
                    console.modify_basic(
                        working_name,
                        config["resolution"],
                        config["cpu"],
                        config["memory"],
                    )
                self.ui_queue.put(("clone_status", f"{working_name}: açılıyor…"))
                console.launch(working_name)
                serial, client = self.wait_for_new_adb_device(
                    before_devices, float(config["boot_timeout"])
                )
                self.managed_working_serial = serial
                self.ui_queue.put(("select_device", serial))
                self.ui_queue.put(("clone_status", f"{working_name}: Android hazır ({serial})"))

                if config["proxy_profile"].get("enabled", False):
                    applied = self.apply_proxy_profile(client, config["proxy_profile"])
                    self.queue_log(f"{working_name}: proxy hazır — {applied}")

                # Klon döngüsünde sertifika hazırlığı bir kez ve kontrollü yapılır.
                certificate_result = self.install_startup_certificate(client, force=False)
                self.queue_log(f"{working_name}: sertifika — {certificate_result}")
                client.keyevent("HOME")
                time.sleep(0.5)

                for usage in range(1, config["runs"] + 1):
                    if self.clone_cycle_stop.is_set():
                        raise InterruptedError
                    self.ui_queue.put(
                        (
                            "clone_status",
                            f"{working_name}: akış {usage}/{config['runs']} çalışıyor",
                        )
                    )
                    self.execute_flow_steps_for_client(
                        client,
                        start_index=0,
                        repeat=1,
                        apply_proxy=None,
                        label=f"{working_name} kullanım {usage}/{config['runs']}",
                    )

                if config["cleanup"]:
                    self.ui_queue.put(("clone_status", f"{working_name}: kapatılıyor…"))
                    console.quit(working_name)
                    self.wait_for_adb_disappear(serial, 150.0)
                    self.ui_queue.put(("clone_status", f"{working_name}: siliniyor…"))
                    console.remove(working_name)
                    self.wait_for_instance_presence(console, working_name, False, 300.0)
                    self.queue_log(f"Geçici çalışma klonu silindi: {working_name}")
                self.managed_working_clone = ""
                self.managed_working_serial = ""
            if self.clone_cycle_stop.is_set():
                message = "Klon döngüsü kullanıcı tarafından durduruldu."
        except InterruptedError:
            message = "Klon döngüsü durduruldu."
        except Exception as exc:
            message = f"Klon döngüsü hata ile durdu: {exc}"
            self.queue_log(message)
        finally:
            if config.get("cleanup") and self.managed_working_clone:
                try:
                    console.quit(self.managed_working_clone)
                    if self.managed_working_serial:
                        self.wait_for_adb_disappear(self.managed_working_serial, 60.0)
                    console.remove(self.managed_working_clone)
                except Exception as cleanup_exc:
                    self.queue_log(f"Geçici klon temizleme uyarısı: {cleanup_exc}")
            self.managed_working_clone = ""
            self.managed_working_serial = ""
            self.ui_queue.put(("clone_done", message))

    def load_certificate_state(self) -> dict[str, Any]:
        raw = load_json(CERTIFICATE_STATE_FILE, {"completed": {}})
        if not isinstance(raw, dict):
            return {"completed": {}}
        if not isinstance(raw.get("completed"), dict):
            raw["completed"] = {}
        return raw

    def save_certificate_state(self) -> None:
        with self.certificate_state_lock:
            completed = self.certificate_state.setdefault("completed", {})
            if len(completed) > 96:
                ordered = sorted(
                    completed.items(),
                    key=lambda pair: float(
                        pair[1].get("updated_at", 0)
                        if isinstance(pair[1], dict)
                        else 0
                    ),
                    reverse=True,
                )
                self.certificate_state["completed"] = dict(ordered[:96])
            atomic_write_json(
                CERTIFICATE_STATE_FILE,
                self.certificate_state,
            )

    def certificate_source_path(self) -> Path:
        # Sertifika kaynağı kullanıcı tarafından sabitlendi. Ayar dosyasındaki
        # eski değerler veya uygulamanın taşınması bu yolu değiştirmez.
        self.certificate_source_var.set(str(FIXED_CERTIFICATE_SOURCE))
        return FIXED_CERTIFICATE_SOURCE

    def certificate_session_key(
        self,
        client: AdbClient,
        source: Path,
    ) -> str:
        serial = client.serial or self.device_var.get().strip() or "default"
        boot = client.boot_session_id()
        fingerprint = self.certificate_fingerprint(source)
        return f"{serial}|{boot}|sha256:{fingerprint}"

    def certificate_already_installed_for_session(
        self,
        session_key: str,
    ) -> bool:
        with self.certificate_state_lock:
            return session_key in self.certificate_state.get(
                "completed",
                {},
            )

    def mark_certificate_installed_for_session(
        self,
        session_key: str,
        source: Path,
        android_path: str,
    ) -> None:
        with self.certificate_state_lock:
            completed = self.certificate_state.setdefault("completed", {})
            completed[session_key] = {
                "source": str(source),
                "android_path": android_path,
                "updated_at": time.time(),
            }
            self.save_certificate_state()

    def choose_startup_certificate(self) -> None:
        # Geriye dönük düğme bağlantısı. Artık dosya seçilmez; sabit kaynak kontrol edilir.
        source = self.certificate_source_path()
        if source.is_file():
            fingerprint = self.certificate_fingerprint(source)
            self.certificate_status_var.set(
                f"Sabit sertifika bulundu: {source.name}"
            )
            self.log(
                f"Sabit sertifika doğrulandı: {source} | sha256={fingerprint}"
            )
            messagebox.showinfo(
                "Sabit sertifika hazır",
                "Program şu dosyayı otomatik kullanacak:\n\n"
                f"{source}\n\n"
                "Nox hedefi:\n"
                f"{self.certificate_android_path_var.get()}",
            )
        else:
            self.certificate_status_var.set(
                "Sabit sertifika dosyası bulunamadı"
            )
            messagebox.showerror(
                "Sertifika bulunamadı",
                "Beklenen sabit sertifika dosyası bulunamadı:\n\n"
                f"{source}\n\n"
                "Dosyayı bu konuma yerleştirdikten sonra kurulum otomatik başlayacaktır.",
            )

    def open_certificate_folder(self) -> None:
        target_dir = FIXED_CERTIFICATE_SOURCE.parent
        try:
            if os.name == "nt":
                os.startfile(str(target_dir))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target_dir)])
            else:
                subprocess.Popen(["xdg-open", str(target_dir)])
        except Exception as exc:
            messagebox.showerror(
                "Klasör açılamadı",
                str(exc),
            )

    def on_certificate_setting_changed(self) -> None:
        # Geriye dönük çağrılar için. Bu özellik artık kapatılamaz.
        self.auto_certificate_var.set(True)
        self.certificate_retry_after.clear()
        self.certificate_status_var.set(
            "Otomatik sertifika izleyicisi açık"
        )
        try:
            devices = AdbClient(
                self.adb_path_var.get().strip()
            ).devices()
            self.schedule_auto_certificate(devices)
        except Exception:
            pass

    def install_certificate_now(self) -> None:
        if not self.validate_device():
            return
        serial = self.device_var.get().strip()
        if serial in self.certificate_inflight_devices:
            self.certificate_status_var.set(
                "Bu Nox için sertifika kurulumu zaten çalışıyor"
            )
            return
        self.certificate_inflight_devices.add(serial)
        self.certificate_status_var.set(
            f"{serial}: sertifika kuruluyor…"
        )
        threading.Thread(
            target=self._certificate_worker,
            args=(serial, True, "elle"),
            daemon=True,
        ).start()

    def reset_current_certificate_state(self) -> None:
        if not self.validate_device():
            return
        serial = self.device_var.get().strip()
        if not messagebox.askyesno(
            "Sertifika durumunu sıfırla",
            "Seçili Nox için kayıtlı açılış sertifikası durumu silinsin mi?",
        ):
            return

        def worker() -> None:
            try:
                client = AdbClient(
                    self.adb_path_var.get().strip(),
                    serial,
                )
                boot = client.boot_session_id()
                prefix = f"{serial}|{boot}|"
                with self.certificate_state_lock:
                    completed = self.certificate_state.setdefault(
                        "completed",
                        {},
                    )
                    keys = [
                        key for key in completed
                        if key.startswith(prefix)
                    ]
                    for key in keys:
                        completed.pop(key, None)
                    self.save_certificate_state()
                with self.certificate_watch_lock:
                    self.certificate_known_session_by_device.pop(
                        serial,
                        None,
                    )
                    self.certificate_retry_after.pop(serial, None)
                self.ui_queue.put(
                    (
                        "certificate_status",
                        (
                            True,
                            serial,
                            f"{len(keys)} sertifika oturum kaydı silindi.",
                        ),
                    )
                )
            except Exception as exc:
                self.ui_queue.put(
                    (
                        "certificate_status",
                        (False, serial, str(exc)),
                    )
                )

        threading.Thread(target=worker, daemon=True).start()

    def schedule_auto_certificate(self, devices: list[str]) -> None:
        """
        Her bağlı Nox için boot/session kimliğini kontrol eder. Aynı serial açık
        kalırken tekrar kurmaz; serial yeniden boot ettiğinde yeni session key ile
        otomatik kurulumu tekrar başlatır.
        """
        source = self.certificate_source_path()
        if not source.is_file():
            self.certificate_status_var.set(
                f"Sabit sertifika bekleniyor: {source}"
            )
            return

        now = time.monotonic()
        for serial in devices:
            if not probable_nox_serial(serial):
                continue
            with self.certificate_watch_lock:
                if serial in self.certificate_probe_inflight_devices:
                    continue
                if serial in self.certificate_inflight_devices:
                    continue
                if now < self.certificate_retry_after.get(serial, 0.0):
                    continue
                self.certificate_probe_inflight_devices.add(serial)

            threading.Thread(
                target=self._certificate_auto_probe_worker,
                args=(serial,),
                daemon=True,
            ).start()

    def _certificate_auto_probe_worker(self, serial: str) -> None:
        """
        Proxy izleyicisinden bağımsız otomatik sertifika kontrolü.
        Android henüz açılmadıysa bir sonraki 2 saniyelik turda tekrar denenir.
        """
        try:
            source = self.certificate_source_path()
            if not source.is_file():
                return

            client = AdbClient(
                self.adb_path_var.get().strip(),
                serial,
            )
            if not client.boot_completed():
                self.ui_queue.put(
                    (
                        "certificate_waiting",
                        (
                            serial,
                            "Android açılışı bekleniyor",
                        ),
                    )
                )
                return

            session_key = self.certificate_session_key(
                client,
                source,
            )
            with self.certificate_watch_lock:
                known = self.certificate_known_session_by_device.get(
                    serial
                )
            if known == session_key:
                return

            if self.certificate_already_installed_for_session(
                session_key
            ):
                with self.certificate_watch_lock:
                    self.certificate_known_session_by_device[
                        serial
                    ] = session_key
                self.ui_queue.put(
                    (
                        "certificate_waiting",
                        (
                            serial,
                            "Bu Nox açılışında sertifika zaten hazır",
                        ),
                    )
                )
                return

            with self.certificate_watch_lock:
                if serial in self.certificate_inflight_devices:
                    return
                self.certificate_inflight_devices.add(serial)

            result = self.install_startup_certificate(
                client,
                force=False,
            )
            with self.certificate_watch_lock:
                self.certificate_known_session_by_device[
                    serial
                ] = session_key
                self.certificate_retry_after.pop(serial, None)
            self.ui_queue.put(
                (
                    "certificate_done",
                    (
                        True,
                        serial,
                        "tam otomatik",
                        result,
                    ),
                )
            )
        except Exception as exc:
            with self.certificate_watch_lock:
                self.certificate_retry_after[serial] = (
                    time.monotonic() + 20.0
                )
            self.ui_queue.put(
                (
                    "certificate_done",
                    (
                        False,
                        serial,
                        "tam otomatik",
                        str(exc),
                    ),
                )
            )
        finally:
            with self.certificate_watch_lock:
                self.certificate_probe_inflight_devices.discard(
                    serial
                )

    def certificate_watch_tick(self) -> None:
        """
        Charles/proxy ayarlarından bağımsız, sürekli açılış izleyicisi.
        NoxFlow açık olduğu sürece iki saniyede bir yeni/reboot edilmiş Nox aranır.
        """
        try:
            devices = AdbClient(
                self.adb_path_var.get().strip()
            ).devices()
            current = set(devices)
            with self.certificate_watch_lock:
                self.certificate_inflight_devices.intersection_update(
                    current
                )
                self.certificate_probe_inflight_devices.intersection_update(
                    current
                )
                for serial in list(
                    self.certificate_known_session_by_device
                ):
                    if serial not in current:
                        self.certificate_known_session_by_device.pop(
                            serial,
                            None,
                        )
            self.schedule_auto_certificate(devices)
        except Exception as exc:
            self.certificate_status_var.set(
                f"Nox açılış izleyicisi bekliyor: {exc}"
            )
        finally:
            self.root.after(
                10000,
                self.certificate_watch_tick,
            )

    def _certificate_worker(
        self,
        serial: str,
        force: bool,
        reason: str,
    ) -> None:
        try:
            client = AdbClient(
                self.adb_path_var.get().strip(),
                serial,
            )
            result = self.install_startup_certificate(
                client,
                force=force,
            )
            try:
                source = self.certificate_source_path()
                session_key = self.certificate_session_key(
                    client,
                    source,
                )
                with self.certificate_watch_lock:
                    self.certificate_known_session_by_device[
                        serial
                    ] = session_key
            except Exception:
                pass
            self.ui_queue.put(
                (
                    "certificate_done",
                    (True, serial, reason, result),
                )
            )
        except Exception as exc:
            self.certificate_retry_after[serial] = (
                time.monotonic() + 30.0
            )
            self.ui_queue.put(
                (
                    "certificate_done",
                    (False, serial, reason, str(exc)),
                )
            )

    @staticmethod
    def _node_matches_certificate_selector(
        node: UiNode,
        resource_ids: tuple[str, ...],
        texts: tuple[str, ...],
        descriptions: tuple[str, ...],
    ) -> bool:
        if not node.enabled:
            return False
        resource_set = {value for value in resource_ids if value}
        text_set = {value.casefold() for value in texts if value}
        description_set = {
            value.casefold() for value in descriptions if value
        }

        if node.resource_id and node.resource_id in resource_set:
            return True
        if node.text and node.text.casefold() in text_set:
            return True
        if (
            node.content_desc
            and node.content_desc.casefold() in description_set
        ):
            return True
        return False

    def certificate_wait_and_tap(
        self,
        client: AdbClient,
        *,
        resource_ids: tuple[str, ...] = (),
        texts: tuple[str, ...] = (),
        descriptions: tuple[str, ...] = (),
        timeout_s: float = 15.0,
        optional: bool = False,
        label: str,
    ) -> bool:
        deadline = time.monotonic() + max(0.5, timeout_s)
        last_node_count = 0

        while time.monotonic() < deadline:
            with tempfile.TemporaryDirectory() as tmpdir:
                xml_path = Path(tmpdir) / "certificate_window.xml"
                try:
                    client.dump_ui(xml_path)
                    nodes = parse_ui_xml(xml_path)
                except Exception:
                    nodes = []

            last_node_count = len(nodes)
            candidates = [
                node for node in nodes
                if self._node_matches_certificate_selector(
                    node,
                    resource_ids,
                    texts,
                    descriptions,
                )
            ]
            if candidates:
                candidates.sort(
                    key=lambda node: (
                        not node.clickable,
                        node.area,
                    )
                )
                x, y = candidates[0].target_center
                client.tap(x, y)
                self.queue_log(
                    f"Sertifika kurulumu — tıklandı: {label} "
                    f"({x},{y})"
                )
                time.sleep(0.8)
                return True
            time.sleep(0.65)

        if optional:
            self.queue_log(
                f"Sertifika kurulumu — isteğe bağlı öğe görülmedi: {label}"
            )
            return False
        raise AdbError(
            f"Sertifika kurulumu için öğe bulunamadı: {label} "
            f"(son UI öğesi sayısı: {last_node_count})"
        )

    def install_startup_certificate(
        self,
        client: AdbClient,
        *,
        force: bool = False,
    ) -> str:
        with self.certificate_install_lock:
            source = self.certificate_source_path()
            if not source.is_file():
                raise AdbError(
                    f"Sabit sertifika dosyası bulunamadı: {source}"
                )

            android_path = validate_android_file_path(
                self.certificate_android_path_var.get()
            )
            importer_package = AdbClient.validate_package(
                self.certificate_importer_package_var.get().strip()
            )

            self.queue_log(
                f"{client.serial}: Android açılışı bekleniyor…"
            )
            client.wait_for_boot(timeout_s=180)
            session_key = self.certificate_session_key(
                client,
                source,
            )

            if (
                not force
                and self.certificate_already_installed_for_session(
                    session_key
                )
            ):
                return "Bu Nox açılışında sertifika daha önce kuruldu; atlandı."

            if not client.package_installed(importer_package):
                raise AdbError(
                    f"Root Certificate Manager kurulu değil: "
                    f"{importer_package}"
                )

            self.queue_log(
                f"{client.serial}: sertifika Android'e gönderiliyor: "
                f"{android_path}"
            )
            client.push_file(source, android_path)

            client.force_stop(importer_package)
            client.launch_package(importer_package)
            time.sleep(1.2)

            # Android 6+ depolama izin penceresi yalnızca ilk çalıştırmada çıkabilir.
            self.certificate_wait_and_tap(
                client,
                resource_ids=(
                    "com.android.packageinstaller:id/permission_allow_button",
                    "com.android.permissioncontroller:id/permission_allow_button",
                ),
                texts=("ALLOW", "Allow", "İZİN VER", "İzin ver"),
                timeout_s=3.0,
                optional=True,
                label="Depolama izni",
            )

            self.certificate_wait_and_tap(
                client,
                resource_ids=(
                    "net.jolivier.cert.Importer:id/action_install_from_sd",
                ),
                texts=(
                    "Import from SD Card",
                    "SD Card'dan içe aktar",
                    "SD karttan içe aktar",
                ),
                timeout_s=20.0,
                label="Import from SD Card",
            )

            self.certificate_wait_and_tap(
                client,
                resource_ids=(
                    "com.android.packageinstaller:id/permission_allow_button",
                    "com.android.permissioncontroller:id/permission_allow_button",
                ),
                texts=("ALLOW", "Allow", "İZİN VER", "İzin ver"),
                timeout_s=3.0,
                optional=True,
                label="Dosya erişim izni",
            )

            file_name = Path(android_path).name
            file_found = self.certificate_wait_and_tap(
                client,
                texts=(file_name,),
                timeout_s=4.0,
                optional=True,
                label=f"Sertifika dosyası: {file_name}",
            )

            if not file_found:
                self.certificate_wait_and_tap(
                    client,
                    resource_ids=(
                        "com.android.documentsui:id/toolbar",
                        "com.android.documentsui:id/roots_toolbar",
                    ),
                    texts=("Show roots", "Kökleri göster"),
                    descriptions=(
                        "Show roots",
                        "Kökleri göster",
                        "Show navigation roots",
                    ),
                    timeout_s=5.0,
                    optional=True,
                    label="Dosya konumları menüsü",
                )
                self.certificate_wait_and_tap(
                    client,
                    texts=(
                        "Downloads",
                        "Download",
                        "İndirilenler",
                    ),
                    timeout_s=8.0,
                    optional=True,
                    label="Downloads klasörü",
                )
                self.certificate_wait_and_tap(
                    client,
                    texts=(file_name,),
                    timeout_s=20.0,
                    label=f"Sertifika dosyası: {file_name}",
                )

            self.certificate_wait_and_tap(
                client,
                resource_ids=("android:id/button1",),
                texts=(
                    "Import",
                    "IMPORT",
                    "İçe aktar",
                    "İÇE AKTAR",
                ),
                timeout_s=15.0,
                label="Import",
            )

            self.certificate_wait_and_tap(
                client,
                resource_ids=("android:id/button1",),
                texts=("OK", "Tamam", "TAMAM"),
                timeout_s=8.0,
                optional=True,
                label="OK",
            )

            self.mark_certificate_installed_for_session(
                session_key,
                source,
                android_path,
            )
            # Sertifika yöneticisini ön planda bırakıp normal akışı engelleme.
            client.keyevent("HOME")
            return (
                f"Sertifika kurulum adımları tamamlandı: "
                f"{file_name}"
            )

    def load_step_run_state(self) -> dict[str, Any]:
        raw = load_json(STEP_STATE_FILE, {"sessions": {}})
        if not isinstance(raw, dict):
            return {"sessions": {}}
        sessions = raw.get("sessions")
        if not isinstance(sessions, dict):
            raw["sessions"] = {}
        return raw

    def save_step_run_state(self) -> None:
        with self.step_state_lock:
            sessions = self.step_run_state.setdefault("sessions", {})
            # Eski kapanmış Nox oturumlarının dosyayı büyütmesini engelle.
            if len(sessions) > 64:
                ordered = sorted(
                    sessions.items(),
                    key=lambda pair: float(
                        pair[1].get("updated_at", 0)
                        if isinstance(pair[1], dict)
                        else 0
                    ),
                    reverse=True,
                )
                self.step_run_state["sessions"] = dict(ordered[:64])
            atomic_write_json(STEP_STATE_FILE, self.step_run_state)

    def nox_session_key(self, client: AdbClient) -> str:
        serial = client.serial or self.device_var.get().strip() or "default"
        return f"{serial}|{client.boot_session_id()}"

    def persistent_step_key(self, step: FlowStep) -> str:
        return f"{self.flow_id}:{step.step_id}"

    def step_completed_for_nox_session(
        self,
        session_key: str,
        step: FlowStep,
    ) -> bool:
        with self.step_state_lock:
            session = self.step_run_state.get("sessions", {}).get(
                session_key,
                {},
            )
            completed = session.get("steps", []) if isinstance(session, dict) else []
            return self.persistent_step_key(step) in completed

    def mark_step_completed_for_nox_session(
        self,
        session_key: str,
        step: FlowStep,
    ) -> None:
        with self.step_state_lock:
            sessions = self.step_run_state.setdefault("sessions", {})
            session = sessions.setdefault(
                session_key,
                {"updated_at": time.time(), "steps": []},
            )
            completed = set(session.get("steps", []))
            completed.add(self.persistent_step_key(step))
            session["steps"] = sorted(completed)
            session["updated_at"] = time.time()
            self.save_step_run_state()

    def reset_current_nox_step_conditions(self) -> None:
        if not self.validate_device():
            return
        if not messagebox.askyesno(
            "Bir-kez durumunu sıfırla",
            "Seçili Nox'un bu açılışında tamamlandı olarak işaretlenen "
            "bütün koşullu adımlar yeniden çalıştırılabilir hale gelsin mi?",
        ):
            return

        def worker() -> None:
            try:
                client = self.adb()
                session_key = self.nox_session_key(client)
                with self.step_state_lock:
                    sessions = self.step_run_state.setdefault("sessions", {})
                    removed = sessions.pop(session_key, None) is not None
                    self.save_step_run_state()
                self.queue_log(
                    "Seçili Nox için bir-kez koşul durumu sıfırlandı."
                    if removed
                    else "Seçili Nox için kayıtlı bir-kez koşul durumu yoktu."
                )
                self.queue_status("Bir-kez koşulları sıfırlandı.")
            except Exception as exc:
                self.queue_log(f"Koşul durumu sıfırlanamadı: {exc}")
                self.queue_status("Koşul durumu sıfırlanamadı.")

        threading.Thread(target=worker, daemon=True).start()

    def load_ui_command_library(self) -> list[UiCommandDefinition]:
        raw_items = load_json(UI_COMMANDS_FILE, [])
        loaded: list[UiCommandDefinition] = []
        if not isinstance(raw_items, list):
            return loaded
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            try:
                loaded.append(UiCommandDefinition(**raw))
            except Exception:
                continue
        return loaded

    def save_ui_command_library(self) -> None:
        atomic_write_json(
            UI_COMMANDS_FILE,
            [asdict(item) for item in self.ui_library],
        )

    def refresh_ui_library_tree(self) -> None:
        tree = getattr(self, "ui_library_tree", None)
        if tree is None:
            return

        packages = sorted(
            {item.package for item in self.ui_library if item.package}
        )
        current_values = ["Tümü", *packages]
        self.ui_library_package_combo["values"] = current_values
        if self.ui_library_package_var.get() not in current_values:
            self.ui_library_package_var.set("Tümü")

        selected_package = self.ui_library_package_var.get()
        needle = self.ui_library_search_var.get().strip().casefold()

        for row in tree.get_children():
            tree.delete(row)

        for item in sorted(
            self.ui_library,
            key=lambda value: (
                value.package.casefold(),
                value.component.casefold(),
                value.name.casefold(),
            ),
        ):
            if selected_package != "Tümü" and item.package != selected_package:
                continue
            haystack = " ".join(
                [
                    item.name,
                    item.package,
                    item.component,
                    item.resource_id,
                    item.text,
                    item.content_desc,
                    item.class_name,
                ]
            ).casefold()
            if needle and needle not in haystack:
                continue
            tree.insert(
                "",
                END,
                iid=item.key,
                values=(
                    item.name,
                    item.package,
                    item.component,
                    item.resource_id,
                    item.text or item.content_desc,
                    item.seen_count,
                ),
            )

        self.ui_teach_status_var.set(
            f"{len(self.ui_library)} hazır UI komutu"
            + (" — tanıtma açık" if self.ui_teach_active else "")
        )

    def select_active_package_in_library(self) -> None:
        package = self.current_package_var.get().strip()
        if package and package != "—":
            self.ui_library_package_var.set(package)
            self.refresh_ui_library_tree()
        self.main_notebook.select(self.command_tab)

    def merge_ui_commands(
        self,
        commands: list[UiCommandDefinition],
    ) -> tuple[int, int]:
        by_key = {item.key: item for item in self.ui_library}
        added = 0
        updated = 0

        for command in commands:
            existing = by_key.get(command.key)
            if existing is None:
                self.ui_library.append(command)
                by_key[command.key] = command
                added += 1
                continue

            # Son görülen activity ve koordinat, uygulama güncellemelerinde daha yararlıdır.
            existing.name = command.name or existing.name
            existing.component = command.component or existing.component
            existing.x = command.x
            existing.y = command.y
            existing.learned_at = command.learned_at
            existing.seen_count = max(1, int(existing.seen_count)) + 1
            updated += 1

        if added or updated:
            self.save_ui_command_library()
            self.refresh_ui_library_tree()
        return added, updated

    def start_ui_teaching_session(self) -> None:
        if not self.validate_device():
            return
        if self.ui_teach_active:
            return
        self.ui_teach_active = True
        self.ui_teach_start_btn.configure(state="disabled")
        self.ui_teach_stop_btn.configure(state="normal")
        self.ui_teach_status_var.set("Tanıtma açık — uygulama ekranlarını bir kez gezin")
        self.log(
            "Otomatik UI tanıtma başladı. Açılan her ekrandaki kullanılabilir "
            "öğeler toplu olarak komut kütüphanesine alınacak."
        )
        self.teach_current_ui_screen()
        self.schedule_ui_teach_scan()

    def stop_ui_teaching_session(self) -> None:
        if not self.ui_teach_active:
            return
        self.ui_teach_active = False
        if self.ui_teach_job is not None:
            try:
                self.root.after_cancel(self.ui_teach_job)
            except Exception:
                pass
            self.ui_teach_job = None
        self.ui_teach_start_btn.configure(state="normal")
        self.ui_teach_stop_btn.configure(state="disabled")
        self.refresh_ui_library_tree()
        self.log(
            f"Otomatik UI tanıtma tamamlandı. "
            f"Kütüphanede {len(self.ui_library)} hazır komut var."
        )

    def schedule_ui_teach_scan(self) -> None:
        if not self.ui_teach_active:
            return
        try:
            interval = max(0.6, float(self.ui_teach_interval_var.get()))
        except Exception:
            interval = 1.2
        self.ui_teach_job = self.root.after(
            int(interval * 1000),
            self.ui_teach_tick,
        )

    def ui_teach_tick(self) -> None:
        self.ui_teach_job = None
        if not self.ui_teach_active:
            return
        self.teach_current_ui_screen(silent=True)
        self.schedule_ui_teach_scan()

    def teach_current_ui_screen(self, silent: bool = False) -> None:
        if not self.validate_device():
            return
        if self.ui_teach_scan_inflight:
            return
        self.ui_teach_scan_inflight = True
        if not silent:
            self.ui_teach_status_var.set("Açık ekran toplu tanıtılıyor…")
        threading.Thread(
            target=self._teach_current_ui_worker,
            daemon=True,
        ).start()

    def _teach_current_ui_worker(self) -> None:
        try:
            client = self.adb()
            with tempfile.TemporaryDirectory() as tmpdir:
                xml_path = Path(tmpdir) / "teach_window.xml"
                client.dump_ui(xml_path)
                nodes = parse_ui_xml(xml_path)
            package = client.current_package()
            component = client.current_component()
            # UI dump ekran ölçüsünü vermediği için mevcut ölçü; yoksa geniş güvenli varsayım.
            screen_size = self.screen_size
            if screen_size == (1, 1):
                max_x = max((node.bounds[2] for node in nodes), default=1080)
                max_y = max((node.bounds[3] for node in nodes), default=1920)
                screen_size = (max_x, max_y)
            commands = collect_actionable_ui_commands(
                nodes,
                package,
                component,
                screen_size,
            )
            self.ui_queue.put(
                (
                    "ui_library_scan",
                    {
                        "commands": commands,
                        "package": package,
                        "component": component,
                        "node_count": len(nodes),
                    },
                )
            )
        except Exception as exc:
            self.ui_queue.put(("ui_library_scan_error", str(exc)))

    def selected_ui_library_items(self) -> list[UiCommandDefinition]:
        keys = list(self.ui_library_tree.selection())
        by_key = {item.key: item for item in self.ui_library}
        return [by_key[key] for key in keys if key in by_key]

    @staticmethod
    def ui_command_to_flow_step(
        command: UiCommandDefinition,
    ) -> FlowStep:
        return FlowStep(
            action="wait_ui_tap",
            name=f"Hazır UI komutu: {command.name}",
            package=command.package,
            component=command.component,
            resource_id=command.resource_id,
            text=command.text,
            class_name=command.class_name,
            content_desc=command.content_desc,
            x=command.x,
            y=command.y,
            wait_after=0.5,
            fallback_to_coordinate=True,
            timeout_s=45.0,
            poll_interval=0.8,
        )

    def add_selected_ui_commands_to_flow(self) -> None:
        selected = self.selected_ui_library_items()
        if not selected:
            messagebox.showwarning(
                "Komut seçilmedi",
                "Akışa eklenecek bir veya daha fazla hazır komut seç.",
            )
            return
        start_index = len(self.steps)
        self.steps.extend(
            self.ui_command_to_flow_step(item)
            for item in selected
        )
        self.refresh_step_tree()
        if len(self.steps) > start_index:
            self.step_tree.selection_set(str(start_index))
            self.step_tree.see(str(start_index))
        self.log(f"{len(selected)} hazır UI komutu akışa eklendi.")
        self.main_notebook.select(self.flow_tab)

    def test_selected_ui_commands(self) -> None:
        selected = self.selected_ui_library_items()
        if len(selected) != 1:
            messagebox.showwarning(
                "Tek komut seç",
                "Test için listeden yalnızca bir hazır UI komutu seç.",
            )
            return
        if not self.validate_device():
            return
        step = self.ui_command_to_flow_step(selected[0])
        self.set_status(f"Hazır UI komutu test ediliyor: {selected[0].name}")
        threading.Thread(
            target=self._test_ui_command_worker,
            args=(step,),
            daemon=True,
        ).start()

    def _test_ui_command_worker(self, step: FlowStep) -> None:
        try:
            self.execute_step(step, self.adb(), allow_wait=True)
            self.queue_status(f"Hazır UI komutu çalıştı: {step.name}")
            self.queue_log(f"Hazır UI komutu testi başarılı: {step.name}")
        except Exception as exc:
            self.queue_status("Hazır UI komutu testi başarısız.")
            self.queue_log(f"Hazır UI komutu testi hatası — {step.name}: {exc}")

    def delete_selected_ui_commands(self) -> None:
        selected_keys = set(self.ui_library_tree.selection())
        if not selected_keys:
            return
        if not messagebox.askyesno(
            "Hazır komutları sil",
            f"{len(selected_keys)} hazır komut silinsin mi?",
        ):
            return
        self.ui_library = [
            item for item in self.ui_library
            if item.key not in selected_keys
        ]
        self.save_ui_command_library()
        self.refresh_ui_library_tree()

    def delete_filtered_ui_commands(self) -> None:
        package = self.ui_library_package_var.get()
        if package == "Tümü":
            messagebox.showwarning(
                "Paket seç",
                "Önce silinecek paketi Paket filtresinden seç.",
            )
            return
        if not messagebox.askyesno(
            "Paket komutlarını sil",
            f"{package} paketine ait bütün hazır UI komutları silinsin mi?",
        ):
            return
        self.ui_library = [
            item for item in self.ui_library
            if item.package != package
        ]
        self.save_ui_command_library()
        self.refresh_ui_library_tree()

    def find_adb(self) -> str | None:
        candidates = [
            shutil.which("adb"),
            shutil.which("nox_adb"),
            r"C:\Program Files\Nox\bin\nox_adb.exe",
            r"C:\Program Files (x86)\Nox\bin\nox_adb.exe",
            r"C:\Program Files\Bignox\BigNoxVM\RT\Nox\bin\nox_adb.exe",
            r"C:\Program Files (x86)\Bignox\BigNoxVM\RT\Nox\bin\nox_adb.exe",
            r"C:\Program Files\Nox\bin\adb.exe",
            r"C:\Program Files (x86)\Nox\bin\adb.exe",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return str(candidate)
        return None

    def adb(self) -> AdbClient:
        return AdbClient(self.adb_path_var.get().strip(), self.device_var.get().strip())

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.configure("TButton", padding=(7, 5))
            style.configure("Primary.TButton", padding=(12, 7), font=("Segoe UI", 10, "bold"))
            style.configure("Danger.TButton", padding=(10, 6))
            style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
            style.configure("Treeview", rowheight=27)
            style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        except Exception:
            pass

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        # Klavye kısayolları.
        self.root.bind("<Control-s>", lambda _event: self.save_flow())
        self.root.bind("<Control-o>", lambda _event: self.load_flow())
        self.root.bind("<Control-n>", lambda _event: self.new_flow())
        self.root.bind("<F5>", lambda _event: self.capture_and_dump())
        self.root.bind("<F9>", lambda _event: self.start_flow())
        self.root.bind("<Escape>", lambda _event: self.stop_flow())

        # Üst bağlantı alanı iki satırlıdır; dar ekranlarda yatay taşmaz.
        connection = ttk.LabelFrame(
            self.root,
            text="Bağlantı",
            padding=(10, 7),
            style="Section.TLabelframe",
        )
        connection.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 6))
        connection.columnconfigure(1, weight=1)
        connection.columnconfigure(6, weight=1)

        ttk.Label(connection, text="ADB / nox_adb").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Entry(
            connection,
            textvariable=self.adb_path_var,
        ).grid(row=0, column=1, columnspan=4, sticky="ew", padx=(7, 5))
        ttk.Button(
            connection,
            text="Gözat…",
            command=self.browse_adb,
        ).grid(row=0, column=5, padx=(0, 6))
        ttk.Button(
            connection,
            text="Cihazları Yenile",
            command=self.refresh_devices,
        ).grid(row=0, column=6, sticky="e")

        ttk.Label(connection, text="Nox cihazı").grid(
            row=1, column=0, sticky="w", pady=(7, 0)
        )
        self.device_combo = ttk.Combobox(
            connection,
            textvariable=self.device_var,
            state="readonly",
            width=27,
        )
        self.device_combo.grid(
            row=1, column=1, sticky="w", padx=(7, 12), pady=(7, 0)
        )
        ttk.Label(connection, text="Aktif paket").grid(
            row=1, column=2, sticky="w", pady=(7, 0)
        )
        ttk.Label(
            connection,
            textvariable=self.current_package_var,
        ).grid(row=1, column=3, sticky="w", padx=(7, 12), pady=(7, 0))
        ttk.Label(connection, text="Activity").grid(
            row=1, column=4, sticky="e", pady=(7, 0)
        )
        ttk.Label(
            connection,
            textvariable=self.current_activity_var,
        ).grid(row=1, column=5, sticky="w", padx=(7, 12), pady=(7, 0))
        ttk.Button(
            connection,
            text="Ekranı ve Öğeleri Yenile  [F5]",
            command=self.capture_and_dump,
        ).grid(row=1, column=6, sticky="e", pady=(7, 0))

        # Ana bölümler artık sekmelidir. Büyük yatay araç çubuğu kaldırıldı.
        self.main_notebook = ttk.Notebook(self.root)
        self.main_notebook.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=8,
            pady=(0, 5),
        )

        self.flow_tab = ttk.Frame(self.main_notebook, padding=6)
        self.action_tab = ttk.Frame(self.main_notebook, padding=10)
        self.command_tab = ttk.Frame(self.main_notebook, padding=10)
        self.proxy_tab = ttk.Frame(self.main_notebook, padding=10)
        self.clone_tab = ttk.Frame(self.main_notebook, padding=10)
        self.certificate_tab = ttk.Frame(self.main_notebook, padding=10)
        self.log_tab = ttk.Frame(self.main_notebook, padding=8)

        self.main_notebook.add(self.flow_tab, text="  Akış ve Nox  ")
        self.main_notebook.add(self.action_tab, text="  Adım Ekle  ")
        self.main_notebook.add(self.command_tab, text="  Hazır UI Komutları  ")
        self.main_notebook.add(self.proxy_tab, text="  Proxy ve Charles  ")
        self.main_notebook.add(self.clone_tab, text="  Nox Klon Döngüsü  ")
        self.main_notebook.add(self.certificate_tab, text="  Açılış Sertifikası  ")
        self.main_notebook.add(self.log_tab, text="  Günlük  ")

        # ------------------------------------------------------------------
        # Akış ve Nox sekmesi
        # ------------------------------------------------------------------
        flow_pane = ttk.Panedwindow(self.flow_tab, orient=HORIZONTAL)
        flow_pane.pack(fill=BOTH, expand=True)

        preview_side = ttk.Frame(flow_pane)
        flow_side = ttk.Frame(flow_pane)
        flow_pane.add(preview_side, weight=7)
        flow_pane.add(flow_side, weight=5)

        preview_toolbar = ttk.Frame(preview_side)
        preview_toolbar.pack(fill="x", pady=(0, 5))
        ttk.Button(
            preview_toolbar,
            text="Yenile",
            command=self.capture_and_dump,
        ).pack(side=LEFT)
        ttk.Button(
            preview_toolbar,
            text="Toplu Kaydı Başlat",
            command=self.start_bulk_recording,
            style="Primary.TButton",
        ).pack(side=LEFT, padx=6)
        self.record_start_btn = preview_toolbar.winfo_children()[-1]
        self.record_stop_btn = ttk.Button(
            preview_toolbar,
            text="Kaydı Bitir",
            command=self.stop_bulk_recording,
            state="disabled",
        )
        self.record_stop_btn.pack(side=LEFT)
        ttk.Checkbutton(
            preview_toolbar,
            text="Hafif mod",
            variable=self.light_mode_var,
        ).pack(side=RIGHT)

        self.canvas = Canvas(
            preview_side,
            background="#202020",
            highlightthickness=0,
            cursor="crosshair",
        )
        self.canvas.pack(fill=BOTH, expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_canvas_press)
        self.canvas.bind("<B1-Motion>", self.on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_canvas_release)
        self.canvas.bind("<Configure>", lambda _event: self.redraw_canvas())

        preview_info = ttk.Frame(preview_side, padding=(0, 5, 0, 0))
        preview_info.pack(fill="x")
        self.selection_var = StringVar(value="Öğe seçilmedi.")
        ttk.Label(
            preview_info,
            textvariable=self.selection_var,
            anchor="w",
        ).pack(side=LEFT, fill="x", expand=True)

        recorder = ttk.LabelFrame(
            preview_side,
            text="Toplu kayıt ayarları",
            padding=7,
            style="Section.TLabelframe",
        )
        recorder.pack(fill="x", pady=(6, 0))
        recorder.columnconfigure(5, weight=1)

        ttk.Checkbutton(
            recorder,
            text="Beklemeleri otomatik kaydet",
            variable=self.record_auto_wait_var,
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            recorder,
            text="Tek parça ekranda görsel öğret",
            variable=self.record_visual_var,
        ).grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Label(recorder, text="Yenileme").grid(
            row=0, column=2, sticky="e", padx=(12, 4)
        )
        ttk.Spinbox(
            recorder,
            from_=0.6,
            to=10,
            increment=0.2,
            textvariable=self.record_refresh_var,
            width=6,
        ).grid(row=0, column=3, sticky="w")
        ttk.Label(recorder, text="sn").grid(row=0, column=4, sticky="w", padx=(3, 0))
        self.record_status_var = StringVar(value="Kayıt kapalı")
        ttk.Label(
            recorder,
            textvariable=self.record_status_var,
            anchor="e",
        ).grid(row=0, column=5, sticky="ew", padx=(12, 0))

        # Sağ taraf: akış ve çalıştırma.
        flow_frame = ttk.LabelFrame(
            flow_side,
            text="Akış",
            padding=8,
            style="Section.TLabelframe",
        )
        flow_frame.pack(fill=BOTH, expand=True)

        flow_header = ttk.Frame(flow_frame)
        flow_header.pack(fill="x")
        ttk.Label(flow_header, text="Akış adı").pack(side=LEFT)
        ttk.Entry(
            flow_header,
            textvariable=self.flow_name_var,
        ).pack(side=LEFT, fill="x", expand=True, padx=6)
        ttk.Button(flow_header, text="Yeni", command=self.new_flow).pack(side=RIGHT)
        ttk.Button(flow_header, text="Aç…", command=self.load_flow).pack(
            side=RIGHT, padx=4
        )
        ttk.Button(flow_header, text="Kaydet", command=self.save_flow).pack(
            side=RIGHT
        )

        tree_container = ttk.Frame(flow_frame)
        tree_container.pack(fill=BOTH, expand=True, pady=(7, 6))
        tree_container.rowconfigure(0, weight=1)
        tree_container.columnconfigure(0, weight=1)

        columns = ("idx", "action", "name", "target", "condition", "wait")
        self.step_tree = ttk.Treeview(
            tree_container,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=18,
        )
        for col, title, width, stretch in [
            ("idx", "#", 38, False),
            ("action", "İşlem", 88, False),
            ("name", "Adım", 175, True),
            ("target", "Hedef", 190, True),
            ("condition", "Koşul", 165, True),
            ("wait", "Bekle", 62, False),
        ]:
            self.step_tree.heading(col, text=title)
            self.step_tree.column(col, width=width, stretch=stretch)
        tree_scroll = ttk.Scrollbar(
            tree_container,
            orient=VERTICAL,
            command=self.step_tree.yview,
        )
        self.step_tree.configure(yscrollcommand=tree_scroll.set)
        self.step_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.step_tree.bind("<Double-1>", self.edit_selected_step)

        # İşlem düğmeleri iki satıra bölündü.
        edit_buttons = ttk.Frame(flow_frame)
        edit_buttons.pack(fill="x")
        for column in range(5):
            edit_buttons.columnconfigure(column, weight=1)

        ttk.Button(
            edit_buttons, text="Yukarı", command=lambda: self.move_step(-1)
        ).grid(row=0, column=0, sticky="ew", padx=(0, 3), pady=2)
        ttk.Button(
            edit_buttons, text="Aşağı", command=lambda: self.move_step(1)
        ).grid(row=0, column=1, sticky="ew", padx=3, pady=2)
        ttk.Button(
            edit_buttons, text="Düzenle", command=self.edit_selected_step
        ).grid(row=0, column=2, sticky="ew", padx=3, pady=2)
        ttk.Button(
            edit_buttons, text="Adımı Test Et", command=self.test_selected_step
        ).grid(row=0, column=3, sticky="ew", padx=3, pady=2)
        ttk.Button(
            edit_buttons,
            text="Komuta Çevir",
            command=self.convert_selected_step_to_command,
        ).grid(row=0, column=4, sticky="ew", padx=(3, 0), pady=2)

        ttk.Button(
            edit_buttons,
            text="Adım Ekle Sekmesine Git",
            command=lambda: self.main_notebook.select(self.action_tab),
        ).grid(row=1, column=0, columnspan=2, sticky="ew", padx=(0, 3), pady=2)
        ttk.Button(
            edit_buttons, text="Seçili Adımı Sil", command=self.delete_step
        ).grid(row=1, column=2, columnspan=2, sticky="ew", padx=3, pady=2)
        ttk.Button(
            edit_buttons, text="Tümünü Temizle", command=self.clear_steps
        ).grid(row=1, column=4, sticky="ew", padx=(3, 0), pady=2)

        run_frame = ttk.LabelFrame(
            flow_side,
            text="Çalıştırma",
            padding=8,
            style="Section.TLabelframe",
        )
        run_frame.pack(fill="x", pady=(7, 0))
        run_frame.columnconfigure(4, weight=1)

        ttk.Label(run_frame, text="Tekrar").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(
            run_frame,
            from_=0,
            to=1000000,
            textvariable=self.repeat_var,
            width=8,
        ).grid(row=0, column=1, sticky="w", padx=(5, 4))
        ttk.Label(run_frame, text="0 = sınırsız").grid(row=0, column=2, sticky="w")

        self.start_selected_btn = ttk.Button(
            run_frame,
            text="Seçiliden Başlat",
            command=self.start_flow_from_selected,
        )
        self.start_selected_btn.grid(row=0, column=5, sticky="e", padx=(6, 0))
        self.stop_btn = ttk.Button(
            run_frame,
            text="Durdur  [Esc]",
            command=self.stop_flow,
            state="disabled",
        )
        self.stop_btn.grid(row=0, column=6, sticky="e", padx=(6, 0))
        self.run_btn = ttk.Button(
            run_frame,
            text="Akışı Başlat  [F9]",
            command=self.start_flow,
            style="Primary.TButton",
        )
        self.run_btn.grid(row=0, column=7, sticky="e", padx=(6, 0))

        ttk.Checkbutton(
            run_frame,
            text="Gerekirse paketi aç",
            variable=self.auto_launch_package_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(7, 0))
        ttk.Checkbutton(
            run_frame,
            text="Arka plan modu",
            variable=self.background_run_var,
        ).grid(row=1, column=2, columnspan=2, sticky="w", pady=(7, 0))
        ttk.Checkbutton(
            run_frame,
            text="Başlarken NoxPlayer'ı küçült",
            variable=self.minimize_nox_var,
        ).grid(row=1, column=4, columnspan=2, sticky="e", pady=(7, 0))
        ttk.Button(
            run_frame,
            text="Bu Nox'un Bir-Kez Durumunu Sıfırla",
            command=self.reset_current_nox_step_conditions,
        ).grid(row=1, column=6, columnspan=2, sticky="e", pady=(7, 0))

        # ------------------------------------------------------------------
        # Adım ekle sekmesi
        # ------------------------------------------------------------------
        ttk.Label(
            self.action_tab,
            text=(
                "İşlemi seç, sonra Nox görüntüsünde hedefi göster. "
                "Toplu kayıt kullanıyorsan bu sekmeye yalnızca özel adımlar için gelmen yeterli."
            ),
            wraplength=1050,
        ).pack(fill="x", pady=(0, 8))

        action_canvas = Canvas(
            self.action_tab,
            highlightthickness=0,
            borderwidth=0,
        )
        action_scroll = ttk.Scrollbar(
            self.action_tab,
            orient=VERTICAL,
            command=action_canvas.yview,
        )
        action_canvas.configure(yscrollcommand=action_scroll.set)
        action_scroll.pack(side=RIGHT, fill="y")
        action_canvas.pack(side=LEFT, fill=BOTH, expand=True)

        action_content = ttk.Frame(action_canvas)
        action_window = action_canvas.create_window(
            (0, 0),
            window=action_content,
            anchor="nw",
        )

        def resize_action_content(_event=None):
            action_canvas.configure(scrollregion=action_canvas.bbox("all"))
            action_canvas.itemconfigure(
                action_window,
                width=action_canvas.winfo_width(),
            )

        action_content.bind("<Configure>", resize_action_content)
        action_canvas.bind("<Configure>", resize_action_content)

        for column in range(2):
            action_content.columnconfigure(column, weight=1)

        def add_group(
            parent,
            title: str,
            buttons: list[tuple[str, Callable[[], None], str]],
            row: int,
            column: int,
        ):
            frame = ttk.LabelFrame(
                parent,
                text=title,
                padding=10,
                style="Section.TLabelframe",
            )
            frame.grid(
                row=row,
                column=column,
                sticky="nsew",
                padx=5,
                pady=5,
            )
            frame.columnconfigure(0, weight=1)
            frame.columnconfigure(1, weight=1)
            for index, (label, command, help_text) in enumerate(buttons):
                button = ttk.Button(frame, text=label, command=command)
                button.grid(
                    row=index,
                    column=0,
                    sticky="ew",
                    padx=(0, 8),
                    pady=3,
                )
                ttk.Label(
                    frame,
                    text=help_text,
                    wraplength=360,
                    justify="left",
                ).grid(row=index, column=1, sticky="w", pady=3)
            return frame

        add_group(
            action_content,
            "Öğe tabanlı işlemler",
            [
                (
                    "Öğeye Tık",
                    lambda: self.arm_action("tap"),
                    "Android'in gördüğü buton veya satırı seçer.",
                ),
                (
                    "Öğeyi Bekle ve Tıkla",
                    lambda: self.arm_action("wait_ui_tap"),
                    "Öğe görünene kadar bekler; sabit saniyeye bağlı değildir.",
                ),
                (
                    "Metinle Tık",
                    self.add_tap_text_step,
                    "Girilen metni Android UI ağacında arar.",
                ),
                (
                    "Uzun Bas",
                    lambda: self.arm_action("long_press"),
                    "UI öğesi üzerinde uzun basma kaydeder.",
                ),
                (
                    "Çift Tık",
                    lambda: self.arm_action("double_tap"),
                    "Aynı hedefe iki hızlı dokunuş gönderir.",
                ),
            ],
            0,
            0,
        )

        add_group(
            action_content,
            "Tek parça oyun ekranları",
            [
                (
                    "Görseli Bekle ve Tıkla",
                    lambda: self.arm_action("wait_image_tap"),
                    "Buton çevresindeki küçük görüntüyü bir kez öğretir.",
                ),
                (
                    "Kesin Koordinata Tık",
                    lambda: self.arm_action("coordinate_tap"),
                    "UI ağacını yok sayıp doğrudan X/Y pikseline basar.",
                ),
                (
                    "Koordinata Uzun Bas",
                    lambda: self.arm_action("coordinate_long_press"),
                    "Tek parça ekranda doğrudan koordinata uzun basar.",
                ),
                (
                    "Kaydır",
                    lambda: self.arm_action("swipe"),
                    "Başlangıç ve bitiş noktasını seçerek swipe kaydeder.",
                ),
            ],
            0,
            1,
        )

        add_group(
            action_content,
            "Android ve zaman",
            [
                ("Bekle", self.add_wait_step, "Akışa düzenlenebilir bekleme süresi ekler."),
                (
                    "Geri",
                    lambda: self.add_key_step("BACK", "Geri"),
                    "Android geri tuşunu ADB üzerinden gönderir.",
                ),
                (
                    "Ana Ekran",
                    lambda: self.add_key_step("HOME", "Ana ekran"),
                    "Android ana ekranına komutla döner.",
                ),
            ],
            1,
            0,
        )

        add_group(
            action_content,
            "Paket işlemleri — ekransız",
            [
                (
                    "Aktif Paketi Aç",
                    self.add_launch_current_package_step,
                    "Aktif paketi ADB komutuyla açar.",
                ),
                (
                    "Paket Seç ve Aç",
                    self.add_launch_package_step,
                    "Kurulu paketlerden birini komutla başlatır.",
                ),
                (
                    "Paket Verisini Temizle",
                    self.add_clear_data_step,
                    "pm clear kullanır; Ayarlar ekranına girmez.",
                ),
                (
                    "Paket Depolaması",
                    self.add_open_storage_step,
                    "Android storage ekranını intent ile açar.",
                ),
                (
                    "Paket Komutlarını Bul",
                    self.open_package_command_inspector,
                    "Activity, broadcast ve deep link adaylarını tarar.",
                ),
                (
                    "Hazır UI Komutları",
                    lambda: self.main_notebook.select(self.command_tab),
                    "Bir kere toplu tanıtılan uygulama düğmelerini açar.",
                ),
            ],
            1,
            1,
        )

        ttk.Label(
            action_content,
            text=(
                "İpucu: Bir işlem dışarı açık activity, broadcast veya deep link sunuyorsa "
                "'Paket Komutlarını Bul' ile tıklama adımını tamamen ekransız komuta çevirebilirsin."
            ),
            wraplength=1000,
            justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(12, 4))

        # ------------------------------------------------------------------
        # Hazır UI Komutları sekmesi
        # ------------------------------------------------------------------
        command_intro = ttk.LabelFrame(
            self.command_tab,
            text="Bir kez tanıt — daha sonra ekrana tıklamadan çalıştır",
            padding=10,
            style="Section.TLabelframe",
        )
        command_intro.pack(fill="x")
        command_intro.columnconfigure(6, weight=1)

        self.ui_teach_start_btn = ttk.Button(
            command_intro,
            text="Otomatik Tanıtmayı Başlat",
            command=self.start_ui_teaching_session,
            style="Primary.TButton",
        )
        self.ui_teach_start_btn.grid(row=0, column=0, sticky="w")
        self.ui_teach_stop_btn = ttk.Button(
            command_intro,
            text="Tanıtmayı Bitir",
            command=self.stop_ui_teaching_session,
            state="disabled",
        )
        self.ui_teach_stop_btn.grid(row=0, column=1, sticky="w", padx=(6, 0))
        ttk.Button(
            command_intro,
            text="Yalnızca Bu Ekranı Toplu Tanıt",
            command=self.teach_current_ui_screen,
        ).grid(row=0, column=2, sticky="w", padx=(14, 0))

        ttk.Label(command_intro, text="Tarama").grid(
            row=0, column=3, sticky="e", padx=(14, 4)
        )
        ttk.Spinbox(
            command_intro,
            from_=0.6,
            to=10,
            increment=0.2,
            textvariable=self.ui_teach_interval_var,
            width=6,
        ).grid(row=0, column=4, sticky="w")
        ttk.Label(command_intro, text="sn").grid(
            row=0, column=5, sticky="w", padx=(3, 0)
        )
        ttk.Label(
            command_intro,
            textvariable=self.ui_teach_status_var,
            anchor="e",
        ).grid(row=0, column=6, sticky="ew", padx=(12, 0))

        ttk.Label(
            command_intro,
            text=(
                "Otomatik tanıtma açıkken uygulamanın ekranlarını bir kez normal şekilde gezin. "
                "Program her ekrandaki resource-id, metin ve açıklamalı tıklanabilir öğeleri "
                "tek tek seçtirmeden kütüphaneye ekler. Daha sonra bu listedeki komutlar "
                "ADB ile görünmeden çalıştırılır."
            ),
            wraplength=1150,
            justify="left",
        ).grid(row=1, column=0, columnspan=7, sticky="ew", pady=(9, 0))

        command_filters = ttk.Frame(self.command_tab)
        command_filters.pack(fill="x", pady=(9, 6))
        ttk.Label(command_filters, text="Paket").pack(side=LEFT)
        self.ui_library_package_combo = ttk.Combobox(
            command_filters,
            textvariable=self.ui_library_package_var,
            state="readonly",
            width=34,
        )
        self.ui_library_package_combo.pack(side=LEFT, padx=(6, 14))
        self.ui_library_package_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.refresh_ui_library_tree(),
        )
        ttk.Label(command_filters, text="Ara").pack(side=LEFT)
        ttk.Entry(
            command_filters,
            textvariable=self.ui_library_search_var,
            width=38,
        ).pack(side=LEFT, padx=6)
        ttk.Button(
            command_filters,
            text="Aktif Paketi Göster",
            command=self.select_active_package_in_library,
        ).pack(side=LEFT, padx=(6, 0))

        library_container = ttk.Frame(self.command_tab)
        library_container.pack(fill=BOTH, expand=True)
        library_container.rowconfigure(0, weight=1)
        library_container.columnconfigure(0, weight=1)

        library_columns = (
            "name",
            "package",
            "activity",
            "resource_id",
            "text",
            "seen",
        )
        self.ui_library_tree = ttk.Treeview(
            library_container,
            columns=library_columns,
            show="headings",
            selectmode="extended",
        )
        for column, title, width, stretch in [
            ("name", "Hazır komut", 200, True),
            ("package", "Paket", 190, True),
            ("activity", "Activity", 230, True),
            ("resource_id", "resource-id", 255, True),
            ("text", "Metin / Açıklama", 210, True),
            ("seen", "Görüldü", 65, False),
        ]:
            self.ui_library_tree.heading(column, text=title)
            self.ui_library_tree.column(
                column,
                width=width,
                stretch=stretch,
            )
        library_vscroll = ttk.Scrollbar(
            library_container,
            orient=VERTICAL,
            command=self.ui_library_tree.yview,
        )
        library_hscroll = ttk.Scrollbar(
            library_container,
            orient=HORIZONTAL,
            command=self.ui_library_tree.xview,
        )
        self.ui_library_tree.configure(
            yscrollcommand=library_vscroll.set,
            xscrollcommand=library_hscroll.set,
        )
        self.ui_library_tree.grid(row=0, column=0, sticky="nsew")
        library_vscroll.grid(row=0, column=1, sticky="ns")
        library_hscroll.grid(row=1, column=0, sticky="ew")
        self.ui_library_tree.bind(
            "<Double-1>",
            lambda _event: self.test_selected_ui_commands(),
        )

        command_buttons = ttk.Frame(self.command_tab)
        command_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(
            command_buttons,
            text="Seçileni Test Et",
            command=self.test_selected_ui_commands,
        ).pack(side=LEFT)
        ttk.Button(
            command_buttons,
            text="Seçilileri Akışa Ekle",
            command=self.add_selected_ui_commands_to_flow,
            style="Primary.TButton",
        ).pack(side=LEFT, padx=6)
        ttk.Button(
            command_buttons,
            text="Seçilileri Sil",
            command=self.delete_selected_ui_commands,
        ).pack(side=RIGHT)
        ttk.Button(
            command_buttons,
            text="Paketin Tüm Komutlarını Sil",
            command=self.delete_filtered_ui_commands,
        ).pack(side=RIGHT, padx=6)

        self.ui_library_search_var.trace_add(
            "write",
            lambda *_args: self.refresh_ui_library_tree(),
        )
        self.refresh_ui_library_tree()

        # ------------------------------------------------------------------
        # Proxy ve Charles sekmesi
        # ------------------------------------------------------------------
        self.proxy_tab.columnconfigure(0, weight=1)

        proxy_frame = ttk.LabelFrame(
            self.proxy_tab,
            text="Nox Proxy",
            padding=12,
            style="Section.TLabelframe",
        )
        proxy_frame.grid(row=0, column=0, sticky="ew")
        for column in range(6):
            proxy_frame.columnconfigure(column, weight=1 if column in {1, 3} else 0)

        ttk.Checkbutton(
            proxy_frame,
            text="Proxy kullan",
            variable=self.proxy_enabled_var,
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            proxy_frame,
            text="Yeni Nox kopyalarına otomatik uygula",
            variable=self.auto_proxy_var,
        ).grid(row=0, column=1, columnspan=2, sticky="w", padx=(12, 0))
        ttk.Checkbutton(
            proxy_frame,
            text="Charles kapalıysa başlat",
            variable=self.auto_start_charles_var,
        ).grid(row=0, column=3, columnspan=2, sticky="w", padx=(12, 0))

        ttk.Label(proxy_frame, text="Yöntem").grid(
            row=1, column=0, sticky="w", pady=(10, 0)
        )
        ttk.Combobox(
            proxy_frame,
            textvariable=self.proxy_mode_var,
            values=["Charles otomatik", "ADB reverse", "LAN IP"],
            state="readonly",
            width=18,
        ).grid(row=1, column=1, sticky="w", padx=(6, 15), pady=(10, 0))
        ttk.Label(proxy_frame, text="IP / host").grid(
            row=1, column=2, sticky="e", pady=(10, 0)
        )
        ttk.Entry(
            proxy_frame,
            textvariable=self.proxy_host_var,
            width=23,
        ).grid(row=1, column=3, sticky="ew", padx=6, pady=(10, 0))
        ttk.Label(proxy_frame, text="Port").grid(
            row=1, column=4, sticky="e", pady=(10, 0)
        )
        ttk.Spinbox(
            proxy_frame,
            from_=1,
            to=65535,
            textvariable=self.proxy_port_var,
            width=8,
        ).grid(row=1, column=5, sticky="w", padx=(6, 0), pady=(10, 0))

        proxy_actions = ttk.Frame(proxy_frame)
        proxy_actions.grid(row=2, column=0, columnspan=6, sticky="ew", pady=(10, 0))
        ttk.Button(
            proxy_actions,
            text="Charles'ı Algıla",
            command=self.detect_charles_now,
        ).pack(side=LEFT)
        ttk.Button(
            proxy_actions,
            text="Bilgisayar IP'sini Bul",
            command=self.fill_detected_ip,
        ).pack(side=LEFT, padx=5)
        ttk.Button(
            proxy_actions,
            text="Şimdi Uygula",
            command=self.apply_proxy_now,
            style="Primary.TButton",
        ).pack(side=RIGHT)
        ttk.Button(
            proxy_actions,
            text="Doğrula",
            command=self.verify_proxy_now,
        ).pack(side=RIGHT, padx=5)
        ttk.Button(
            proxy_actions,
            text="Proxy'yi Temizle",
            command=self.clear_proxy_now,
        ).pack(side=RIGHT)

        ttk.Label(
            proxy_frame,
            textvariable=self.charles_status_var,
            anchor="w",
        ).grid(row=3, column=0, columnspan=6, sticky="ew", pady=(10, 0))
        ttk.Label(
            proxy_frame,
            textvariable=self.proxy_status_var,
            anchor="w",
        ).grid(row=4, column=0, columnspan=6, sticky="ew", pady=(3, 0))

        target_frame = ttk.LabelFrame(
            self.proxy_tab,
            text="Charles hedef filtresi",
            padding=12,
            style="Section.TLabelframe",
        )
        target_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        target_frame.columnconfigure(1, weight=1)
        target_frame.columnconfigure(3, weight=1)

        ttk.Checkbutton(
            target_frame,
            text="Yalnızca hedef hostu Charles'a gönder",
            variable=self.target_only_var,
            command=self.on_target_settings_changed,
        ).grid(row=0, column=0, columnspan=4, sticky="w")

        ttk.Label(target_frame, text="Hedef host").grid(
            row=1, column=0, sticky="w", pady=(9, 0)
        )
        ttk.Entry(
            target_frame,
            textvariable=self.target_host_var,
        ).grid(row=1, column=1, sticky="ew", padx=(7, 16), pady=(9, 0))
        ttk.Label(target_frame, text="Geçit portu").grid(
            row=1, column=2, sticky="w", pady=(9, 0)
        )
        ttk.Spinbox(
            target_frame,
            from_=1024,
            to=65535,
            textvariable=self.gate_port_var,
            width=9,
        ).grid(row=1, column=3, sticky="w", padx=(7, 0), pady=(9, 0))

        ttk.Label(target_frame, text="Recording Include path").grid(
            row=2, column=0, sticky="w", pady=(9, 0)
        )
        ttk.Entry(
            target_frame,
            textvariable=self.target_path_var,
        ).grid(row=2, column=1, columnspan=3, sticky="ew", padx=(7, 0), pady=(9, 0))

        target_actions = ttk.Frame(target_frame)
        target_actions.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Button(
            target_actions,
            text="Geçidi Yenile",
            command=self.restart_selective_gate,
        ).pack(side=LEFT)
        ttk.Button(
            target_actions,
            text="Charles Ayarını Kopyala",
            command=self.copy_charles_target_settings,
        ).pack(side=LEFT, padx=5)
        ttk.Label(
            target_actions,
            textvariable=self.gate_status_var,
        ).pack(side=RIGHT)

        ttk.Label(
            self.proxy_tab,
            text=(
                "Seçici geçit açıkken yalnızca hedef host Charles'a ulaşır; "
                "diğer bağlantılar doğrudan internete çıkar ve Charles oturumunu şişirmez."
            ),
            wraplength=1050,
            justify="left",
        ).grid(row=2, column=0, sticky="ew", pady=(10, 0))

        # ------------------------------------------------------------------
        # Nox klon döngüsü sekmesi
        # ------------------------------------------------------------------
        self.clone_tab.columnconfigure(0, weight=1)

        clone_source = ttk.LabelFrame(
            self.clone_tab,
            text="Altın şablon ve NoxConsole",
            padding=12,
            style="Section.TLabelframe",
        )
        clone_source.grid(row=0, column=0, sticky="ew")
        clone_source.columnconfigure(1, weight=1)
        ttk.Label(clone_source, text="NoxConsole.exe").grid(row=0, column=0, sticky="w")
        ttk.Entry(clone_source, textvariable=self.noxconsole_path_var).grid(
            row=0, column=1, sticky="ew", padx=7
        )
        ttk.Button(
            clone_source,
            text="Bul…",
            command=self.browse_noxconsole,
        ).grid(row=0, column=2, sticky="e")
        ttk.Label(clone_source, text="Örnek / şablon Nox").grid(
            row=1, column=0, sticky="w", pady=(9, 0)
        )
        self.clone_template_combo = ttk.Combobox(
            clone_source,
            textvariable=self.clone_template_var,
            state="readonly",
        )
        self.clone_template_combo.grid(
            row=1, column=1, sticky="ew", padx=7, pady=(9, 0)
        )
        ttk.Button(
            clone_source,
            text="Kopyaları Yenile",
            command=self.refresh_nox_instances,
        ).grid(row=1, column=2, sticky="e", pady=(9, 0))
        ttk.Label(
            clone_source,
            text=(
                "Şablon kopyalanırken OpenGL+, 1920×1080, yüksek FPS, ASTC ve diğer Nox "
                "ayarları aynen devralınır. Şablon hiçbir zaman silinmez; yalnızca programın "
                "oluşturduğu geçici çalışma kopyası silinir."
            ),
            wraplength=1120,
            justify="left",
        ).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        clone_cycle = ttk.LabelFrame(
            self.clone_tab,
            text="Otomatik çalışma döngüsü",
            padding=12,
            style="Section.TLabelframe",
        )
        clone_cycle.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        for column in (1, 3, 5):
            clone_cycle.columnconfigure(column, weight=1)
        ttk.Label(clone_cycle, text="Geçici kopya öneki").grid(row=0, column=0, sticky="w")
        ttk.Entry(clone_cycle, textvariable=self.clone_prefix_var).grid(
            row=0, column=1, sticky="ew", padx=(7, 16)
        )
        ttk.Label(clone_cycle, text="Klon başına akış").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(
            clone_cycle, from_=1, to=100000, textvariable=self.clone_runs_var, width=8
        ).grid(row=0, column=3, sticky="w", padx=(7, 16))
        ttk.Label(clone_cycle, text="Toplam klon").grid(row=0, column=4, sticky="w")
        ttk.Spinbox(
            clone_cycle, from_=0, to=100000, textvariable=self.clone_count_var, width=8
        ).grid(row=0, column=5, sticky="w", padx=(7, 0))
        ttk.Label(clone_cycle, text="0 = durdurulana kadar").grid(
            row=0, column=6, sticky="w", padx=(5, 0)
        )

        ttk.Label(clone_cycle, text="Kopyalama zaman aşımı").grid(
            row=1, column=0, sticky="w", pady=(9, 0)
        )
        ttk.Spinbox(
            clone_cycle, from_=60, to=3600, textvariable=self.clone_copy_timeout_var, width=8
        ).grid(row=1, column=1, sticky="w", padx=(7, 16), pady=(9, 0))
        ttk.Label(clone_cycle, text="Android açılış zaman aşımı").grid(
            row=1, column=2, sticky="w", pady=(9, 0)
        )
        ttk.Spinbox(
            clone_cycle, from_=60, to=900, textvariable=self.clone_boot_timeout_var, width=8
        ).grid(row=1, column=3, sticky="w", padx=(7, 16), pady=(9, 0))
        ttk.Checkbutton(
            clone_cycle,
            text="İş bitince geçici kopyayı kapat ve sil",
            variable=self.clone_cleanup_var,
        ).grid(row=1, column=4, columnspan=3, sticky="w", pady=(9, 0))

        ttk.Checkbutton(
            clone_cycle,
            text="Temel ayarları ayrıca zorla",
            variable=self.clone_force_basic_var,
        ).grid(row=2, column=0, sticky="w", pady=(9, 0))
        ttk.Label(clone_cycle, text="Çözünürlük w,h,dpi").grid(
            row=2, column=1, sticky="e", pady=(9, 0)
        )
        ttk.Entry(clone_cycle, textvariable=self.clone_resolution_var, width=18).grid(
            row=2, column=2, sticky="w", padx=(7, 16), pady=(9, 0)
        )
        ttk.Label(clone_cycle, text="CPU").grid(row=2, column=3, sticky="e", pady=(9, 0))
        ttk.Spinbox(
            clone_cycle, from_=1, to=8, textvariable=self.clone_cpu_var, width=6
        ).grid(row=2, column=4, sticky="w", padx=(7, 16), pady=(9, 0))
        ttk.Label(clone_cycle, text="RAM MB").grid(row=2, column=5, sticky="e", pady=(9, 0))
        ttk.Spinbox(
            clone_cycle, from_=512, to=16384, increment=512,
            textvariable=self.clone_memory_var, width=8
        ).grid(row=2, column=6, sticky="w", padx=(7, 0), pady=(9, 0))

        clone_actions = ttk.Frame(clone_cycle)
        clone_actions.grid(row=3, column=0, columnspan=7, sticky="ew", pady=(12, 0))
        self.clone_start_btn = ttk.Button(
            clone_actions,
            text="Klon Döngüsünü Başlat",
            command=self.start_clone_cycle,
            style="Primary.TButton",
        )
        self.clone_start_btn.pack(side=LEFT)
        self.clone_stop_btn = ttk.Button(
            clone_actions,
            text="Döngüyü Durdur",
            command=self.stop_clone_cycle,
            state="disabled",
        )
        self.clone_stop_btn.pack(side=LEFT, padx=6)
        ttk.Button(
            clone_actions,
            text="Şablonu Aç",
            command=self.launch_selected_template,
        ).pack(side=LEFT, padx=(12, 0))
        ttk.Button(
            clone_actions,
            text="Şablonu Kapat",
            command=self.quit_selected_template,
        ).pack(side=LEFT, padx=6)
        ttk.Label(
            clone_actions,
            textvariable=self.clone_status_var,
            anchor="e",
        ).pack(side=RIGHT, fill="x", expand=True)

        ttk.Label(
            self.clone_tab,
            text=(
                "Döngü: şablonu kopyala → geçici kopyayı aç → yeni ADB cihazını ve Android "
                "boot tamamlanmasını bekle → Charles proxy ve sertifikayı hazırla → akışı "
                "belirlenen sayıda çalıştır → Nox'u kapat → ADB'nin kapanmasını bekle → geçici "
                "kopyayı sil → yeni kopyaya geç. Uzun çalışma modunda ekran görüntüsü yalnızca "
                "görsel adımlarda alınır."
            ),
            wraplength=1120,
            justify="left",
        ).grid(row=2, column=0, sticky="ew", pady=(10, 0))

        # ------------------------------------------------------------------
        # Açılış sertifikası sekmesi
        # ------------------------------------------------------------------
        self.certificate_tab.columnconfigure(0, weight=1)

        certificate_frame = ttk.LabelFrame(
            self.certificate_tab,
            text="Nox açıldığında Charles kök sertifikasını otomatik kur",
            padding=12,
            style="Section.TLabelframe",
        )
        certificate_frame.grid(row=0, column=0, sticky="ew")
        certificate_frame.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            certificate_frame,
            text="Her Nox açılışında otomatik yükle — daima açık",
            variable=self.auto_certificate_var,
            state="disabled",
        ).grid(row=0, column=0, columnspan=3, sticky="w")

        ttk.Label(
            certificate_frame,
            text="Windows'taki sabit sertifika",
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(
            certificate_frame,
            textvariable=self.certificate_source_var,
            state="readonly",
        ).grid(row=1, column=1, sticky="ew", padx=7, pady=(10, 0))
        ttk.Button(
            certificate_frame,
            text="Sabit Dosyayı Kontrol Et",
            command=self.choose_startup_certificate,
        ).grid(row=1, column=2, sticky="e", pady=(10, 0))

        ttk.Label(
            certificate_frame,
            text="Android'deki sabit yol",
        ).grid(row=2, column=0, sticky="w", pady=(9, 0))
        ttk.Entry(
            certificate_frame,
            textvariable=self.certificate_android_path_var,
        ).grid(
            row=2,
            column=1,
            columnspan=2,
            sticky="ew",
            padx=(7, 0),
            pady=(9, 0),
        )

        ttk.Label(
            certificate_frame,
            text="Root Certificate Manager paketi",
        ).grid(row=3, column=0, sticky="w", pady=(9, 0))
        ttk.Entry(
            certificate_frame,
            textvariable=self.certificate_importer_package_var,
        ).grid(
            row=3,
            column=1,
            columnspan=2,
            sticky="ew",
            padx=(7, 0),
            pady=(9, 0),
        )

        certificate_actions = ttk.Frame(certificate_frame)
        certificate_actions.grid(
            row=4,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(12, 0),
        )
        ttk.Button(
            certificate_actions,
            text="Manuel Testi Zorla",
            command=self.install_certificate_now,
            style="Primary.TButton",
        ).pack(side=LEFT)
        ttk.Button(
            certificate_actions,
            text="Bu Nox İçin Kuruldu Durumunu Sıfırla",
            command=self.reset_current_certificate_state,
        ).pack(side=LEFT, padx=6)
        ttk.Button(
            certificate_actions,
            text="Sabit Klasörü Aç",
            command=self.open_certificate_folder,
        ).pack(side=LEFT)
        ttk.Label(
            certificate_actions,
            textvariable=self.certificate_status_var,
            anchor="e",
        ).pack(side=RIGHT)

        ttk.Label(
            certificate_frame,
            text=(
                f"Kaynak seçim yapılmadan mevcut sürümün içine gömülüdür: "
                f"{FIXED_CERTIFICATE_SOURCE}. Çalışma sırası: Android açılışını "
                "bekle → sertifikayı /sdcard/Download altındaki sabit yola gönder → "
                "Root Certificate Manager'ı aç → "
                "'Import from SD Card' öğesini resource-id ile çalıştır → Downloads "
                "ve sabit dosya adını seç → Import ve OK düğmelerini ADB ile onayla. "
                "NoxPlayer penceresi görünmek zorunda değildir. Bu izleyici Charles "
                "ayarlarından bağımsız çalışır ve herhangi bir düğmeye basılması gerekmez."
            ),
            wraplength=1120,
            justify="left",
        ).grid(
            row=5,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(12, 0),
        )

        warning_frame = ttk.LabelFrame(
            self.certificate_tab,
            text="Güvenlik ve çalışma koşulu",
            padding=12,
            style="Section.TLabelframe",
        )
        warning_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(
            warning_frame,
            text=(
                "Bu özellik seçilen CA sertifikasını o Nox kopyasının güven deposuna "
                "eklemeye çalışır ve o emülatördeki HTTPS güvenini etkiler. Yalnızca "
                "kendi test Nox'larınızda kullanın. Root Certificate Manager uygulaması "
                "ve Nox root erişimi hazır olmalıdır. Aynı sertifika, aynı Nox açılışında "
                "ikinci kez kurulmaz; Nox yeniden açıldığında görev yeniden çalışabilir."
            ),
            wraplength=1120,
            justify="left",
        ).pack(fill="x")

        # ------------------------------------------------------------------
        # Günlük sekmesi
        # ------------------------------------------------------------------
        log_toolbar = ttk.Frame(self.log_tab)
        log_toolbar.pack(fill="x", pady=(0, 6))
        ttk.Label(
            log_toolbar,
            text="Çalışma günlüğü ve hata ayrıntıları",
        ).pack(side=LEFT)

        def clear_log():
            self.log_text.delete("1.0", END)

        def copy_log():
            text = self.log_text.get("1.0", END).strip()
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()

        ttk.Button(
            log_toolbar,
            text="Panoya Kopyala",
            command=copy_log,
        ).pack(side=RIGHT)
        ttk.Button(
            log_toolbar,
            text="Temizle",
            command=clear_log,
        ).pack(side=RIGHT, padx=5)

        self.log_text = ScrolledText(
            self.log_tab,
            wrap="word",
            font=("Consolas", 9),
        )
        self.log_text.pack(fill=BOTH, expand=True)

        # Alt durum çubuğu.
        status_bar = ttk.Frame(self.root, padding=(10, 4))
        status_bar.grid(row=2, column=0, sticky="ew")
        ttk.Label(
            status_bar,
            textvariable=self.status_var,
            anchor="w",
        ).pack(side=LEFT, fill="x", expand=True)
        ttk.Label(
            status_bar,
            text="Ctrl+S Kaydet  •  Ctrl+O Aç  •  F5 Yenile  •  F9 Başlat  •  Esc Durdur",
        ).pack(side=RIGHT)

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.insert(END, f"[{stamp}] {message}\n")
        self.log_text.see(END)

    def queue_log(self, message: str) -> None:
        self.ui_queue.put(("log", message))

    def set_status(self, message: str) -> None:
        self.status_var.set(message)

    def queue_status(self, message: str) -> None:
        self.ui_queue.put(("status", message))

    def process_ui_queue(self) -> None:
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.log(str(payload))
            elif kind == "status":
                self.set_status(str(payload))
            elif kind == "runner_done":
                self.run_btn.configure(state="normal")
                self.start_selected_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")
                self.set_status(str(payload))
        self.root.after(250, self.process_ui_queue)

    def browse_adb(self) -> None:
        path = filedialog.askopenfilename(
            title="adb.exe veya nox_adb.exe seç",
            filetypes=[("ADB", "*.exe"), ("Tüm dosyalar", "*.*")],
        )
        if path:
            self.adb_path_var.set(path)
            self.refresh_devices()

    def refresh_devices(self, silent: bool = False) -> None:
        try:
            devices = AdbClient(self.adb_path_var.get().strip()).devices()
        except Exception as exc:
            if not silent:
                messagebox.showerror("ADB hatası", str(exc))
            return
        self.device_combo["values"] = devices
        if devices and self.device_var.get() not in devices:
            self.device_var.set(devices[0])
        self.proxy_applied_devices.intersection_update(devices)
        self.proxy_inflight_devices.intersection_update(devices)
        if not silent:
            self.log(f"{len(devices)} bağlı Android cihazı bulundu.")

    def detect_charles_now(self) -> None:
        self.charles_status_var.set("Charles algılanıyor…")
        threading.Thread(target=self._detect_charles_worker, args=(True,), daemon=True).start()

    def _detect_charles_worker(self, allow_start: bool) -> None:
        try:
            try:
                info = detect_charles_proxy(int(self.proxy_port_var.get()))
            except Exception:
                if not (allow_start and self.auto_start_charles_var.get()):
                    raise
                if not windows_charles_pids():
                    executable = start_charles_if_available()
                    self.ui_queue.put(("log", f"Charles başlatıldı: {executable}"))
                    deadline = time.monotonic() + 20
                    last_error: Exception | None = None
                    while time.monotonic() < deadline:
                        time.sleep(0.75)
                        try:
                            info = detect_charles_proxy(int(self.proxy_port_var.get()))
                            break
                        except Exception as exc:
                            last_error = exc
                    else:
                        raise RuntimeError(f"Charles başladı ancak proxy hazır olmadı: {last_error}")
                else:
                    raise

            self.ui_queue.put(("charles_detected", info))
        except Exception as exc:
            self.ui_queue.put(("charles_error", str(exc)))

    def current_charles_info(self, allow_start: bool = False) -> dict[str, Any]:
        try:
            return detect_charles_proxy(int(self.proxy_port_var.get()))
        except Exception:
            if not (allow_start and self.auto_start_charles_var.get()):
                raise
            if not windows_charles_pids():
                start_charles_if_available()
                deadline = time.monotonic() + 20
                last_error: Exception | None = None
                while time.monotonic() < deadline:
                    time.sleep(0.75)
                    try:
                        return detect_charles_proxy(int(self.proxy_port_var.get()))
                    except Exception as exc:
                        last_error = exc
                raise RuntimeError(f"Charles proxy hazır olmadı: {last_error}")
            raise

    def target_host_rules(self) -> list[str]:
        raw = self.target_host_var.get()
        rules = [
            normalize_proxy_hostname(item)
            for item in raw.split(",")
            if normalize_proxy_hostname(item)
        ]
        if not rules:
            raise ValueError("Hedef host boş olamaz.")
        return rules

    def ensure_selective_gate(self, charles_port: int) -> dict[str, Any]:
        gate_port = validate_proxy_port(self.gate_port_var.get())
        rules = self.target_host_rules()
        self.selective_gate.configure(
            listen_port=gate_port,
            upstream_port=charles_port,
            allowed_hosts=rules,
        )
        self.selective_gate.start()
        snapshot = self.selective_gate.snapshot()
        self.gate_status_var.set(
            f"Geçit 127.0.0.1:{gate_port} | hedef→Charles:{charles_port}"
        )
        return snapshot

    def restart_selective_gate(self) -> None:
        try:
            info = self.current_charles_info(allow_start=True)
            self.selective_gate.stop()
            snapshot = self.ensure_selective_gate(int(info["port"]))
            self.proxy_applied_devices.clear()
            self.log(
                "Seçici geçit yenilendi — "
                f"{', '.join(snapshot['allowed_hosts'])} Charles'a; diğerleri DIRECT."
            )
        except Exception as exc:
            messagebox.showerror("Seçici geçit", str(exc))

    def on_target_settings_changed(self) -> None:
        self.proxy_applied_devices.clear()
        if not self.target_only_var.get():
            self.selective_gate.stop()
            self.gate_status_var.set("Seçici geçit kapalı")
        else:
            self.restart_selective_gate()

    def copy_charles_target_settings(self) -> None:
        try:
            host = self.target_host_rules()[0]
            path = self.target_path_var.get().strip() or "/"
        except Exception as exc:
            messagebox.showerror("Hedef ayarı", str(exc))
            return
        text = (
            "Charles > Proxy > Recording Settings > Include\n"
            "Protocol: https\n"
            f"Host: {host}\n"
            "Port: 443\n"
            f"Path: {path}\n\n"
            "Charles > Proxy > SSL Proxying Settings\n"
            f"Host: {host}\n"
            "Port: 443\n"
        )
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        messagebox.showinfo(
            "Charles hedef ayarı",
            "Hedef ayarlar panoya kopyalandı.\n\n"
            "Recording Settings > Include listesinde yalnızca bu kayıt etkin olsun. "
            "SSL Proxying listesinde de aynı host:443 bulunsun.",
        )

    def update_gate_status(self) -> None:
        snapshot = self.selective_gate.snapshot()
        if not snapshot["running"]:
            self.gate_status_var.set("Seçici geçit kapalı")
            return
        self.gate_status_var.set(
            f"Hedef:{snapshot['target_connections']} | "
            f"atlanan:{snapshot['bypassed_connections']} | "
            f"hata:{snapshot['errors']}"
        )

    def fill_detected_ip(self) -> None:
        address = detect_host_ip()
        self.proxy_host_var.set(address)
        self.log(f"Bilgisayar IP adresi algılandı: {address}")

    def proxy_profile(self) -> dict[str, Any]:
        port = validate_proxy_port(self.proxy_port_var.get())
        mode = self.proxy_mode_var.get().strip()
        if mode not in {"Charles otomatik", "ADB reverse", "LAN IP"}:
            raise ValueError("Geçersiz proxy yöntemi.")
        host = validate_proxy_host(self.proxy_host_var.get())

        profile: dict[str, Any] = {
            "enabled": bool(self.proxy_enabled_var.get()),
            "mode": mode,
            "host": host,
            "port": port,
        }
        if profile["enabled"] and mode == "Charles otomatik":
            info = self.current_charles_info(allow_start=True)
            charles_port = int(info["port"])
            profile["host"] = detect_host_ip()
            profile["charles"] = info
            profile["transport"] = "ADB reverse"

            if self.target_only_var.get():
                snapshot = self.ensure_selective_gate(charles_port)
                profile["port"] = int(snapshot["listen_port"])
                profile["selective_gate"] = True
                profile["charles_port"] = charles_port
                profile["target_hosts"] = list(snapshot["allowed_hosts"])
            else:
                profile["port"] = charles_port
        return profile

    def apply_proxy_profile(self, client: AdbClient, profile: dict[str, Any]) -> str:
        if not profile.get("enabled", False):
            return client.get_http_proxy()

        port = validate_proxy_port(profile["port"])
        mode = profile.get("transport", profile["mode"])
        if mode == "ADB reverse":
            try:
                client.reverse_port(port, port)
                applied = client.set_http_proxy("127.0.0.1", port)
                if applied != f"127.0.0.1:{port}":
                    raise AdbError(f"Proxy doğrulaması başarısız: {applied}")
                if profile.get("selective_gate"):
                    hosts = ", ".join(profile.get("target_hosts", []))
                    return (
                        f"Nox {applied} → seçici geçit; yalnızca {hosts} "
                        f"Charles 127.0.0.1:{profile['charles_port']} adresine gider"
                    )
                if profile.get("mode") == "Charles otomatik":
                    return (
                        f"Nox {applied} → Charles 127.0.0.1:{port} "
                        f"(ADB reverse, bilgisayar IP'si değişse de çalışır)"
                    )
                return f"{applied} (ADB reverse)"
            except Exception as reverse_error:
                if profile.get("selective_gate"):
                    raise AdbError(
                        "Seçici hedef modu ADB reverse gerektiriyor; diğer trafiği "
                        "Charles'tan uzak tutmak için LAN'a açık proxy kullanılmadı. "
                        f"ADB reverse hatası: {reverse_error}"
                    )
                # Eski nox_adb sürümlerinde reverse bulunmayabilir.
                host = validate_proxy_host(profile["host"])
                applied = client.set_http_proxy(host, port)
                if applied != f"{host}:{port}":
                    raise AdbError(
                        f"ADB reverse başarısız ({reverse_error}); LAN proxy doğrulaması da "
                        f"başarısız: {applied}"
                    )
                return f"{applied} (LAN geri dönüşü)"
        else:
            host = validate_proxy_host(profile["host"])
            applied = client.set_http_proxy(host, port)
            if applied != f"{host}:{port}":
                raise AdbError(f"Proxy doğrulaması başarısız: {applied}")
            return applied

    def apply_proxy_now(self) -> None:
        if not self.validate_device():
            return
        try:
            profile = self.proxy_profile()
        except Exception as exc:
            messagebox.showerror("Proxy ayarı", str(exc))
            return
        serial = self.device_var.get().strip()
        self.proxy_inflight_devices.add(serial)
        self.proxy_status_var.set("Uygulanıyor…")
        threading.Thread(
            target=self._apply_proxy_worker,
            args=(serial, profile, "manuel"),
            daemon=True,
        ).start()

    def _apply_proxy_worker(self, serial: str, profile: dict[str, Any], source: str) -> None:
        try:
            client = AdbClient(self.adb_path_var.get().strip(), serial)
            result = self.apply_proxy_profile(client, profile)
            self.ui_queue.put(
                ("proxy_result", {
                    "success": True,
                    "serial": serial,
                    "message": f"{serial}: {result}",
                    "source": source,
                })
            )
        except Exception as exc:
            self.ui_queue.put(
                ("proxy_result", {
                    "success": False,
                    "serial": serial,
                    "message": f"{serial}: {exc}",
                    "source": source,
                })
            )

    def verify_proxy_now(self) -> None:
        if not self.validate_device():
            return
        serial = self.device_var.get().strip()
        self.proxy_status_var.set("Doğrulanıyor…")
        threading.Thread(target=self._verify_proxy_worker, args=(serial,), daemon=True).start()

    def _verify_proxy_worker(self, serial: str) -> None:
        try:
            value = AdbClient(self.adb_path_var.get().strip(), serial).get_http_proxy()
            self.ui_queue.put(("proxy_verify", (True, serial, value)))
        except Exception as exc:
            self.ui_queue.put(("proxy_verify", (False, serial, str(exc))))

    def clear_proxy_now(self) -> None:
        if not self.validate_device():
            return
        serial = self.device_var.get().strip()
        try:
            port = validate_proxy_port(
                self.gate_port_var.get()
                if self.target_only_var.get()
                else self.proxy_port_var.get()
            )
        except Exception as exc:
            messagebox.showerror("Proxy ayarı", str(exc))
            return
        self.proxy_status_var.set("Temizleniyor…")
        threading.Thread(
            target=self._clear_proxy_worker,
            args=(serial, port),
            daemon=True,
        ).start()

    def _clear_proxy_worker(self, serial: str, port: int) -> None:
        try:
            value = AdbClient(self.adb_path_var.get().strip(), serial).clear_http_proxy(port)
            self.ui_queue.put(("proxy_clear", (True, serial, value)))
        except Exception as exc:
            self.ui_queue.put(("proxy_clear", (False, serial, str(exc))))

    def schedule_auto_proxy(self, devices: list[str]) -> None:
        if not self.auto_proxy_var.get() or not self.proxy_enabled_var.get():
            return
        try:
            profile = self.proxy_profile()
        except Exception as exc:
            self.proxy_status_var.set(f"Ayar hatası: {exc}")
            return

        for serial in devices:
            if not probable_nox_serial(serial):
                continue
            if serial in self.proxy_applied_devices or serial in self.proxy_inflight_devices:
                continue
            self.proxy_inflight_devices.add(serial)
            self.proxy_status_var.set(f"Yeni Nox bulundu: {serial}")
            threading.Thread(
                target=self._apply_proxy_worker,
                args=(serial, profile, "otomatik"),
                daemon=True,
            ).start()

    def proxy_watch_tick(self) -> None:
        try:
            devices = AdbClient(self.adb_path_var.get().strip()).devices()
            current = set(devices)
            self.proxy_applied_devices.intersection_update(current)
            self.proxy_inflight_devices.intersection_update(current)

            # Charles otomatik modunda port yeniden başlatma/dynamic port nedeniyle
            # değişirse bütün bağlı Nox cihazlarına yeni ayarı tekrar uygula.
            if (
                self.proxy_enabled_var.get()
                and self.proxy_mode_var.get() == "Charles otomatik"
                and time.monotonic() - self.last_charles_check >= 20
            ):
                self.last_charles_check = time.monotonic()
                try:
                    info = detect_charles_proxy(int(self.proxy_port_var.get()))
                    signature = (
                        int(info["port"]),
                        tuple(info.get("pids", [])),
                        tuple(info.get("listen_hosts", [])),
                    )
                    if signature != self.last_charles_signature:
                        self.last_charles_signature = signature
                        self.proxy_port_var.set(int(info["port"]))
                        self.proxy_host_var.set(detect_host_ip())
                        if self.target_only_var.get():
                            snapshot = self.ensure_selective_gate(int(info["port"]))
                            self.charles_status_var.set(
                                f"Charles:{self.proxy_host_var.get()}:{info['port']} | "
                                f"Nox→geçit:127.0.0.1:{snapshot['listen_port']}"
                            )
                            # Nox'un geçit portu değişmediyse yeniden proxy yazmaya gerek yok.
                        else:
                            self.proxy_applied_devices.clear()
                            self.charles_status_var.set(
                                f"Charles: {self.proxy_host_var.get()}:{info['port']} | "
                                f"Nox: 127.0.0.1:{info['port']} (reverse)"
                            )
                        self.log(f"Charles proxy değişikliği algılandı: port {info['port']}")
                except Exception as exc:
                    self.charles_status_var.set(f"Charles bekleniyor: {exc}")

            # Klon farklı bir ADB seri numarasıyla açıldığı anda otomatik uygulanır.
            self.schedule_auto_proxy(devices)
            if list(self.device_combo["values"]) != devices:
                self.device_combo["values"] = devices
                if devices and self.device_var.get() not in devices:
                    self.device_var.set(devices[0])
        except Exception:
            pass
        finally:
            self.update_gate_status()
            self.root.after(10000, self.proxy_watch_tick)

    def validate_device(self) -> bool:
        if not self.device_var.get().strip():
            messagebox.showwarning("Cihaz seçilmedi", "Önce bağlı bir Nox cihazı seç.")
            return False
        return True

    def capture_and_dump(self) -> None:
        if not self.validate_device():
            return
        if self.capture_inflight:
            return
        self.capture_inflight = True
        self.set_status("Nox ekranı ve öğeleri alınıyor…")
        threading.Thread(target=self._capture_worker, daemon=True).start()

    def _capture_worker(self) -> None:
        screen_path = DATA_DIR / "latest_screen.png"
        xml_path = DATA_DIR / "latest_window.xml"
        try:
            client = self.adb()
            client.capture_screen(screen_path)
            try:
                client.dump_ui(xml_path)
                nodes = parse_ui_xml(xml_path)
            except Exception as exc:
                nodes = []
                self.queue_log(f"UI öğeleri okunamadı; koordinat modu kullanılabilir: {exc}")
            package = client.current_package()
            component = client.current_component()
            self.ui_queue.put(
                ("capture_ready", (screen_path, nodes, package, component))
            )
        except Exception as exc:
            self.ui_queue.put(("capture_error", str(exc)))

    def process_ui_queue(self) -> None:
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.log(str(payload))
            elif kind == "status":
                self.set_status(str(payload))
            elif kind == "runner_done":
                self.run_btn.configure(state="normal")
                self.start_selected_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")
                self.set_status(str(payload))
            elif kind == "capture_ready":
                self.capture_inflight = False
                path, nodes, package, component = payload
                try:
                    self.screen_image = Image.open(path).convert("RGB")
                    self.screen_size = self.screen_image.size
                    self.nodes = nodes
                    self.current_package_var.set(package or "—")
                    self.current_activity_var.set(component or "—")
                    self.redraw_canvas()
                    self.set_status(f"Ekran hazır — {len(nodes)} UI öğesi")
                    self.log(f"Ekran alındı. Aktif paket: {package or 'bilinmiyor'}, öğe: {len(nodes)}")
                except Exception as exc:
                    messagebox.showerror("Görüntü hatası", str(exc))
            elif kind == "capture_error":
                self.capture_inflight = False
                self.set_status("Ekran alınamadı.")
                if self.recording_active:
                    self.log(f"Kayıt ekranı yenilenemedi: {payload}")
                else:
                    messagebox.showerror("Yakalama hatası", str(payload))
            elif kind == "proxy_result":
                serial = payload["serial"]
                self.proxy_inflight_devices.discard(serial)
                if payload["success"]:
                    self.proxy_applied_devices.add(serial)
                    self.proxy_status_var.set(f"Proxy hazır: {serial}")
                    self.log(f"Proxy uygulandı ({payload['source']}): {payload['message']}")
                else:
                    self.proxy_applied_devices.discard(serial)
                    self.proxy_status_var.set("Proxy uygulanamadı")
                    self.log(f"Proxy hatası ({payload['source']}): {payload['message']}")
            elif kind == "proxy_verify":
                success, serial, value = payload
                if success:
                    self.proxy_status_var.set(f"{serial}: {value}")
                    self.log(f"Proxy doğrulandı — {serial}: {value}")
                else:
                    self.proxy_status_var.set("Proxy doğrulanamadı")
                    self.log(f"Proxy doğrulama hatası — {serial}: {value}")
            elif kind == "proxy_clear":
                success, serial, value = payload
                self.proxy_applied_devices.discard(serial)
                self.proxy_inflight_devices.discard(serial)
                if success:
                    self.proxy_status_var.set(f"Proxy temizlendi: {serial}")
                    self.log(f"Proxy temizlendi — {serial}; mevcut değer: {value}")
                else:
                    self.proxy_status_var.set("Proxy temizlenemedi")
                    self.log(f"Proxy temizleme hatası — {serial}: {value}")
            elif kind == "charles_detected":
                info = payload
                host = detect_host_ip()
                port = int(info["port"])
                self.proxy_host_var.set(host)
                self.proxy_port_var.set(port)
                if self.target_only_var.get():
                    try:
                        snapshot = self.ensure_selective_gate(port)
                        nox_port = snapshot["listen_port"]
                        self.charles_status_var.set(
                            f"Charles:{host}:{port} | Nox→geçit:127.0.0.1:{nox_port}"
                        )
                    except Exception as exc:
                        self.charles_status_var.set(f"Geçit hatası: {exc}")
                else:
                    self.charles_status_var.set(
                        f"Charles: {host}:{port} | Nox: 127.0.0.1:{port} (reverse)"
                    )
                signature = (
                    port,
                    tuple(info.get("pids", [])),
                    tuple(info.get("listen_hosts", [])),
                )
                if signature != self.last_charles_signature:
                    self.last_charles_signature = signature
                    self.proxy_applied_devices.clear()
                self.log(
                    f"Charles algılandı — port {port}; "
                    f"PID {', '.join(map(str, info.get('pids', []))) or 'bilinmiyor'}"
                )
            elif kind == "charles_error":
                self.charles_status_var.set(f"Charles hatası: {payload}")
                self.log(f"Charles algılama hatası: {payload}")
            elif kind == "package_inspection":
                dialog = payload.get("dialog")
                if dialog is not None and dialog.winfo_exists():
                    payload["callback"](payload)
            elif kind == "package_inspection_error":
                dialog = payload.get("dialog")
                if dialog is not None and dialog.winfo_exists():
                    payload["callback"](payload["message"])
            elif kind == "package_command_test":
                payload["callback"](
                    bool(payload["success"]),
                    str(payload["message"]),
                )
            elif kind == "ui_library_scan":
                self.ui_teach_scan_inflight = False
                commands = payload["commands"]
                added, updated = self.merge_ui_commands(commands)
                package = payload.get("package") or "bilinmiyor"
                component = payload.get("component") or "bilinmiyor"
                self.current_package_var.set(package)
                self.current_activity_var.set(component)
                self.ui_teach_status_var.set(
                    f"{package}: +{added} yeni, {updated} güncellendi — "
                    f"toplam {len(self.ui_library)}"
                )
                if added or not self.ui_teach_active:
                    self.log(
                        f"UI toplu tanıtma — {package}: {added} yeni, "
                        f"{updated} güncellendi; activity={component}"
                    )
            elif kind == "ui_library_scan_error":
                self.ui_teach_scan_inflight = False
                self.ui_teach_status_var.set(f"Tanıtma hatası: {payload}")
                self.log(f"UI toplu tanıtma hatası: {payload}")
            elif kind == "certificate_done":
                success, serial, reason, message = payload
                with self.certificate_watch_lock:
                    self.certificate_inflight_devices.discard(serial)
                if success:
                    self.certificate_retry_after.pop(serial, None)
                    self.certificate_status_var.set(
                        f"{serial}: {message}"
                    )
                    self.log(
                        f"Sertifika görevi tamamlandı ({reason}) — "
                        f"{serial}: {message}"
                    )
                else:
                    self.certificate_status_var.set(
                        f"{serial}: kurulum hatası"
                    )
                    self.log(
                        f"Sertifika görevi hatası ({reason}) — "
                        f"{serial}: {message}"
                    )
            elif kind == "certificate_status":
                success, serial, message = payload
                self.certificate_status_var.set(
                    f"{serial}: {message}"
                )
                self.log(
                    f"Sertifika durumu — {serial}: {message}"
                )
            elif kind == "certificate_waiting":
                serial, message = payload
                self.certificate_status_var.set(
                    f"{serial}: {message}"
                )
            elif kind == "clone_status":
                self.clone_status_var.set(str(payload))
            elif kind == "select_device":
                serial = str(payload)
                values = list(self.device_combo["values"])
                if serial not in values:
                    values.append(serial)
                    self.device_combo["values"] = values
                self.device_var.set(serial)
            elif kind == "clone_done":
                self.clone_status_var.set(str(payload))
                self.clone_start_btn.configure(state="normal")
                self.clone_stop_btn.configure(state="disabled")
                self.run_btn.configure(state="normal")
                self.start_selected_btn.configure(state="normal")
                self.runner_stop.clear()
                self.clone_cycle_stop.clear()
        self.root.after(250, self.process_ui_queue)

    def redraw_canvas(self) -> None:
        self.canvas.delete("all")
        if self.screen_image is None:
            self.canvas.create_text(
                max(200, self.canvas.winfo_width() // 2),
                max(120, self.canvas.winfo_height() // 2),
                text="“Ekranı + Öğeleri Yenile” ile Nox görüntüsünü al.",
                fill="white",
                font=("Segoe UI", 14),
            )
            return

        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        iw, ih = self.screen_image.size
        scale = min(cw / iw, ch / ih)
        dw, dh = max(1, int(iw * scale)), max(1, int(ih * scale))
        self.display_scale = scale
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        self.display_offset = (ox, oy)

        resized = self.screen_image.resize((dw, dh), Image.Resampling.LANCZOS)
        self.screen_photo = ImageTk.PhotoImage(resized)
        self.canvas.create_image(ox, oy, image=self.screen_photo, anchor="nw", tags="screen")

        # Yalnızca seçili öğeyi çiz; hafif ve sade.
        if self.selected_node:
            x1, y1, x2, y2 = self.selected_node.bounds
            self.canvas.create_rectangle(
                ox + x1 * scale,
                oy + y1 * scale,
                ox + x2 * scale,
                oy + y2 * scale,
                outline="#00ff88",
                width=3,
                tags="selection",
            )

        if self.swipe_start:
            sx, sy = self.swipe_start
            self.canvas.create_oval(
                ox + sx * scale - 5,
                oy + sy * scale - 5,
                ox + sx * scale + 5,
                oy + sy * scale + 5,
                outline="#ffcc00",
                width=3,
            )

    def canvas_to_device(self, event_x: int, event_y: int) -> tuple[int, int] | None:
        if self.screen_image is None:
            return None
        ox, oy = self.display_offset
        x = int((event_x - ox) / self.display_scale)
        y = int((event_y - oy) / self.display_scale)
        iw, ih = self.screen_size
        if 0 <= x < iw and 0 <= y < ih:
            return x, y
        return None

    def arm_action(self, action: str) -> None:
        if self.screen_image is None:
            messagebox.showinfo("Önce ekranı al", "Önce Nox ekranını yenile.")
            return
        self.pending_action = action
        self.swipe_start = None
        labels = {
            "tap": "UI öğesi seçilecek yeri seç.",
            "wait_ui_tap": "Görünmesi beklenecek Android öğesini seç.",
            "wait_image_tap": "Bir kez öğretilecek görsel butonun merkezini seç.",
            "coordinate_tap": "Kesin basılacak Nox pikselini seç.",
            "long_press": "UI öğesi üzerinde uzun basılacak yeri seç.",
            "coordinate_long_press": "Kesin uzun basılacak Nox pikselini seç.",
            "double_tap": "Çift tıklanacak yeri seç.",
            "swipe": "Kaydırmanın başlangıç noktasını seç.",
        }
        self.set_status(labels[action])

    def start_bulk_recording(self) -> None:
        if not self.validate_device():
            return
        if self.runner_thread and self.runner_thread.is_alive():
            messagebox.showwarning(
                "Akış çalışıyor",
                "Toplu kayıt başlamadan önce çalışan akışı durdur.",
            )
            return
        if self.steps:
            answer = messagebox.askyesnocancel(
                "Toplu kaydı başlat",
                "Mevcut adımlar silinsin mi?\n\n"
                "Evet: temizleyip yeni kayıt\n"
                "Hayır: mevcut akışın sonuna ekle",
            )
            if answer is None:
                return
            if answer:
                self.steps.clear()
                self.refresh_step_tree()

        self.recording_active = True
        self.recording_press = None
        self.recording_last_release = time.monotonic()
        self.recording_initial_package_added = False
        self.pending_action = None
        self.swipe_start = None
        self.record_start_btn.configure(state="disabled")
        self.record_stop_btn.configure(state="normal")
        self.record_status_var.set("KAYIT AÇIK — ekranda işlemi normal yap")
        self.set_status("Toplu kayıt başladı.")
        self.log(
            "Toplu kayıt başladı. Canvas üzerindeki tık, uzun bas ve kaydırmalar "
            "hem Nox'a gönderilecek hem akışa eklenecek."
        )
        self.capture_and_dump()
        self.schedule_recording_refresh()

    def stop_bulk_recording(self) -> None:
        if not self.recording_active:
            return
        self.recording_active = False
        self.recording_press = None
        if self.recording_refresh_job is not None:
            try:
                self.root.after_cancel(self.recording_refresh_job)
            except Exception:
                pass
            self.recording_refresh_job = None
        self.record_start_btn.configure(state="normal")
        self.record_stop_btn.configure(state="disabled")
        self.record_status_var.set(f"Kayıt tamamlandı — {len(self.steps)} adım")
        self.set_status("Toplu kayıt tamamlandı.")
        self.log(f"Toplu kayıt bitti. Toplam {len(self.steps)} adım.")

    def schedule_recording_refresh(self) -> None:
        if not self.recording_active:
            return
        try:
            seconds = max(0.6, float(self.record_refresh_var.get()))
        except Exception:
            seconds = 1.2
        self.recording_refresh_job = self.root.after(
            int(seconds * 1000),
            self.recording_refresh_tick,
        )

    def recording_refresh_tick(self) -> None:
        self.recording_refresh_job = None
        if not self.recording_active:
            return
        self.capture_and_dump()
        self.schedule_recording_refresh()

    def on_canvas_press(self, event) -> None:
        if not self.recording_active:
            return
        coords = self.canvas_to_device(event.x, event.y)
        if coords is None:
            return
        self.recording_press = {
            "start": coords,
            "end": coords,
            "started": time.monotonic(),
        }

    def on_canvas_drag(self, event) -> None:
        if not self.recording_active or self.recording_press is None:
            return
        coords = self.canvas_to_device(event.x, event.y)
        if coords is not None:
            self.recording_press["end"] = coords

    def on_canvas_release(self, event) -> None:
        if not self.recording_active:
            self.on_canvas_click(event)
            return
        if self.recording_press is None:
            return

        coords = self.canvas_to_device(event.x, event.y)
        if coords is not None:
            self.recording_press["end"] = coords

        press = self.recording_press
        self.recording_press = None
        start_x, start_y = press["start"]
        end_x, end_y = press["end"]
        started = float(press["started"])
        released = time.monotonic()
        duration_ms = max(1, int((released - started) * 1000))
        distance = ((end_x - start_x) ** 2 + (end_y - start_y) ** 2) ** 0.5

        self.record_initial_package_if_needed()
        self.record_interaction_delay(started)

        if distance >= 28:
            step = FlowStep(
                action="swipe",
                name=f"Kaydır: ({start_x},{start_y}) → ({end_x},{end_y})",
                package=self.current_recording_package(),
                x=start_x,
                y=start_y,
                x2=end_x,
                y2=end_y,
                duration_ms=max(150, duration_ms),
                wait_after=0.0,
            )
            execute = ("swipe", start_x, start_y, end_x, end_y, step.duration_ms)
        elif duration_ms >= max(250, int(self.record_long_press_ms_var.get())):
            step = FlowStep(
                action="long_press",
                name=f"Koordinata uzun bas: {start_x},{start_y}",
                package=self.current_recording_package(),
                x=start_x,
                y=start_y,
                duration_ms=duration_ms,
                wait_after=0.0,
                fallback_to_coordinate=True,
            )
            execute = ("long_press", start_x, start_y, duration_ms)
        else:
            step = self.build_automatic_recorded_tap(start_x, start_y)
            execute = ("tap", start_x, start_y)

        self.steps.append(step)
        self.refresh_step_tree()
        self.step_tree.selection_set(str(len(self.steps) - 1))
        self.step_tree.see(str(len(self.steps) - 1))
        self.record_status_var.set(
            f"KAYIT AÇIK — {len(self.steps)} adım | son: {step.name}"
        )
        self.log(f"Otomatik kaydedildi: {step.name}")
        self.recording_last_release = released

        threading.Thread(
            target=self.execute_recorded_gesture,
            args=(execute,),
            daemon=True,
        ).start()

    def current_recording_package(self) -> str:
        package = self.current_package_var.get().strip()
        return "" if package == "—" else package

    def record_initial_package_if_needed(self) -> None:
        if self.recording_initial_package_added:
            return
        package = self.current_recording_package()
        if package and package not in {
            "com.android.launcher",
            "com.android.launcher3",
            "com.android.settings",
        }:
            self.steps.append(
                FlowStep(
                    action="launch_package",
                    name=f"Başlangıç paketini aç: {package}",
                    package=package,
                    wait_after=1.0,
                )
            )
        self.recording_initial_package_added = True

    def record_interaction_delay(self, started: float) -> None:
        if not self.record_auto_wait_var.get():
            return
        if self.recording_last_release is None:
            return
        try:
            threshold = max(0.0, float(self.record_wait_threshold_var.get()))
        except Exception:
            threshold = 0.35
        delay = max(0.0, started - self.recording_last_release)
        if delay < threshold:
            return
        # Kullanıcının ekrandaki sonucu beklediği süreyi koru.
        delay = round(delay, 2)
        self.steps.append(
            FlowStep(
                action="wait",
                name=f"Kayıttan bekleme: {delay:g} sn",
                wait_after=delay,
            )
        )

    def build_automatic_recorded_tap(self, x: int, y: int) -> FlowStep:
        package = self.current_recording_package()
        node = find_best_node(self.nodes, x, y)
        generic = generic_fullscreen_node(node, self.screen_size)

        if node is not None and not generic and (
            node.resource_id or node.text or node.content_desc
        ):
            label = (
                node.text
                or node.content_desc
                or node.resource_id.rsplit("/", 1)[-1]
                or "öğe"
            )
            return FlowStep(
                action="wait_ui_tap",
                name=f"Öğeyi bekle ve tıkla: {label}",
                package=node.package or package,
                resource_id=node.resource_id,
                text=node.text,
                class_name=node.class_name,
                content_desc=node.content_desc,
                x=x,
                y=y,
                wait_after=0.2,
                fallback_to_coordinate=True,
                timeout_s=45.0,
                poll_interval=0.8,
            )

        if self.record_visual_var.get() and self.screen_image is not None:
            return self.build_recorded_visual_step(x, y, package)

        return FlowStep(
            action="tap",
            name=f"Kesin koordinata tık: {x},{y}",
            package=package,
            x=x,
            y=y,
            wait_after=0.2,
            fallback_to_coordinate=True,
        )

    def build_recorded_visual_step(
        self,
        x: int,
        y: int,
        package: str,
        width: int = 200,
        height: int = 90,
    ) -> FlowStep:
        if self.screen_image is None:
            raise RuntimeError("Görsel kayıt için ekran bulunamadı.")
        sw, sh = self.screen_image.size
        width = min(width, sw)
        height = min(height, sh)
        region_x = max(0, min(sw - width, x - width // 2))
        region_y = max(0, min(sh - height, y - height // 2))
        crop = self.screen_image.crop(
            (region_x, region_y, region_x + width, region_y + height)
        )
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return FlowStep(
            action="wait_image_tap",
            name=f"Görseli bekle ve tıkla: {x},{y}",
            package=package,
            x=x,
            y=y,
            wait_after=0.2,
            fallback_to_coordinate=True,
            timeout_s=60.0,
            poll_interval=0.9,
            template_png_base64=encoded,
            region_x=region_x,
            region_y=region_y,
            region_w=width,
            region_h=height,
            similarity=0.90,
        )

    def execute_recorded_gesture(self, gesture: tuple[Any, ...]) -> None:
        try:
            client = self.adb()
            action = gesture[0]
            if action == "tap":
                client.tap(int(gesture[1]), int(gesture[2]))
            elif action == "long_press":
                client.long_press(
                    int(gesture[1]),
                    int(gesture[2]),
                    int(gesture[3]),
                )
            elif action == "swipe":
                client.swipe(
                    int(gesture[1]),
                    int(gesture[2]),
                    int(gesture[3]),
                    int(gesture[4]),
                    int(gesture[5]),
                )
            else:
                raise ValueError(f"Bilinmeyen kayıt hareketi: {action}")
        except Exception as exc:
            self.queue_log(f"Kaydedilen hareket Nox'a gönderilemedi: {exc}")

    def on_canvas_click(self, event) -> None:
        coords = self.canvas_to_device(event.x, event.y)
        if coords is None:
            return
        x, y = coords
        node = find_best_node(self.nodes, x, y)
        self.selected_node = node
        if node:
            label = node.text or node.content_desc or node.resource_id or node.class_name
            target = node.target_center
            ancestor_note = " | üst satır hedefi" if node.click_bounds and node.click_bounds != node.bounds else ""
            generic_note = ""
            if node.resource_id == "android:id/content" or (
                node.class_name.endswith("FrameLayout") and node.area >= self.screen_size[0] * self.screen_size[1] * 0.7
            ):
                generic_note = " | UYARI: tek parça yüzey; Kesin Koordinat kullan"
            self.selection_var.set(
                f"Seçili: {label or 'öğe'} | id={node.resource_id or '—'} | "
                f"class={node.class_name or '—'} | hedef={target[0]},{target[1]}{ancestor_note}{generic_note}"
            )
        else:
            self.selection_var.set(f"Koordinat: {x}, {y} — UI öğesi bulunamadı.")
        self.redraw_canvas()

        if not self.pending_action:
            return

        action = self.pending_action
        force_coordinate = action in {"coordinate_tap", "coordinate_long_press"}
        dialog_action = {
            "coordinate_tap": "tap",
            "coordinate_long_press": "long_press",
        }.get(action, action)

        if action == "wait_image_tap":
            self.pending_action = None
            package = self.current_package_var.get()
            if package == "—":
                package = ""
            dialog = VisualWaitDialog(
                self.root,
                self.screen_image,
                x,
                y,
                package,
            )
            if dialog.result:
                self.steps.append(dialog.result)
                self.refresh_step_tree()
                self.log(f"Görsel adım öğretildi: {dialog.result.name}")
            self.set_status("Hazır")
            return

        if action == "swipe":
            if self.swipe_start is None:
                self.swipe_start = (x, y)
                self.set_status("Şimdi kaydırmanın bitiş noktasını seç.")
                self.redraw_canvas()
                return
            start = self.swipe_start
            self.swipe_start = None
            self.pending_action = None
            dialog = SwipeDialog(self.root, start, (x, y), self.current_package_var.get())
            if dialog.result:
                self.steps.append(dialog.result)
                self.refresh_step_tree()
            self.set_status("Hazır")
            self.redraw_canvas()
            return

        self.pending_action = None
        dialog = NodePickerDialog(
            self.root,
            "Adım ekle",
            dialog_action,
            (None if force_coordinate else node),
            x,
            y,
            self.current_package_var.get() if self.current_package_var.get() != "—" else "",
            force_coordinate=force_coordinate,
        )
        if dialog.result:
            self.steps.append(dialog.result)
            self.refresh_step_tree()
            self.log(f"Adım eklendi: {dialog.result.name}")
        self.set_status("Hazır")

    def add_wait_step(self) -> None:
        seconds = simpledialog.askfloat("Bekle", "Kaç saniye beklensin?", minvalue=0, maxvalue=86400)
        if seconds is None:
            return
        self.steps.append(FlowStep(action="wait", name=f"{seconds:g} saniye bekle", wait_after=float(seconds)))
        self.refresh_step_tree()

    def add_key_step(self, key: str, name: str) -> None:
        wait_after = simpledialog.askfloat(
            name, "İşlemden sonra kaç saniye beklensin?", initialvalue=1.0, minvalue=0, maxvalue=120
        )
        if wait_after is None:
            return
        self.steps.append(
            FlowStep(action="keyevent", name=name, text=key, wait_after=float(wait_after))
        )
        self.refresh_step_tree()

    def add_launch_current_package_step(self) -> None:
        package = self.current_package_var.get().strip()
        if not package or package == "—":
            messagebox.showwarning("Paket bilinmiyor", "Önce ekranı yenileyip aktif paketi algıla.")
            return
        self._append_launch_step(package)

    def add_tap_text_step(self) -> None:
        """Ekran görüntüsü koordinatı yerine Android UI metniyle hedef oluşturur."""
        text = simpledialog.askstring(
            "Metinle tık",
            "Ekranda görünecek metni yazın.\n"
            "Örnek: Storage & cache",
            parent=self.root,
        )
        if not text or not text.strip():
            return
        wait_after = simpledialog.askfloat(
            "Metinle tık",
            "Tıklamadan sonra kaç saniye beklensin?",
            initialvalue=1.0,
            minvalue=0,
            maxvalue=120,
            parent=self.root,
        )
        if wait_after is None:
            return
        package = self.current_package_var.get().strip()
        if package == "—":
            package = ""
        self.steps.append(
            FlowStep(
                action="tap",
                name=f"Metne tık: {text.strip()}",
                package=package,
                text=text.strip(),
                wait_after=float(wait_after),
                fallback_to_coordinate=False,
            )
        )
        self.refresh_step_tree()

    def choose_package(
        self,
        title: str,
        button_text: str,
        callback: Callable[[str], None],
    ) -> None:
        if not self.validate_device():
            return
        self.set_status("Paketler okunuyor…")
        try:
            packages = self.adb().list_packages(third_party_only=True)
        except Exception as exc:
            messagebox.showerror("Paketler okunamadı", str(exc))
            self.set_status("Hazır")
            return

        dialog = Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("560x520")
        dialog.transient(self.root)
        dialog.grab_set()

        search_var = StringVar()
        ttk.Label(dialog, text="Filtre:").pack(anchor="w", padx=10, pady=(10, 2))
        search = ttk.Entry(dialog, textvariable=search_var)
        search.pack(fill="x", padx=10)
        tree = ttk.Treeview(dialog, columns=("package",), show="headings")
        tree.heading("package", text="Paket adı")
        tree.column("package", width=510)
        tree.pack(fill=BOTH, expand=True, padx=10, pady=8)

        def rebuild(*_):
            search_text = search_var.get().lower().strip()
            for item in tree.get_children():
                tree.delete(item)
            for package in packages:
                if not search_text or search_text in package.lower():
                    tree.insert("", END, values=(package,))

        def accept():
            selection = tree.selection()
            if not selection:
                return
            package = tree.item(selection[0], "values")[0]
            dialog.destroy()
            callback(package)
            self.set_status("Hazır")

        search_var.trace_add("write", rebuild)
        tree.bind("<Double-1>", lambda _e: accept())
        ttk.Button(dialog, text=button_text, command=accept).pack(pady=(0, 10))
        rebuild()
        search.focus_set()

    def package_builtin_candidates(
        self,
        package: str,
        launcher_component: str,
    ) -> list[PackageCommandCandidate]:
        candidates = [
            PackageCommandCandidate(
                kind="launch_package",
                label="Paketi normal aç",
                package=package,
                detail=package,
            ),
            PackageCommandCandidate(
                kind="force_stop_package",
                label="Paketi arka planda zorla kapat",
                package=package,
                detail="am force-stop",
            ),
            PackageCommandCandidate(
                kind="clear_app_data",
                label="Paket verisini ve önbelleğini temizle",
                package=package,
                detail="pm clear",
            ),
            PackageCommandCandidate(
                kind="open_app_details",
                label="Uygulama bilgi ekranını aç",
                package=package,
                detail="Android Settings intent",
            ),
            PackageCommandCandidate(
                kind="open_app_storage",
                label="Storage & cache ekranını aç",
                package=package,
                detail="Android Settings intent",
            ),
        ]
        if launcher_component:
            candidates.append(
                PackageCommandCandidate(
                    kind="launch_activity",
                    label=f"Launcher activity aç: {launcher_component}",
                    package=package,
                    component=launcher_component,
                    intent_action="android.intent.action.MAIN",
                    detail="Doğrudan activity",
                )
            )
        return candidates

    def convert_selected_step_to_command(self) -> None:
        index = self.selected_step_index()
        if index is None:
            messagebox.showwarning("Adım seçilmedi", "Önce dönüştürülecek adımı seç.")
            return
        step = self.steps[index]
        self.open_package_command_inspector(
            replace_index=index,
            initial_package=step.package,
        )

    def open_package_command_inspector(
        self,
        replace_index: int | None = None,
        initial_package: str = "",
    ) -> None:
        if not self.validate_device():
            return

        dialog = Toplevel(self.root)
        dialog.title(
            "Paket Komutlarını Bul"
            + (" — seçili adımı değiştir" if replace_index is not None else "")
        )
        dialog.geometry("1040x690")
        dialog.transient(self.root)
        dialog.grab_set()

        package_var = StringVar(
            value=(
                initial_package
                or (
                    self.current_package_var.get()
                    if self.current_package_var.get() != "—"
                    else ""
                )
            )
        )
        filter_var = StringVar()
        status_var = StringVar(value="Paket seçip İncele düğmesine bas.")
        candidates_by_item: dict[str, PackageCommandCandidate] = {}
        all_candidates: list[PackageCommandCandidate] = []

        top = ttk.Frame(dialog, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Paket:").pack(side=LEFT)
        package_combo = ttk.Combobox(
            top,
            textvariable=package_var,
            width=52,
        )
        package_combo.pack(side=LEFT, padx=6)
        ttk.Button(
            top,
            text="Paketleri Yenile",
            command=lambda: load_packages(),
        ).pack(side=LEFT)
        ttk.Button(
            top,
            text="İncele",
            command=lambda: inspect_package(),
        ).pack(side=LEFT, padx=6)
        ttk.Label(top, textvariable=status_var).pack(side=RIGHT)

        filter_row = ttk.Frame(dialog, padding=(10, 0, 10, 6))
        filter_row.pack(fill="x")
        ttk.Label(filter_row, text="Sonuç filtresi:").pack(side=LEFT)
        ttk.Entry(filter_row, textvariable=filter_var, width=45).pack(
            side=LEFT, padx=6
        )
        ttk.Label(
            filter_row,
            text=(
                "Not: bulunan bileşen dışarı açık görünse bile gerçek cihazda Test ile doğrula."
            ),
        ).pack(side=LEFT, padx=10)

        columns = ("type", "label", "component", "action", "detail")
        tree = ttk.Treeview(
            dialog,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        for column, title, width in [
            ("type", "Tür", 120),
            ("label", "Komut", 260),
            ("component", "Component", 245),
            ("action", "Intent / Şema", 225),
            ("detail", "Açıklama", 180),
        ]:
            tree.heading(column, text=title)
            tree.column(column, width=width, stretch=column in {"label", "component", "action"})
        tree.pack(fill=BOTH, expand=True, padx=10, pady=(0, 8))

        buttons = ttk.Frame(dialog, padding=(10, 0, 10, 10))
        buttons.pack(fill="x")
        ttk.Button(
            buttons,
            text="Komutu Test Et",
            command=lambda: test_selected(),
        ).pack(side=LEFT)
        add_text = (
            "Seçili Adımı Bu Komutla Değiştir"
            if replace_index is not None
            else "Akışa Komut Adımı Ekle"
        )
        ttk.Button(
            buttons,
            text=add_text,
            command=lambda: add_selected(),
        ).pack(side=LEFT, padx=6)
        ttk.Button(
            buttons,
            text="Kapat",
            command=dialog.destroy,
        ).pack(side=RIGHT)

        def candidate_action_text(candidate: PackageCommandCandidate) -> str:
            if candidate.kind == "open_uri":
                return f"{candidate.uri_scheme}://"
            return candidate.intent_action

        def rebuild(*_args) -> None:
            needle = filter_var.get().strip().casefold()
            for item in tree.get_children():
                tree.delete(item)
            candidates_by_item.clear()
            for candidate in all_candidates:
                haystack = " ".join(
                    [
                        candidate.kind,
                        candidate.label,
                        candidate.component,
                        candidate.intent_action,
                        candidate.uri_scheme,
                        candidate.detail,
                    ]
                ).casefold()
                if needle and needle not in haystack:
                    continue
                item = tree.insert(
                    "",
                    END,
                    values=(
                        candidate.kind,
                        candidate.label,
                        candidate.component,
                        candidate_action_text(candidate),
                        candidate.detail,
                    ),
                )
                candidates_by_item[item] = candidate

        def load_packages() -> None:
            status_var.set("Paketler okunuyor…")
            try:
                packages = self.adb().list_packages(third_party_only=True)
            except Exception as exc:
                messagebox.showerror("Paket listesi", str(exc), parent=dialog)
                status_var.set("Paketler okunamadı.")
                return
            package_combo["values"] = packages
            if packages and package_var.get() not in packages:
                package_var.set(packages[0])
            status_var.set(f"{len(packages)} üçüncü taraf paket bulundu.")

        def inspect_package() -> None:
            package = package_var.get().strip()
            try:
                package = AdbClient.validate_package(package)
            except Exception as exc:
                messagebox.showerror("Paket", str(exc), parent=dialog)
                return
            status_var.set("Paket komutları taranıyor…")
            tree.delete(*tree.get_children())

            def worker() -> None:
                try:
                    client = self.adb()
                    launcher = client.resolve_launcher_activity(package)
                    dump_text = client.package_dump(package)
                    discovered = parse_package_resolvers(dump_text, package)
                    candidates = self.package_builtin_candidates(package, launcher)
                    candidates.extend(discovered)
                    self.ui_queue.put(
                        (
                            "package_inspection",
                            {
                                "dialog": dialog,
                                "package": package,
                                "launcher": launcher,
                                "candidates": candidates,
                                "callback": finish_inspection,
                            },
                        )
                    )
                except Exception as exc:
                    self.ui_queue.put(
                        (
                            "package_inspection_error",
                            {
                                "dialog": dialog,
                                "message": str(exc),
                                "callback": finish_error,
                            },
                        )
                    )

            threading.Thread(target=worker, daemon=True).start()

        def finish_inspection(payload: dict[str, Any]) -> None:
            all_candidates.clear()
            all_candidates.extend(payload["candidates"])
            rebuild()
            status_var.set(
                f"{payload['package']}: {len(all_candidates)} komut adayı; "
                f"launcher={payload['launcher'] or 'bulunamadı'}"
            )

        def finish_error(message: str) -> None:
            status_var.set("İnceleme başarısız.")
            messagebox.showerror("Paket inceleme", message, parent=dialog)

        def selected_candidate() -> PackageCommandCandidate | None:
            selection = tree.selection()
            if not selection:
                messagebox.showwarning(
                    "Komut seçilmedi",
                    "Önce listeden bir komut seç.",
                    parent=dialog,
                )
                return None
            return candidates_by_item.get(selection[0])

        def materialize_candidate(
            candidate: PackageCommandCandidate,
        ) -> FlowStep | None:
            data_uri = ""
            if candidate.kind == "open_uri":
                default_uri = f"{candidate.uri_scheme}://"
                data_uri = simpledialog.askstring(
                    "Deep link URI",
                    "Çalıştırılacak tam URI'yi yazın:",
                    initialvalue=default_uri,
                    parent=dialog,
                ) or ""
                if not data_uri:
                    return None

            wait_after = simpledialog.askfloat(
                "Komut sonrası bekleme",
                "Komuttan sonra kaç saniye beklensin?",
                initialvalue=2.0 if candidate.kind in {"launch_package", "launch_activity", "open_uri"} else 0.8,
                minvalue=0,
                maxvalue=3600,
                parent=dialog,
            )
            if wait_after is None:
                return None

            return FlowStep(
                action=candidate.kind,
                name=candidate.label,
                package=candidate.package,
                component=candidate.component,
                intent_action=candidate.intent_action,
                data_uri=data_uri,
                wait_after=float(wait_after),
            )

        def test_selected() -> None:
            candidate = selected_candidate()
            if candidate is None:
                return
            step = materialize_candidate(candidate)
            if step is None:
                return
            status_var.set(f"Test ediliyor: {candidate.label}")

            def worker() -> None:
                try:
                    self.execute_step(step, self.adb(), allow_wait=False)
                    self.ui_queue.put(
                        (
                            "package_command_test",
                            {
                                "callback": finish_test,
                                "success": True,
                                "message": candidate.label,
                            },
                        )
                    )
                except Exception as exc:
                    self.ui_queue.put(
                        (
                            "package_command_test",
                            {
                                "callback": finish_test,
                                "success": False,
                                "message": str(exc),
                            },
                        )
                    )

            threading.Thread(target=worker, daemon=True).start()

        def finish_test(success: bool, message: str) -> None:
            if success:
                status_var.set(f"Test başarılı: {message}")
                self.log(f"Ekransız komut testi başarılı: {message}")
            else:
                status_var.set("Komut testi başarısız.")
                messagebox.showerror(
                    "Komut testi",
                    message,
                    parent=dialog,
                )

        def add_selected() -> None:
            candidate = selected_candidate()
            if candidate is None:
                return
            step = materialize_candidate(candidate)
            if step is None:
                return
            if replace_index is None:
                self.steps.append(step)
                target_index = len(self.steps) - 1
                self.log(f"Ekransız komut adımı eklendi: {step.name}")
            else:
                old_name = self.steps[replace_index].name
                self.steps[replace_index] = step
                target_index = replace_index
                self.log(
                    f"Adım komuta çevrildi: {old_name} → {step.name}"
                )
            self.refresh_step_tree()
            self.step_tree.selection_set(str(target_index))
            self.step_tree.see(str(target_index))
            status_var.set(f"Akış güncellendi: {step.name}")

        filter_var.trace_add("write", rebuild)
        tree.bind("<Double-1>", lambda _event: test_selected())
        load_packages()
        if package_var.get():
            inspect_package()

    def add_launch_package_step(self) -> None:
        self.choose_package("Paket seç", "Paket Açma Adımı Ekle", self._append_launch_step)

    def add_open_storage_step(self) -> None:
        self.choose_package(
            "Depolaması açılacak paket",
            "Storage & cache Adımı Ekle",
            self._append_open_storage_step,
        )

    def _append_open_storage_step(self, package: str) -> None:
        wait_after = simpledialog.askfloat(
            "Storage & cache",
            "Depolama ekranı açıldıktan sonra kaç saniye beklensin?",
            initialvalue=2.0,
            minvalue=0,
            maxvalue=120,
            parent=self.root,
        )
        if wait_after is None:
            return
        self.steps.append(
            FlowStep(
                action="open_app_storage",
                name=f"Depolamayı aç: {package}",
                package=package,
                wait_after=float(wait_after),
            )
        )
        self.refresh_step_tree()

    def add_clear_data_step(self) -> None:
        self.choose_package(
            "Verisi temizlenecek paket",
            "Veri Temizleme Adımı Ekle",
            self._append_clear_data_step,
        )

    def _append_clear_data_step(self, package: str) -> None:
        wait_after = simpledialog.askfloat(
            "Paket verisini temizle",
            "Veri temizlendikten sonra kaç saniye beklensin?",
            initialvalue=1.5,
            minvalue=0,
            maxvalue=120,
            parent=self.root,
        )
        if wait_after is None:
            return
        self.steps.append(
            FlowStep(
                action="clear_app_data",
                name=f"Veriyi temizle: {package}",
                package=package,
                wait_after=float(wait_after),
            )
        )
        self.refresh_step_tree()

    def _append_launch_step(self, package: str) -> None:
        wait_after = simpledialog.askfloat(
            "Paket aç",
            "Uygulama açıldıktan sonra kaç saniye beklensin?",
            initialvalue=3.0,
            minvalue=0,
            maxvalue=300,
        )
        if wait_after is None:
            return
        self.steps.append(
            FlowStep(
                action="launch_package",
                name=f"Paketi aç: {package}",
                package=package,
                wait_after=float(wait_after),
            )
        )
        self.refresh_step_tree()

    def target_label(self, step: FlowStep) -> str:
        if step.action in {
            "launch_package",
            "force_stop_package",
            "open_app_details",
            "open_app_storage",
            "clear_app_data",
        }:
            return step.package
        if step.action == "launch_activity":
            return step.component or step.package
        if step.action == "send_broadcast":
            return f"{step.intent_action} → {step.component or 'implicit'}"
        if step.action == "open_uri":
            return step.data_uri
        if step.action == "wait":
            return ""
        if step.action == "keyevent":
            return step.text
        if step.action == "wait_image_tap":
            return (
                f"Görsel bölge ({step.region_x},{step.region_y},"
                f"{step.region_w}×{step.region_h}) ≥ {step.similarity:.2f}"
            )
        if step.resource_id:
            return step.resource_id
        if step.text:
            return step.text
        if step.content_desc:
            return step.content_desc
        if step.action in {"tap", "long_press", "double_tap"} and not (
            step.resource_id or step.text or step.content_desc or step.class_name
        ):
            return f"Kesin koordinat ({step.x},{step.y})"
        if step.action == "swipe":
            return f"({step.x},{step.y}) → ({step.x2},{step.y2})"
        return f"({step.x},{step.y})"

    def refresh_step_tree(self) -> None:
        for item in self.step_tree.get_children():
            self.step_tree.delete(item)
        labels = {
            "tap": "Tık",
            "wait_ui_tap": "Öğe bekle",
            "wait_image_tap": "Görsel bekle",
            "long_press": "Uzun bas",
            "double_tap": "Çift tık",
            "swipe": "Kaydır",
            "wait": "Bekle",
            "keyevent": "Tuş",
            "launch_package": "Paket aç",
            "force_stop_package": "Paket kapat",
            "launch_activity": "Activity",
            "send_broadcast": "Broadcast",
            "open_uri": "Deep link",
            "open_app_details": "Uyg. bilgisi",
            "open_app_storage": "Depolama",
            "clear_app_data": "Veri temizle",
        }
        for index, step in enumerate(self.steps, 1):
            self.step_tree.insert(
                "", END, iid=str(index - 1),
                values=(
                    index,
                    labels.get(step.action, step.action),
                    step.name,
                    self.target_label(step),
                    RUN_CONDITION_LABELS.get(
                        step.run_condition,
                        step.run_condition,
                    ),
                    f"{step.wait_after:g}s",
                )
            )

    def selected_step_index(self) -> int | None:
        selection = self.step_tree.selection()
        if not selection:
            return None
        return int(selection[0])

    def edit_selected_step(self, _event=None) -> None:
        index = self.selected_step_index()
        if index is None:
            return
        step = self.steps[index]

        dialog = Toplevel(self.root)
        dialog.title(f"Adımı düzenle — {index + 1}")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        name_var = StringVar(value=step.name)
        wait_var = DoubleVar(value=float(step.wait_after))
        duration_var = IntVar(value=int(step.duration_ms))
        x_var = StringVar(value="" if step.x is None else str(step.x))
        y_var = StringVar(value="" if step.y is None else str(step.y))
        x2_var = StringVar(value="" if step.x2 is None else str(step.x2))
        y2_var = StringVar(value="" if step.y2 is None else str(step.y2))
        package_var = StringVar(value=step.package)
        fallback_var = BooleanVar(value=bool(step.fallback_to_coordinate))
        timeout_var = DoubleVar(value=float(step.timeout_s))
        poll_var = DoubleVar(value=float(step.poll_interval))
        similarity_var = DoubleVar(value=float(step.similarity))
        component_var = StringVar(value=step.component)
        intent_action_var = StringVar(value=step.intent_action)
        data_uri_var = StringVar(value=step.data_uri)
        condition_var = StringVar(
            value=RUN_CONDITION_LABELS.get(
                step.run_condition,
                RUN_CONDITION_LABELS["always"],
            )
        )

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=BOTH, expand=True)

        ttk.Label(frame, text="Adım adı").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=name_var, width=48).grid(row=0, column=1, columnspan=3, sticky="ew", padx=(8, 0))

        ttk.Label(frame, text="İşlem").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(frame, text=step.action).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(frame, text="Çalışma koşulu").grid(
            row=1, column=2, sticky="e", padx=(12, 0), pady=(6, 0)
        )
        ttk.Combobox(
            frame,
            textvariable=condition_var,
            values=list(RUN_CONDITION_LABELS.values()),
            state="readonly",
            width=34,
        ).grid(row=1, column=3, sticky="w", padx=(8, 0), pady=(6, 0))

        ttk.Label(frame, text="Sonra bekle (sn)").grid(row=2, column=0, sticky="w")
        ttk.Spinbox(frame, from_=0, to=86400, increment=0.1, textvariable=wait_var, width=12).grid(
            row=2, column=1, sticky="w", padx=(8, 0)
        )

        ttk.Label(frame, text="İşlem süresi (ms)").grid(row=3, column=0, sticky="w")
        ttk.Spinbox(frame, from_=50, to=60000, increment=50, textvariable=duration_var, width=12).grid(
            row=3, column=1, sticky="w", padx=(8, 0)
        )

        ttk.Label(frame, text="Paket").grid(row=4, column=0, sticky="w")
        ttk.Entry(frame, textvariable=package_var, width=40).grid(
            row=4, column=1, columnspan=3, sticky="ew", padx=(8, 0)
        )

        command_frame = ttk.LabelFrame(frame, text="Ekransız komut bilgileri", padding=8)
        command_frame.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Label(command_frame, text="Component").grid(row=0, column=0, sticky="w")
        ttk.Entry(command_frame, textvariable=component_var, width=46).grid(
            row=0, column=1, sticky="ew", padx=(8, 0)
        )
        ttk.Label(command_frame, text="Intent action").grid(row=1, column=0, sticky="w")
        ttk.Entry(command_frame, textvariable=intent_action_var, width=46).grid(
            row=1, column=1, sticky="ew", padx=(8, 0)
        )
        ttk.Label(command_frame, text="URI").grid(row=2, column=0, sticky="w")
        ttk.Entry(command_frame, textvariable=data_uri_var, width=46).grid(
            row=2, column=1, sticky="ew", padx=(8, 0)
        )

        ttk.Label(frame, text="En fazla bekle (sn)").grid(row=6, column=0, sticky="w")
        ttk.Spinbox(frame, from_=0.1, to=86400, increment=0.5, textvariable=timeout_var, width=12).grid(
            row=6, column=1, sticky="w", padx=(8, 0)
        )
        ttk.Label(frame, text="Kontrol aralığı (sn)").grid(row=6, column=2, sticky="w", padx=(12, 0))
        ttk.Spinbox(frame, from_=0.2, to=60, increment=0.1, textvariable=poll_var, width=12).grid(
            row=6, column=3, sticky="w", padx=(8, 0)
        )

        ttk.Label(frame, text="Görsel benzerlik").grid(row=7, column=0, sticky="w")
        ttk.Spinbox(frame, from_=0.5, to=0.999, increment=0.01, textvariable=similarity_var, width=12).grid(
            row=7, column=1, sticky="w", padx=(8, 0)
        )

        coordinate_frame = ttk.LabelFrame(frame, text="Nox koordinatları", padding=8)
        coordinate_frame.grid(row=8, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Label(coordinate_frame, text="X").grid(row=0, column=0)
        ttk.Entry(coordinate_frame, textvariable=x_var, width=9).grid(row=0, column=1, padx=(4, 12))
        ttk.Label(coordinate_frame, text="Y").grid(row=0, column=2)
        ttk.Entry(coordinate_frame, textvariable=y_var, width=9).grid(row=0, column=3, padx=(4, 12))
        ttk.Label(coordinate_frame, text="X2").grid(row=0, column=4)
        ttk.Entry(coordinate_frame, textvariable=x2_var, width=9).grid(row=0, column=5, padx=(4, 12))
        ttk.Label(coordinate_frame, text="Y2").grid(row=0, column=6)
        ttk.Entry(coordinate_frame, textvariable=y2_var, width=9).grid(row=0, column=7, padx=(4, 0))

        ttk.Checkbutton(
            frame,
            text="UI öğesi bulunamazsa koordinatı kullan",
            variable=fallback_var,
        ).grid(row=9, column=0, columnspan=4, sticky="w", pady=(8, 0))

        selector = step.resource_id or step.text or step.content_desc or step.class_name
        ttk.Label(
            frame,
            text=f"Seçici: {selector if selector else 'Yok — kesin koordinat adımı'}",
            wraplength=510,
        ).grid(row=10, column=0, columnspan=4, sticky="w", pady=(6, 0))

        def parse_optional_int(value: str, label: str) -> int | None:
            value = value.strip()
            if not value:
                return None
            try:
                result = int(value)
            except ValueError:
                raise ValueError(f"{label} tam sayı olmalı.")
            if result < 0:
                raise ValueError(f"{label} negatif olamaz.")
            return result

        def save_changes() -> None:
            try:
                wait_after = float(wait_var.get())
                duration_ms = int(duration_var.get())
                timeout_s = float(timeout_var.get())
                poll_interval = float(poll_var.get())
                similarity = float(similarity_var.get())
                if wait_after < 0:
                    raise ValueError("Bekleme süresi negatif olamaz.")
                if duration_ms < 1:
                    raise ValueError("İşlem süresi en az 1 ms olmalı.")
                if timeout_s <= 0 or poll_interval <= 0:
                    raise ValueError("Zaman aşımı ve kontrol aralığı pozitif olmalı.")
                if not 0.5 <= similarity <= 0.999:
                    raise ValueError("Benzerlik 0.5 ile 0.999 arasında olmalı.")
                new_x = parse_optional_int(x_var.get(), "X")
                new_y = parse_optional_int(y_var.get(), "Y")
                new_x2 = parse_optional_int(x2_var.get(), "X2")
                new_y2 = parse_optional_int(y2_var.get(), "Y2")
            except Exception as exc:
                messagebox.showerror("Geçersiz değer", str(exc), parent=dialog)
                return

            step.name = name_var.get().strip() or step.name
            step.wait_after = wait_after
            step.duration_ms = duration_ms
            step.package = package_var.get().strip()
            step.x = new_x
            step.y = new_y
            step.x2 = new_x2
            step.y2 = new_y2
            step.fallback_to_coordinate = bool(fallback_var.get())
            step.timeout_s = timeout_s
            step.poll_interval = poll_interval
            step.similarity = similarity
            step.component = component_var.get().strip()
            step.intent_action = intent_action_var.get().strip()
            step.data_uri = data_uri_var.get().strip()
            step.run_condition = RUN_CONDITION_VALUES.get(
                condition_var.get(),
                "always",
            )
            self.refresh_step_tree()
            self.step_tree.selection_set(str(index))
            self.step_tree.see(str(index))
            self.log(f"Adım düzenlendi: {step.name}")
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.grid(row=11, column=0, columnspan=4, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="İptal", command=dialog.destroy).pack(side=RIGHT)
        ttk.Button(buttons, text="Kaydet", command=save_changes).pack(side=RIGHT, padx=(0, 8))

        dialog.bind("<Return>", lambda _e: save_changes())
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.wait_window(dialog)

    def move_step(self, delta: int) -> None:
        index = self.selected_step_index()
        if index is None:
            return
        target = index + delta
        if not (0 <= target < len(self.steps)):
            return
        self.steps[index], self.steps[target] = self.steps[target], self.steps[index]
        self.refresh_step_tree()
        self.step_tree.selection_set(str(target))
        self.step_tree.see(str(target))

    def delete_step(self) -> None:
        index = self.selected_step_index()
        if index is None:
            return
        del self.steps[index]
        self.refresh_step_tree()

    def clear_steps(self) -> None:
        if self.steps and not messagebox.askyesno("Tümünü temizle", "Bütün adımlar silinsin mi?"):
            return
        self.steps.clear()
        self.refresh_step_tree()

    def new_flow(self) -> None:
        if self.steps and not messagebox.askyesno("Yeni akış", "Mevcut adımlar temizlensin mi?"):
            return
        self.steps.clear()
        self.flow_id = uuid.uuid4().hex
        self.flow_name_var.set("Yeni Akış")
        self.refresh_step_tree()

    def save_flow(self) -> None:
        if not self.steps:
            messagebox.showwarning("Akış boş", "Kaydedilecek adım yok.")
            return
        suggested = re.sub(r"[^A-Za-z0-9ğüşöçıİĞÜŞÖÇ _-]", "_", self.flow_name_var.get()).strip() or "akis"
        path = filedialog.asksaveasfilename(
            title="Akışı kaydet",
            initialdir=FLOWS_DIR,
            initialfile=f"{suggested}.noxflow.json",
            defaultextension=".json",
            filetypes=[("NoxFlow", "*.noxflow.json"), ("JSON", "*.json")],
        )
        if not path:
            return
        payload = {
            "version": 2,
            "flow_id": self.flow_id,
            "name": self.flow_name_var.get().strip(),
            "device": self.device_var.get().strip(),
            "steps": [asdict(step) for step in self.steps],
        }
        atomic_write_json(Path(path), payload)
        self.log(f"Akış kaydedildi: {path}")

    def load_flow(self) -> None:
        path = filedialog.askopenfilename(
            title="Akış aç",
            initialdir=FLOWS_DIR,
            filetypes=[("NoxFlow", "*.noxflow.json"), ("JSON", "*.json")],
        )
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.steps = [FlowStep(**raw) for raw in payload.get("steps", [])]
            self.flow_id = payload.get("flow_id") or uuid.uuid4().hex
            self.flow_name_var.set(payload.get("name", Path(path).stem))
            saved_device = payload.get("device", "")
            if saved_device:
                self.device_var.set(saved_device)
            self.refresh_step_tree()
            self.log(f"Akış açıldı: {path}")
        except Exception as exc:
            messagebox.showerror("Akış açılamadı", str(exc))

    def test_selected_step(self) -> None:
        index = self.selected_step_index()
        if index is None:
            messagebox.showwarning("Adım seçilmedi", "Önce test edilecek adımı seç.")
            return
        if not self.validate_device():
            return
        step = self.steps[index]
        self.set_status(f"Test: {step.name}")
        threading.Thread(target=self._test_step_worker, args=(step,), daemon=True).start()

    def _test_step_worker(self, step: FlowStep) -> None:
        try:
            self.execute_step(step, self.adb(), allow_wait=True)
            self.queue_status("Adım testi tamamlandı.")
            self.queue_log(f"Adım testi başarılı: {step.name}")
        except Exception as exc:
            self.queue_status("Adım testi başarısız.")
            self.queue_log(f"Adım testi hatası — {step.name}: {exc}")

    def resolve_step_point(self, step: FlowStep, client: AdbClient) -> tuple[int, int]:
        has_selector = bool(step.resource_id or step.text or step.content_desc or step.class_name)
        if has_selector:
            with tempfile.TemporaryDirectory() as tmpdir:
                xml_path = Path(tmpdir) / "window.xml"
                try:
                    client.dump_ui(xml_path)
                    nodes = parse_ui_xml(xml_path)
                except Exception:
                    nodes = []

            candidates = nodes
            if step.package:
                package_matches = [n for n in candidates if n.package == step.package]
                if package_matches:
                    candidates = package_matches
            if step.resource_id:
                exact = [n for n in candidates if n.resource_id == step.resource_id]
                if exact:
                    candidates = exact
                else:
                    candidates = []
            if step.text and candidates:
                wanted = step.text.casefold()
                exact = [n for n in candidates if n.text.casefold() == wanted]
                if exact:
                    candidates = exact
                else:
                    contains = [n for n in candidates if wanted in n.text.casefold()]
                    candidates = contains
            if step.content_desc and candidates:
                exact = [n for n in candidates if n.content_desc == step.content_desc]
                if exact:
                    candidates = exact
            if step.class_name and candidates:
                exact = [n for n in candidates if n.class_name == step.class_name]
                if exact:
                    candidates = exact

            candidates = [n for n in candidates if n.enabled]
            if candidates:
                candidates.sort(key=lambda n: (not n.clickable, n.area))
                return candidates[0].target_center

        if step.fallback_to_coordinate and step.x is not None and step.y is not None:
            return int(step.x), int(step.y)

        raise AdbError(f"Hedef öğe bulunamadı: {step.name}")

    def ensure_package(self, step: FlowStep, client: AdbClient) -> None:
        if not self.auto_launch_package_var.get() or not step.package:
            return

        current_package = client.current_package()
        current_component = client.current_component()

        # Hazır UI komutu tanıtılırken hangi activity'de görüldüyse önce o ekranı
        # doğrudan açmayı deneriz. Export edilmemişse normal paket açmaya düşer.
        if (
            step.action == "wait_ui_tap"
            and step.component
            and current_component != step.component
        ):
            try:
                self.queue_log(
                    f"Hazır UI komutunun activity'si açılıyor: {step.component}"
                )
                client.start_activity(step.component)
                time.sleep(1.0)
                return
            except Exception as exc:
                self.queue_log(
                    f"Activity doğrudan açılamadı; paket açmaya geçiliyor: {exc}"
                )

        if current_package != step.package:
            self.queue_log(f"Paket aktif değil; açılıyor: {step.package}")
            client.launch_package(step.package)
            time.sleep(1.5)

    @staticmethod
    def image_similarity(left: Image.Image, right: Image.Image) -> float:
        left = left.convert("RGB")
        right = right.convert("RGB")
        if left.size != right.size:
            right = right.resize(left.size, Image.Resampling.BILINEAR)
        diff = ImageChops.difference(left, right)
        stat = ImageStat.Stat(diff)
        mean_difference = sum(stat.mean) / max(1, len(stat.mean))
        return max(0.0, min(1.0, 1.0 - (mean_difference / 255.0)))

    def wait_for_ui_target(self, step: FlowStep, client: AdbClient) -> tuple[int, int]:
        deadline = time.monotonic() + max(0.1, step.timeout_s)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.runner_stop.is_set():
                raise InterruptedError
            try:
                return self.resolve_step_point(step, client)
            except Exception as exc:
                last_error = exc
            self.interruptible_wait(max(0.2, step.poll_interval))
        raise AdbError(f"Öğe zamanında görünmedi: {step.name}. Son hata: {last_error}")

    def wait_for_visual_target(self, step: FlowStep, client: AdbClient) -> tuple[int, int]:
        if not step.template_png_base64:
            raise AdbError("Görsel şablon eksik.")
        if None in (step.region_x, step.region_y, step.region_w, step.region_h, step.x, step.y):
            raise AdbError("Görsel bölge veya tıklama koordinatı eksik.")

        try:
            template_bytes = base64.b64decode(step.template_png_base64)
            template = Image.open(io.BytesIO(template_bytes)).convert("RGB")
        except Exception as exc:
            raise AdbError(f"Görsel şablon açılamadı: {exc}")

        deadline = time.monotonic() + max(0.1, step.timeout_s)
        best = 0.0
        with tempfile.TemporaryDirectory() as tmpdir:
            screen_path = Path(tmpdir) / "visual_wait.png"
            while time.monotonic() < deadline:
                if self.runner_stop.is_set():
                    raise InterruptedError
                client.capture_screen(screen_path)
                current = Image.open(screen_path).convert("RGB")
                rx = int(step.region_x)
                ry = int(step.region_y)
                rw = int(step.region_w)
                rh = int(step.region_h)
                region = current.crop((rx, ry, rx + rw, ry + rh))
                score = self.image_similarity(template, region)
                best = max(best, score)
                if score >= step.similarity:
                    self.queue_log(
                        f"Görsel bulundu — {step.name}: benzerlik {score:.3f}"
                    )
                    return int(step.x), int(step.y)
                self.interruptible_wait(max(0.2, step.poll_interval))

        raise AdbError(
            f"Görsel zamanında bulunamadı: {step.name}. "
            f"En yüksek benzerlik {best:.3f}, gereken {step.similarity:.3f}"
        )

    def execute_step(self, step: FlowStep, client: AdbClient, allow_wait: bool = True) -> None:
        if not step.enabled:
            return

        if step.action == "wait":
            if allow_wait:
                self.interruptible_wait(step.wait_after)
            return

        if step.action == "launch_package":
            client.launch_package(step.package)
        elif step.action == "force_stop_package":
            client.force_stop(step.package)
        elif step.action == "launch_activity":
            client.start_activity(
                step.component,
                step.intent_action,
                step.data_uri,
            )
        elif step.action == "send_broadcast":
            client.send_broadcast(
                step.intent_action,
                step.component,
            )
        elif step.action == "open_uri":
            client.open_uri(step.data_uri, step.package)
        elif step.action == "open_app_details":
            client.open_app_details(step.package)
        elif step.action == "open_app_storage":
            client.open_app_storage(step.package)
        elif step.action == "clear_app_data":
            client.clear_app_data(step.package)
        elif step.action == "keyevent":
            client.keyevent(step.text)
        elif step.action == "swipe":
            if None in (step.x, step.y, step.x2, step.y2):
                raise AdbError("Kaydırma koordinatları eksik.")
            client.swipe(int(step.x), int(step.y), int(step.x2), int(step.y2), int(step.duration_ms))
        elif step.action in {"tap", "wait_ui_tap", "wait_image_tap", "long_press", "double_tap"}:
            self.ensure_package(step, client)
            if step.action == "wait_ui_tap":
                x, y = self.wait_for_ui_target(step, client)
                client.tap(x, y)
            elif step.action == "wait_image_tap":
                x, y = self.wait_for_visual_target(step, client)
                client.tap(x, y)
            else:
                x, y = self.resolve_step_point(step, client)
                if step.action == "tap":
                    client.tap(x, y)
                elif step.action == "long_press":
                    client.long_press(x, y, int(step.duration_ms))
                else:
                    client.double_tap(x, y)
        else:
            raise AdbError(f"Bilinmeyen işlem: {step.action}")

        if allow_wait and step.wait_after > 0:
            self.interruptible_wait(step.wait_after)

    def interruptible_wait(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self.runner_stop.is_set():
                raise InterruptedError("Akış kullanıcı tarafından durduruldu.")
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def start_flow(self) -> None:
        self.start_flow_at(0)

    def start_flow_from_selected(self) -> None:
        index = self.selected_step_index()
        if index is None:
            messagebox.showwarning(
                "Adım seçilmedi",
                "Devam edilecek başlangıç adımını akış listesinden seç.",
            )
            return
        self.start_flow_at(index)

    def start_flow_at(self, start_index: int) -> None:
        if self.recording_active:
            self.stop_bulk_recording()
        if not self.steps:
            messagebox.showwarning("Akış boş", "Önce birkaç adım ekle.")
            return
        if not self.validate_device():
            return
        if self.runner_thread and self.runner_thread.is_alive():
            return
        try:
            proxy_profile = self.proxy_profile()
        except Exception as exc:
            messagebox.showerror("Proxy ayarı", str(exc))
            return
        if self.background_run_var.get() and self.minimize_nox_var.get():
            count = minimize_noxplayer_windows()
            self.log(f"Arka plan için {count} NoxPlayer penceresi küçültüldü.")
        if not (0 <= start_index < len(self.steps)):
            messagebox.showwarning(
                "Başlangıç adımı",
                "Seçilen başlangıç adımı akış aralığında değil.",
            )
            return
        self.runner_stop.clear()
        self.clone_cycle_stop.clear()
        self.run_btn.configure(state="disabled")
        self.start_selected_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.log(
            f"Akış {start_index + 1}. adımdan başlatılıyor; "
            f"önceki {start_index} adım oynatılmayacak."
        )
        self.runner_thread = threading.Thread(
            target=self._run_flow_worker,
            args=(proxy_profile, start_index),
            daemon=True,
        )
        self.runner_thread.start()

    def _run_flow_worker(
        self,
        proxy_profile: dict[str, Any],
        start_index: int,
    ) -> None:
        client = self.adb()
        repeat = max(0, int(self.repeat_var.get()))
        try:
            self.wait_for_certificate_worker(client.serial, timeout_s=45.0)
            result = self.execute_flow_steps_for_client(
                client,
                start_index=start_index,
                repeat=repeat,
                apply_proxy=proxy_profile,
                label="Normal akış",
            )
            message = "Akış durduruldu." if result == "durduruldu" else "Akış tamamlandı."
        except InterruptedError:
            message = "Akış durduruldu."
        except Exception as exc:
            self.queue_log(f"Akış hatası: {exc}")
            message = "Akış hata ile durdu."
        self.ui_queue.put(("runner_done", message))

    def stop_flow(self) -> None:
        self.runner_stop.set()
        self.set_status("Durduruluyor…")

    def on_close(self) -> None:
        if self.recording_active:
            self.stop_bulk_recording()
        if self.ui_teach_active:
            self.stop_ui_teaching_session()
        self.clone_cycle_stop.set()
        self.runner_stop.set()
        settings = {
            "adb_path": self.adb_path_var.get().strip(),
            "device": self.device_var.get().strip(),
            "proxy_enabled": bool(self.proxy_enabled_var.get()),
            "proxy_mode": self.proxy_mode_var.get().strip(),
            "proxy_host": self.proxy_host_var.get().strip(),
            "proxy_port": int(self.proxy_port_var.get()),
            "auto_proxy": bool(self.auto_proxy_var.get()),
            "auto_start_charles": bool(self.auto_start_charles_var.get()),
            "target_only": bool(self.target_only_var.get()),
            "target_host": self.target_host_var.get().strip(),
            "target_path": self.target_path_var.get().strip(),
            "gate_port": int(self.gate_port_var.get()),
            "record_auto_wait": bool(self.record_auto_wait_var.get()),
            "record_visual": bool(self.record_visual_var.get()),
            "record_refresh": float(self.record_refresh_var.get()),
            "record_wait_threshold": float(self.record_wait_threshold_var.get()),
            "record_long_press_ms": int(self.record_long_press_ms_var.get()),
            "background_run": bool(self.background_run_var.get()),
            "minimize_nox": bool(self.minimize_nox_var.get()),
            "ui_teach_interval": float(self.ui_teach_interval_var.get()),
            "auto_certificate": True,
            "certificate_source": str(FIXED_CERTIFICATE_SOURCE),
            "certificate_android_path": self.certificate_android_path_var.get().strip(),
            "certificate_importer_package": self.certificate_importer_package_var.get().strip(),
            "noxconsole_path": self.noxconsole_path_var.get().strip(),
            "clone_template": self.clone_template_var.get().strip(),
            "clone_prefix": self.clone_prefix_var.get().strip(),
            "clone_runs": int(self.clone_runs_var.get()),
            "clone_count": int(self.clone_count_var.get()),
            "clone_copy_timeout": int(self.clone_copy_timeout_var.get()),
            "clone_boot_timeout": int(self.clone_boot_timeout_var.get()),
            "clone_cleanup": bool(self.clone_cleanup_var.get()),
            "clone_force_basic": bool(self.clone_force_basic_var.get()),
            "clone_resolution": self.clone_resolution_var.get().strip(),
            "clone_cpu": int(self.clone_cpu_var.get()),
            "clone_memory": int(self.clone_memory_var.get()),
        }
        try:
            atomic_write_json(SETTINGS_FILE, settings)
        except Exception:
            pass
        try:
            self.selective_gate.stop()
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    root = Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    app = NoxFlowApp(root)
    for tab_name in ("proxy_tab", "certificate_tab", "clone_tab"):
        tab = getattr(app, tab_name, None)
        if tab is not None:
            try:
                app.main_notebook.forget(tab)
            except Exception:
                pass
    root.mainloop()


if __name__ == "__main__":
    main()
