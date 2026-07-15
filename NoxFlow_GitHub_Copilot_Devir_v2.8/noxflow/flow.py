from __future__ import annotations

from pathlib import Path
import json
import tempfile
import time
import threading

from .models import FlowDefinition, FlowStep
from .adb import AdbClient, AdbError, parse_ui
from .util import StopRequested, interruptible_sleep


def load_flow(path: Path) -> FlowDefinition:
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return FlowDefinition(
        raw.get("name", path.stem),
        [
            FlowStep.from_dict(item)
            for item in raw.get("steps", [])
        ],
        raw.get("flow_id") or path.stem,
    )


class FlowRunner:
    def __init__(
        self,
        flow: FlowDefinition,
        stop_event: threading.Event,
        log,
    ):
        self.flow = flow
        self.stop_event = stop_event
        self.log = log

    def sleep(self, seconds: float) -> None:
        interruptible_sleep(
            seconds,
            self.stop_event,
            0.1,
        )

    def resolve(
        self,
        client: AdbClient,
        step: FlowStep,
    ) -> tuple[int, int]:
        if (
            step.resource_id
            or step.text
            or step.content_desc
            or step.class_name
        ):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "ui.xml"
                try:
                    client.dump_ui(path)
                    nodes = parse_ui(path)
                except Exception:
                    nodes = []

            candidates = nodes
            if step.package:
                package_nodes = [
                    node
                    for node in candidates
                    if node.package == step.package
                ]
                if package_nodes:
                    candidates = package_nodes

            if step.resource_id:
                candidates = [
                    node
                    for node in candidates
                    if node.resource_id == step.resource_id
                ]

            if step.text and candidates:
                wanted = step.text.casefold()
                exact = [
                    node
                    for node in candidates
                    if node.text.casefold() == wanted
                ]
                candidates = exact or [
                    node
                    for node in candidates
                    if wanted in node.text.casefold()
                ]

            if step.content_desc and candidates:
                exact = [
                    node
                    for node in candidates
                    if node.content_desc == step.content_desc
                ]
                if exact:
                    candidates = exact

            if step.class_name and candidates:
                exact = [
                    node
                    for node in candidates
                    if node.class_name == step.class_name
                ]
                if exact:
                    candidates = exact

            candidates = [
                node
                for node in candidates
                if node.enabled
            ]
            if candidates:
                candidates.sort(
                    key=lambda node: (
                        not node.clickable,
                        node.area,
                    )
                )
                return candidates[0].target_center

        if (
            step.fallback_to_coordinate
            and step.x is not None
            and step.y is not None
        ):
            return int(step.x), int(step.y)

        raise AdbError(
            f"Hedef bulunamadı: {step.name}"
        )

    def ensure_package(
        self,
        client: AdbClient,
        step: FlowStep,
    ) -> None:
        if not step.package:
            return

        current = client.current_package()
        if (
            step.action == "wait_ui_tap"
            and step.component
            and client.current_component() != step.component
        ):
            try:
                client.start_activity(step.component)
                self.sleep(1)
                return
            except Exception:
                pass

        if current != step.package:
            client.launch_package(step.package)
            self.sleep(1.5)

    def wait_ui(
        self,
        client: AdbClient,
        step: FlowStep,
    ) -> tuple[int, int]:
        deadline = (
            time.monotonic()
            + max(0.1, step.timeout_s)
        )
        last_error: Exception | None = None

        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                raise StopRequested(
                    "Akış durduruldu."
                )
            try:
                return self.resolve(client, step)
            except Exception as exc:
                last_error = exc
            self.sleep(
                max(0.2, step.poll_interval)
            )

        raise AdbError(
            f"Öğe zamanında görünmedi: "
            f"{step.name}: {last_error}"
        )

    def execute(
        self,
        client: AdbClient,
        step: FlowStep,
    ) -> None:
        if not step.enabled:
            return

        action = step.action
        if action == "wait":
            self.sleep(step.wait_after)
            return

        if action == "launch_package":
            client.launch_package(step.package)
        elif action == "force_stop_package":
            client.force_stop(step.package)
        elif action == "clear_app_data":
            client.clear_data(step.package)
        elif action == "launch_activity":
            client.start_activity(
                step.component,
                step.intent_action,
                step.data_uri,
            )
        elif action == "send_broadcast":
            client.broadcast(
                step.intent_action,
                step.component,
            )
        elif action == "open_uri":
            client.open_uri(
                step.data_uri,
                step.package,
            )
        elif action == "open_app_details":
            client.open_app_details(step.package)
        elif action == "open_app_storage":
            client.open_app_storage(step.package)
        elif action == "keyevent":
            client.keyevent(step.text)
        elif action == "swipe":
            client.swipe(
                int(step.x),
                int(step.y),
                int(step.x2),
                int(step.y2),
                int(step.duration_ms),
            )
        elif action in (
            "tap",
            "long_press",
            "double_tap",
            "wait_ui_tap",
            "wait_image_tap",
        ):
            self.ensure_package(client, step)

            if action == "wait_ui_tap":
                x, y = self.wait_ui(client, step)
            elif action == "wait_image_tap":
                from .visual import wait_visual

                x, y = wait_visual(
                    client,
                    step,
                    self.stop_event,
                    self.log,
                )
            else:
                x, y = self.resolve(client, step)

            if action in (
                "tap",
                "wait_ui_tap",
                "wait_image_tap",
            ):
                client.tap(x, y)
            elif action == "long_press":
                client.long_press(
                    x,
                    y,
                    step.duration_ms,
                )
            else:
                client.double_tap(x, y)
        else:
            raise AdbError(
                f"Bilinmeyen akış işlemi: {action}"
            )

        if step.wait_after > 0:
            self.sleep(step.wait_after)

    def run(
        self,
        client: AdbClient,
        repeats: int,
        label: str,
        max_seconds: float | None = None,
    ) -> None:
        once_run: set[str] = set()
        once_session: set[str] = set()
        iteration = 0
        started = time.monotonic()
        deadline = (
            started + max_seconds
            if max_seconds is not None
            else None
        )

        while not self.stop_event.is_set():
            if repeats > 0 and iteration >= repeats:
                break

            if (
                deadline is not None
                and iteration > 0
                and time.monotonic() >= deadline
            ):
                self.log(
                    f"{label}: kullanım süresi doldu; "
                    f"tamamlanan akış turu={iteration}"
                )
                break

            iteration += 1
            self.log(
                f"{label}: akış döngüsü "
                f"{iteration} başladı"
            )

            for index, step in enumerate(
                self.flow.steps,
                1,
            ):
                if self.stop_event.is_set():
                    raise StopRequested(
                        "Akış sırasında durdurma istendi."
                    )
                if not step.enabled:
                    continue
                if (
                    step.run_condition
                    == "once_per_flow_run"
                    and step.step_id in once_run
                ):
                    continue
                if (
                    step.run_condition
                    == "once_per_nox_session"
                    and step.step_id in once_session
                ):
                    continue

                self.log(
                    f"{label}: "
                    f"{index}/{len(self.flow.steps)} "
                    f"{step.name}"
                )
                self.execute(client, step)

                if (
                    step.run_condition
                    == "once_per_flow_run"
                ):
                    once_run.add(step.step_id)
                if (
                    step.run_condition
                    == "once_per_nox_session"
                ):
                    once_session.add(step.step_id)

            self.log(
                f"{label}: akış döngüsü "
                f"{iteration} tamamlandı"
            )

        if self.stop_event.is_set():
            raise StopRequested(
                "Akış kullanımı durduruldu."
            )

        elapsed = time.monotonic() - started
        self.log(
            f"{label}: çalışma kullanımı tamamlandı — "
            f"{iteration} tur, "
            f"{elapsed / 60:.1f} dakika"
        )
