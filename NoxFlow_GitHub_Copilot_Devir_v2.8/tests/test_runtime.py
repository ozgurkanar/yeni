from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from noxflow import __version__
from noxflow.config import RuntimeConfig, EMBEDDED_CERTIFICATE, EMBEDDED_FLOW
from noxflow.flow import load_flow
from noxflow.nox import NoxConsole, parse_nox_list_output


class Tests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(__version__, "2.8")

    def test_embedded_resources(self):
        self.assertTrue(EMBEDDED_CERTIFICATE.is_file())
        self.assertTrue(EMBEDDED_FLOW.is_file())

    def test_uploaded_flow_is_embedded(self):
        original = json.loads((ROOT / "flows" / "Yeni_Akis_orijinal.noxflow.json").read_text(encoding="utf-8"))
        embedded = json.loads((ROOT / "flows" / "gomulu_akis.noxflow.json").read_text(encoding="utf-8"))
        self.assertEqual(len(original["steps"]), 10)
        self.assertEqual(embedded["steps"], original["steps"])
        self.assertEqual(embedded["lifecycle"]["clone_source"], "nox")
        self.assertEqual(embedded["lifecycle"]["clone_target"], "Nox_1")
        self.assertIn("kill_all_nox_processes", embedded["lifecycle"]["start"])
        self.assertIn("copy_clone_via_noxconsole", embedded["lifecycle"]["start"])
        self.assertIn("install_certificate", embedded["lifecycle"]["start"])

    def test_flow_loads(self):
        flow = load_flow(ROOT / "flows" / "gomulu_akis.noxflow.json")
        self.assertEqual(len(flow.steps), 10)

    def test_config_fresh_clone_policy(self):
        raw = json.loads((ROOT / "config" / "runtime.json").read_text(encoding="utf-8"))
        self.assertTrue(raw["cleanup_on_start"])
        self.assertTrue(raw["cleanup_on_stop"])
        self.assertFalse(raw["reuse_existing_clone_on_start"])
        self.assertFalse(raw["preserve_clone_on_stop"])
        self.assertEqual(raw["source_vm_name"], "nox")
        self.assertEqual(raw["working_clone_name"], "Nox_1")

    def test_nox_process_cleanup_exists(self):
        self.assertTrue(callable(getattr(NoxConsole, "kill_all_nox_processes", None)))
        names = NoxConsole.NOX_PROCESS_NAMES
        for expected in ("NoxPlayer.exe", "MultiPlayerManager.exe", "NoxVMHandle.exe", "NoxVMSVC.exe", "NoxHeadless.exe"):
            self.assertIn(expected, names)

    def test_real_noxconsole_clone_chain(self):
        source = (ROOT / "noxflow" / "nox.py").read_text(encoding="utf-8")
        self.assertIn('"copy",', source)
        self.assertIn('f"-name:{target}"', source)
        self.assertIn('f"-from:{source_ref}"', source)
        self.assertIn("def recreate_clone", source)
        self.assertIn("self.remove(clone_name)", source)
        self.assertNotIn("shutil.copytree", source)

    def test_orchestrator_order(self):
        source = (ROOT / "noxflow" / "orchestrator.py").read_text(encoding="utf-8")
        run = source[source.index("    def run(self) -> None:"):]
        positions = [
            run.index("self.source_instance"),
            run.index("self.ensure_clone_exists"),
            run.index("self.open_clone"),
            run.index("self.prepare_services"),
            run.index("self.prepare_clone_android"),
            run.index("self.play_flow"),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("self.nox.kill_all_nox_processes()", run)

    def test_source_is_never_launched(self):
        source = (ROOT / "noxflow" / "orchestrator.py").read_text(encoding="utf-8")
        self.assertIn("self.nox.launch(clone_name)", source)
        self.assertNotIn("self.nox.launch(source.name)", source)

    def test_parse_list(self):
        items = parse_nox_list_output("0,nox,NoxPlayer,0\n1,Nox_1,NoxPlayer1,4567\n")
        self.assertEqual(items[0].name, "nox")
        self.assertFalse(items[0].running)
        self.assertEqual(items[1].name, "Nox_1")
        self.assertTrue(items[1].running)


if __name__ == "__main__":
    unittest.main()
