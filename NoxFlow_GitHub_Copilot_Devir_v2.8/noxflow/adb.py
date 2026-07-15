from __future__ import annotations
from pathlib import Path
import os, re, shlex, subprocess, tempfile, time, xml.etree.ElementTree as ET
from .models import UiNode
from .util import StopRequested, interruptible_sleep

class AdbError(RuntimeError): pass

class AdbClient:
    def __init__(self, executable: str, serial: str = ''):
        self.executable = executable
        self.serial = serial

    def _base(self):
        command = [self.executable]
        if self.serial: command += ['-s', self.serial]
        return command

    def run(self, args: list[str], timeout: float = 30, binary: bool = False, check: bool = True):
        result = subprocess.run(
            self._base()+args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=not binary, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        if check and result.returncode != 0:
            out = result.stdout.decode(errors='replace') if binary else result.stdout
            err = result.stderr.decode(errors='replace') if binary else result.stderr
            raise AdbError((err or out or 'ADB komutu başarısız').strip())
        return result

    def shell(self, command: str, timeout: float = 30) -> str:
        return self.run(['shell', command], timeout=timeout).stdout.strip()

    def devices(self) -> list[str]:
        result = subprocess.run([self.executable, 'devices'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode != 0: raise AdbError((result.stderr or result.stdout).strip())
        return [p[0] for p in (line.split() for line in result.stdout.splitlines()[1:]) if len(p)>=2 and p[1]=='device']

    def boot_completed(self) -> bool:
        try: return self.shell('getprop sys.boot_completed', 8).strip() == '1'
        except Exception: return False

    def wait_boot(self, timeout_s: float, stop_event, log) -> None:
        deadline = time.monotonic()+timeout_s
        while time.monotonic()<deadline:
            if stop_event.is_set(): raise StopRequested()
            if self.boot_completed():
                self.shell('input keyevent 82', 8)  # unlock best effort
                return
            time.sleep(2)
        raise AdbError(f'Android {timeout_s:g} saniyede açılmadı: {self.serial}')

    def boot_id(self) -> str:
        value = self.shell('cat /proc/sys/kernel/random/boot_id', 10).lower()
        if value: return value
        return self.shell('cat /proc/uptime', 10).split()[0]

    def reverse(self, device_port: int, host_port: int):
        self.run(['reverse', f'tcp:{device_port}', f'tcp:{host_port}'], timeout=20)

    def set_proxy(self, host: str, port: int):
        expected=f'{host}:{port}'
        self.shell(f'settings put global http_proxy {shlex.quote(expected)}', 20)
        actual=self.shell('settings get global http_proxy', 10)
        if actual != expected: raise AdbError(f'Proxy doğrulanamadı: {actual!r}')

    def clear_proxy(self):
        self.shell('settings put global http_proxy :0', 15)

    def set_120hz_android(self, log):
        # Nox host renderer setting is separate; these expose 120 Hz inside Android.
        commands = [
            'settings put system peak_refresh_rate 120.0',
            'settings put system min_refresh_rate 120.0',
            'settings put system user_refresh_rate 120',
            'settings put system display_refresh_rate 120',
        ]
        for cmd in commands:
            try: self.shell(cmd, 8)
            except Exception as exc: log(f'Android 120 Hz yardımcı ayarı atlandı: {exc}')

    def keyevent(self, key: str): self.shell(f'input keyevent {shlex.quote(key)}', 15)
    def tap(self, x: int, y: int): self.shell(f'input tap {int(x)} {int(y)}', 15)
    def double_tap(self, x: int, y: int):
        self.tap(x,y); time.sleep(.08); self.tap(x,y)
    def long_press(self, x: int, y: int, duration_ms: int):
        self.shell(f'input swipe {int(x)} {int(y)} {int(x)} {int(y)} {int(duration_ms)}', 20)
    def swipe(self, x1:int,y1:int,x2:int,y2:int,duration_ms:int):
        self.shell(f'input swipe {x1} {y1} {x2} {y2} {duration_ms}', 20)

    @staticmethod
    def validate_package(package: str) -> str:
        if not re.fullmatch(r'[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+', package or ''):
            raise AdbError(f'Geçersiz paket: {package!r}')
        return package

    def current_package(self) -> str:
        text=self.shell('dumpsys window windows | grep -E "mCurrentFocus|mFocusedApp"', 15)
        m=re.search(r'([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+)/', text)
        return m.group(1) if m else ''

    def current_component(self) -> str:
        text=self.shell('dumpsys window windows | grep -E "mCurrentFocus|mFocusedApp"', 15)
        m=re.search(r'([A-Za-z0-9._]+)/(\.?[A-Za-z0-9._$]+)', text)
        return f'{m.group(1)}/{m.group(2)}' if m else ''

    def launch_package(self, package: str):
        package=self.validate_package(package)
        out=self.shell(f'monkey -p {shlex.quote(package)} -c android.intent.category.LAUNCHER 1', 30)
        if 'monkey aborted' in out.lower(): raise AdbError(out)

    def force_stop(self, package: str):
        self.shell(f'am force-stop {shlex.quote(self.validate_package(package))}', 20)

    def clear_data(self, package: str):
        out=self.shell(f'pm clear {shlex.quote(self.validate_package(package))}', 60)
        if 'Success' not in out: raise AdbError(f'Veri temizlenemedi: {out}')

    def start_activity(self, component: str, action: str='', uri: str=''):
        parts=['am','start','-W','-n',shlex.quote(component)]
        if action: parts += ['-a', shlex.quote(action)]
        if uri: parts += ['-d', shlex.quote(uri)]
        out=self.shell(' '.join(parts), 30)
        if 'error:' in out.lower() or 'exception' in out.lower(): raise AdbError(out)

    def broadcast(self, action: str, component: str=''):
        parts=['am','broadcast','-a',shlex.quote(action)]
        if component: parts += ['-n', shlex.quote(component)]
        out=self.shell(' '.join(parts), 30)
        if 'securityexception' in out.lower(): raise AdbError(out)

    def open_uri(self, uri: str, package: str=''):
        parts=['am','start','-W','-a','android.intent.action.VIEW','-d',shlex.quote(uri)]
        if package: parts += ['-p',shlex.quote(self.validate_package(package))]
        out=self.shell(' '.join(parts), 30)
        if 'error:' in out.lower() or 'unable to resolve' in out.lower(): raise AdbError(out)

    def open_app_details(self, package: str):
        self.shell(f'am start -a android.settings.APPLICATION_DETAILS_SETTINGS -d package:{shlex.quote(self.validate_package(package))}', 30)

    def open_app_storage(self, package: str):
        package=self.validate_package(package)
        out=self.shell(f'am start -a android.settings.INTERNAL_STORAGE_SETTINGS -d package:{shlex.quote(package)}', 30)
        if 'Error' in out: self.open_app_details(package)

    def push(self, source: Path, destination: str):
        parent=destination.rsplit('/',1)[0]
        self.shell(f'mkdir -p {shlex.quote(parent)}', 15)
        self.run(['push', str(source), destination], timeout=120)

    def package_installed(self, package: str) -> bool:
        result=self.run(['shell','pm','path',self.validate_package(package)], timeout=15, check=False)
        return result.returncode==0 and (result.stdout or '').startswith('package:')

    def dump_ui(self, output: Path):
        remote='/sdcard/window_dump.xml'
        self.shell(f'uiautomator dump {remote}', 25)
        self.run(['pull', remote, str(output)], timeout=25)

    def screenshot(self, output: Path):
        result=self.run(['exec-out','screencap','-p'], timeout=35, binary=True)
        output.write_bytes(result.stdout)


def parse_bounds(value: str):
    m=re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', value or '')
    return tuple(map(int,m.groups())) if m else None


def parse_ui(path: Path) -> list[UiNode]:
    nodes=[]
    root=ET.parse(path).getroot()
    def walk(elem, ancestor=None):
        if elem.tag=='node':
            bounds=parse_bounds(elem.attrib.get('bounds',''))
            if bounds:
                clickable=elem.attrib.get('clickable','false').lower()=='true'
                click_bounds=bounds if clickable else ancestor
                nodes.append(UiNode(
                    package=elem.attrib.get('package',''), text=elem.attrib.get('text',''),
                    resource_id=elem.attrib.get('resource-id',''), class_name=elem.attrib.get('class',''),
                    content_desc=elem.attrib.get('content-desc',''), clickable=clickable,
                    enabled=elem.attrib.get('enabled','true').lower()=='true', bounds=bounds,
                    click_bounds=click_bounds))
                ancestor=click_bounds
        for child in elem: walk(child, ancestor)
    walk(root)
    return nodes
