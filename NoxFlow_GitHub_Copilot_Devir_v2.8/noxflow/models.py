from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
import uuid

@dataclass(slots=True)
class UiNode:
    package: str = ""
    text: str = ""
    resource_id: str = ""
    class_name: str = ""
    content_desc: str = ""
    clickable: bool = False
    enabled: bool = True
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    click_bounds: tuple[int, int, int, int] | None = None

    @property
    def area(self) -> int:
        x1, y1, x2, y2 = self.bounds
        return max(0, x2-x1) * max(0, y2-y1)

    @property
    def target_center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.click_bounds or self.bounds
        return ((x1+x2)//2, (y1+y2)//2)

@dataclass(slots=True)
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
    timeout_s: float = 30.0
    poll_interval: float = 0.8
    template_png_base64: str = ""
    region_x: int | None = None
    region_y: int | None = None
    region_w: int | None = None
    region_h: int | None = None
    similarity: float = 0.90
    component: str = ""
    intent_action: str = ""
    data_uri: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FlowStep":
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in raw.items() if k in known})

@dataclass(slots=True)
class FlowDefinition:
    name: str
    steps: list[FlowStep]
    flow_id: str = field(default_factory=lambda: uuid.uuid4().hex)

@dataclass(slots=True)
class NoxInstance:
    index: int | None
    name: str
    title: str
    running: bool = False
    pid: int | None = None
