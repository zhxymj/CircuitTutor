"""源封装复现与等价性核对回归。

关键不变量：
  - 复现件的焊盘几何与源 Eagle 封装逐属性相等（渲染后再独立读回比对）；
  - 库存扫描既能找到精确等价件，也能拒绝近似件——否则"库存无等价"只是假设；
  - 不支持的源几何必须报错，绝不用近似值顶替；
  - 写盘字节与被核对的文本逐字节一致（哈希可追溯）。
"""
from decimal import Decimal
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import eagle_footprint_repro as repro
from core.kicad_footprints import footprint_for_part
from core.pcb_real import load_footprint
from evaluation.external_reference_audit import ROOT

SOURCE_SCHEMATIC = ROOT / "sparkfun_qwiic_adapter" / "SparkFun Qwiic Adapter.sch"
CONNECTOR_LIBRARY = "SparkFun-Connectors"


def package_node(raw: bytes, library: str, package: str) -> ET.Element:
    schematic = ET.fromstring(raw).find("drawing/schematic")
    lib = next(x for x in schematic.findall("libraries/library") if x.get("name") == library)
    return next(x for x in lib.findall("packages/package") if x.get("name") == package)


def synthetic_package(inner: str) -> ET.Element:
    return ET.fromstring(f'<package name="SYN">{inner}</package>')


class ReproducedFootprintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = SOURCE_SCHEMATIC.read_bytes()
        cls.sha = hashlib.sha256(cls.raw).hexdigest()

    def render(self, package: str) -> tuple[list[dict], str]:
        pads = repro.parse_package_pads(package_node(self.raw, CONNECTOR_LIBRARY, package))
        text = repro.render_footprint(package, pads, source_library=CONNECTOR_LIBRARY,
                                      source_package=package, source_sha256=self.sha)
        return pads, text

    def test_reproduced_pads_match_source_exactly(self):
        for package, expected_count in (("1X04", 4), ("1X04_1MM_RA", 6)):
            with self.subTest(package=package):
                pads, text = self.render(package)
                self.assertEqual(len(pads), expected_count)
                result = repro.verify_pad_equivalence(pads, text)
                self.assertTrue(result["pad_geometry_exact"])
                self.assertEqual(result["matched_pads"], expected_count)
                self.assertEqual(result["unmatched_pads"], 0)
                self.assertEqual(result["unexpected_extra_pads"], [])
                self.assertTrue(all(row["matched"] and not row["mismatches"]
                                    for row in result["per_pad"]))

    def test_thru_hole_and_smd_kinds_are_preserved_separately(self):
        pads, _ = self.render("1X04")
        self.assertTrue(all(p["kind"] == "thru_hole" for p in pads))
        self.assertTrue(all(p["shape"] == "circle" for p in pads))
        self.assertTrue(all(p["drill_mm"] == Decimal("1.016") for p in pads))
        self.assertTrue(all(p["w_mm"] == Decimal("1.8796") for p in pads))
        smd, _ = self.render("1X04_1MM_RA")
        self.assertTrue(all(p["kind"] == "smd" for p in smd))
        self.assertTrue(all(p["drill_mm"] is None for p in smd))
        signal = [p for p in smd if p["name"] in {"1", "2", "3", "4"}]
        self.assertEqual(len(signal), 4)
        self.assertTrue(all(p["w_mm"] == Decimal("0.6") and p["h_mm"] == Decimal("1.35")
                            for p in signal))
        # 1mm 间距是这块板的物理事实，不能被四舍五入成 2.54mm 排针
        xs = sorted(p["x_mm"] for p in signal)
        self.assertEqual([str(v) for v in xs], ["-1.5", "-0.5", "0.5", "1.5"])

    def test_eagle_y_up_becomes_kicad_y_down(self):
        pads, text = self.render("1X04_1MM_RA")
        source = {p["name"]: p for p in pads}
        written = {p["name"]: p for p in repro.read_footprint_pads(text)}
        self.assertEqual(source["NC2"]["y_mm"], Decimal("-3.675"))
        self.assertEqual(written["NC2"]["y_mm"], Decimal("3.675"))
        self.assertEqual(written["NC2"]["x_mm"], source["NC2"]["x_mm"])
        self.assertEqual(written["1"]["rotation_deg"], source["1"]["rotation_deg"])

    def test_thru_hole_rotation_is_carried_through(self):
        pads, text = self.render("1X04")
        self.assertTrue(all(p["rotation_deg"] == Decimal(90) for p in pads))
        written = repro.read_footprint_pads(text)
        self.assertTrue(all(p["rotation_deg"] == Decimal(90) for p in written))

    def test_unsupported_source_geometry_is_refused_not_approximated(self):
        cases = {
            "圆角 SMD": '<smd name="1" x="0" y="0" dx="1" dy="1" layer="1" roundness="50"/>',
            "八角焊盘": '<pad name="1" x="0" y="0" drill="0.8" diameter="1.5" shape="octagon"/>',
            "长条焊盘": '<pad name="1" x="0" y="0" drill="0.8" diameter="1.5" shape="long"/>',
            "缺孔径": '<pad name="1" x="0" y="0" diameter="1.5"/>',
            "缺编号": '<smd name="" x="0" y="0" dx="1" dy="1" layer="1"/>',
            "内层 SMD": '<smd name="1" x="0" y="0" dx="1" dy="1" layer="2"/>',
            "重复编号": ('<smd name="1" x="0" y="0" dx="1" dy="1" layer="1"/>'
                         '<smd name="1" x="2" y="0" dx="1" dy="1" layer="1"/>'),
            "无焊盘": '<wire x1="0" y1="0" x2="1" y2="1" width="0.2" layer="21"/>',
        }
        for label, inner in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(repro.UnsupportedSourceGeometry):
                    repro.parse_package_pads(synthetic_package(inner))

    def test_bottom_side_smd_maps_to_back_copper(self):
        pads = repro.parse_package_pads(synthetic_package(
            '<smd name="1" x="0" y="0" dx="1" dy="2" layer="16"/>'))
        self.assertEqual(pads[0]["copper_layer"], "B.Cu")
        text = repro.render_footprint("SYN", pads, source_library="L", source_package="SYN",
                                      source_sha256=self.sha)
        self.assertIn('(layers "B.Cu" "B.Paste" "B.Mask")', text)
        self.assertTrue(repro.verify_pad_equivalence(pads, text)["pad_geometry_exact"])

    def test_pad_signature_is_origin_independent(self):
        pads, _ = self.render("1X04")
        shifted = [{**p, "x_mm": p["x_mm"] + Decimal("13.7"),
                    "y_mm": p["y_mm"] - Decimal("4.2")} for p in pads]
        self.assertEqual(repro.pad_geometry_signature(pads),
                         repro.pad_geometry_signature(shifted))

    def test_pad_signature_detects_a_hundredth_of_a_millimetre(self):
        pads, _ = self.render("1X04")
        near = [dict(p) for p in pads]
        near[0]["w_mm"] = near[0]["w_mm"] + Decimal("0.01")
        self.assertNotEqual(repro.pad_geometry_signature(pads),
                            repro.pad_geometry_signature(near))

    def test_stock_scan_finds_exact_match_and_rejects_near_miss(self):
        """正负对照：扫描器必须既能命中也能拒绝，否则"无等价件"没有意义。"""
        pads, text = self.render("1X04_1MM_RA")
        root = Path(tempfile.mkdtemp(prefix="stock-scan-"))
        try:
            lib = root / "FakeStock.pretty"
            lib.mkdir()
            (lib / "Exact.kicad_mod").write_bytes(
                text.replace('(footprint "1X04_1MM_RA"', '(footprint "Exact"', 1)
                     .replace('(property "Value" "1X04_1MM_RA"', '(property "Value" "Exact"', 1)
                     .encode("utf-8"))
            (lib / "NearMiss.kicad_mod").write_bytes(
                text.replace("(size 0.6 1.35)", "(size 0.6 1.36)")
                    .replace('(footprint "1X04_1MM_RA"', '(footprint "NearMiss"', 1)
                    .replace('(property "Value" "1X04_1MM_RA"', '(property "Value" "NearMiss"', 1)
                    .encode("utf-8"))
            # 焊盘数不同的文件应被廉价预筛跳过，不参与解析
            (lib / "DifferentCount.kicad_mod").write_bytes(
                text.replace('(footprint "1X04_1MM_RA"', '(footprint "DifferentCount"', 1)
                     .replace('(property "Value" "1X04_1MM_RA"',
                              '(property "Value" "DifferentCount"', 1)
                     .replace('  (pad "4" smd rect (at 1.5 0 0) (size 0.6 1.35) '
                              '(layers "F.Cu" "F.Paste" "F.Mask"))\n', "")
                    .encode("utf-8"))
            result = repro.scan_stock_equivalents(pads, root)
            self.assertEqual(result["files_scanned"], 3)
            self.assertTrue(result["exact_stock_equivalent_found"])
            self.assertEqual([m["name"] for m in result["exact_geometry_matches"]], ["Exact"])
            # 近似件被完整解析并比较过，只是不相等；焊盘数不同的件走廉价预筛
            self.assertEqual(result["files_fully_compared"], 2)
            self.assertEqual(result["files_rejected_by_pad_count_or_parse"], 1)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_stock_scan_reports_unavailable_root(self):
        pads, _ = self.render("1X04")
        with self.assertRaises(FileNotFoundError):
            repro.scan_stock_equivalents(pads, Path(tempfile.gettempdir()) / "no-such-fp-root")

    def test_reproduced_footprint_is_loadable_by_the_pcb_pipeline(self):
        root = Path(tempfile.mkdtemp(prefix="repro-lib-"))
        try:
            entries = []
            for package in ("1X04", "1X04_1MM_RA"):
                pads, text = self.render(package)
                entries.append({"library": repro.repro_library_name(CONNECTOR_LIBRARY),
                                "footprint_id": repro.repro_footprint_id(CONNECTOR_LIBRARY, package),
                                "source_library": CONNECTOR_LIBRARY,
                                "source_package": package, "text": text})
            written = repro.write_repro_library(root, entries)
            self.assertEqual(len(written), 2)
            for entry, record in zip(entries, written):
                with self.subTest(footprint=entry["footprint_id"]):
                    # 写盘字节必须与被核对的文本逐字节一致，否则哈希不可追溯
                    self.assertEqual(record["sha256"],
                                     hashlib.sha256(entry["text"].encode("utf-8")).hexdigest())
                    self.assertEqual(Path(record["path"]).read_bytes(),
                                     entry["text"].encode("utf-8"))
                    _, geometry, digest = load_footprint(entry["footprint_id"], [root])
                    self.assertEqual(digest, record["sha256"])
                    self.assertEqual(sorted(geometry),
                                     sorted(p["name"] for p in
                                            repro.parse_package_pads(
                                                package_node(self.raw, CONNECTOR_LIBRARY,
                                                             entry["source_package"]))))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_footprint_id_sanitiser_keeps_the_contract_shape(self):
        self.assertEqual(repro.repro_footprint_id(CONNECTOR_LIBRARY, "1X04_1MM_RA"),
                         "EagleRepro_SparkFun-Connectors:1X04_1MM_RA")
        self.assertEqual(repro.repro_footprint_id("Lib With Space", "PKG/A"),
                         "EagleRepro_Lib_With_Space:PKG_A")


class FootprintOverrideTests(unittest.TestCase):
    def test_explicit_component_footprint_beats_the_catalog_suggestion(self):
        self.assertEqual(footprint_for_part({"type": "i2c_header"}),
                         "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical")
        self.assertEqual(footprint_for_part({"type": "i2c_header",
                                             "component": {"footprint": "Lib:Exact"}}),
                         "Lib:Exact")

    def test_blank_override_falls_back_to_the_suggestion(self):
        for component in ({}, {"footprint": ""}, {"footprint": "   "}):
            with self.subTest(component=component):
                self.assertEqual(footprint_for_part({"type": "led", "component": component}),
                                 footprint_for_part({"type": "led"}))


if __name__ == "__main__":
    unittest.main()
