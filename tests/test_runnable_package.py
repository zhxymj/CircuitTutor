import importlib.util
import hashlib
import json
import os
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "build_runnable_package.py"
SPEC = importlib.util.spec_from_file_location("build_runnable_package", SCRIPT)
BUILDER = importlib.util.module_from_spec(SPEC)
if SPEC and SPEC.loader and SCRIPT.is_file():
    sys.modules[SPEC.name] = BUILDER
    SPEC.loader.exec_module(BUILDER)


class RunnablePackageManifestTests(unittest.TestCase):
    def test_list_reports_manifest_summary_without_creating_output(self):
        self.assertTrue(SCRIPT.is_file(), "runnable package builder has not been implemented")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "智绘电路-企业命题演示包" / "智绘电路"
            toolchain = root / "智绘电路工具链"
            project.mkdir(parents=True)
            toolchain.mkdir()
            (project / "server.py").write_text("pass\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--list", "--project-root", str(project), "--toolchain-root", str(toolchain)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = json.loads(result.stdout)
            self.assertGreaterEqual(summary["file_count"], 1)
            self.assertIn("exclusion_counts", summary)

    def test_manifest_keeps_canonical_runtime_models_but_excludes_duplicate_assets_and_hcd_weight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "智绘电路-企业命题演示包" / "智绘电路"
            toolchain = root / "智绘电路工具链"
            runtime = project / "evidence" / "upgrade" / "open_source_integration" / "runtime"
            canonical_model = runtime / "worktree" / "CircuitVision" / "models" / "SAM2" / "sam.pt"
            hcd_model = runtime / "worktree" / "CircuitVision" / "hcd" / "checkpoint.pth"
            duplicate = runtime / "assets" / "model.pt"
            current_yolo = project / "evidence" / "current" / "yolo" / "best.pt"
            for path in (canonical_model, hcd_model, duplicate, current_yolo):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")

            names = {entry.archive_path for entry in BUILDER.collect_manifest(project, toolchain)}

            self.assertEqual(sum(name.endswith("/models/SAM2/sam.pt") for name in names), 1)
            self.assertFalse(any(name.endswith("runtime/assets/model.pt") for name in names))
            self.assertFalse(any(name.endswith("hcd/checkpoint.pth") for name in names))
            self.assertTrue(any(name.endswith("evidence/current/yolo/best.pt") for name in names))

    def test_manifest_keeps_runtime_inputs_and_excludes_secrets_and_cache(self):
        self.assertTrue(SCRIPT.is_file(), "runnable package builder has not been implemented")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "智绘电路-企业命题演示包" / "智绘电路"
            toolchain = root / "智绘电路工具链"
            (project / "core").mkdir(parents=True)
            (project / "core" / "engine.py").write_text("pass\n", encoding="utf-8")
            (project / "core" / "secret.py").write_text("module\n", encoding="utf-8")
            credential_module = project / "core" / "google" / "credential.py"
            credential_module.parent.mkdir()
            credential_module.write_text("class Credential: pass\n", encoding="utf-8")
            certificate = project / "core" / "certifi" / "cacert.pem"
            certificate.parent.mkdir()
            certificate.write_text("public CA bundle\n", encoding="utf-8")
            (project / ".env").write_text("SECRET=never-package\n", encoding="utf-8")
            (project / ".env.template").write_text("API_KEY=\n", encoding="utf-8")
            (project / "credentials.json").write_text("{}\n", encoding="utf-8")
            (project / "private.pem").write_text("private\n", encoding="utf-8")
            (project / ".git").mkdir()
            (project / ".git" / "config").write_text("private\n", encoding="utf-8")
            (project / "tmp").mkdir()
            (project / "tmp" / "partial.chunk").write_text("partial\n", encoding="utf-8")
            for included in ("image_samples", "external_references", "requirements_v2", "yolo_training"):
                folder = project / "data" / included
                folder.mkdir(parents=True)
                (folder / "input.txt").write_text("data input\n", encoding="utf-8")
            fixture = project / "data" / "external_import" / "fixtures" / "valid.json"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("{}\n", encoding="utf-8")
            (toolchain / "vision-python" / "Scripts").mkdir(parents=True)
            (toolchain / "vision-python" / "Scripts" / "python.exe").write_bytes(b"fixture")
            (toolchain / "vision-python-cache").mkdir()
            (toolchain / "vision-python-cache" / "download.whl").write_bytes(b"cache")
            (project / "docs").mkdir()
            (project / "docs" / "guide.md").write_text("docs\n", encoding="utf-8")
            (project / "web" / "assets").mkdir(parents=True)
            (project / "web" / "assets" / "logo.svg").write_text("<svg/>\n", encoding="utf-8")

            manifest = BUILDER.collect_manifest(project, toolchain)
            archive_names = {entry.archive_path for entry in manifest}

            self.assertIn(
                "智绘电路可运行包/智绘电路-企业命题演示包/智绘电路/core/engine.py",
                archive_names,
            )
            self.assertIn(
                "智绘电路可运行包/智绘电路工具链/vision-python/Scripts/python.exe",
                archive_names,
            )
            self.assertIn("智绘电路可运行包/智绘电路-企业命题演示包/智绘电路/.env.template", archive_names)
            self.assertTrue(any("core/secret.py" in name for name in archive_names))
            self.assertTrue(any("core/google/credential.py" in name for name in archive_names))
            self.assertTrue(any("core/certifi/cacert.pem" in name for name in archive_names))
            self.assertFalse(any(Path(name).name in (".env", "credentials.json", "private.pem") for name in archive_names))
            self.assertFalse(any("/.git/" in name for name in archive_names))
            self.assertFalse(any("partial.chunk" in name or "vision-python-cache" in name for name in archive_names))
            for included in ("image_samples", "external_references", "requirements_v2", "yolo_training"):
                self.assertTrue(any(f"data/{included}/input.txt" in name for name in archive_names))
            self.assertTrue(any("core/secret.py" in name for name in archive_names))
            self.assertTrue(any("docs/guide.md" in name for name in archive_names))
            self.assertTrue(any("web/assets/logo.svg" in name for name in archive_names))
            self.assertIn(
                "智绘电路可运行包/智绘电路-企业命题演示包/智绘电路/data/external_import/fixtures/valid.json",
                archive_names,
            )

    def test_zip_writer_uses_zip64_and_contains_relative_first_run_templates(self):
        self.assertTrue(SCRIPT.is_file(), "runnable package builder has not been implemented")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.py"
            source.write_text("print('fixture')\n", encoding="utf-8")
            image = root / "fixture.pt"
            image.write_bytes(b"already-compressed")
            entry = BUILDER.ManifestEntry(
                source=source,
                archive_path="智绘电路可运行包/智绘电路-企业命题演示包/智绘电路/source.py",
                size=source.stat().st_size,
            )
            image_entry = BUILDER.ManifestEntry(
                source=image,
                archive_path="智绘电路可运行包/智绘电路-企业命题演示包/智绘电路/fixture.pt",
                size=image.stat().st_size,
            )
            target = root / "small.zip"

            BUILDER.write_archive([entry, image_entry], target)

            with ZipFile(target) as archive:
                names = set(archive.namelist())
                self.assertIn("智绘电路可运行包/README.md", names)
                self.assertIn("智绘电路可运行包/首次解压.ps1", names)
                self.assertIn("智绘电路可运行包/首次解压.bat", names)
                self.assertIn("智绘电路可运行包/manifest.sha256.json", names)
                readme = archive.read("智绘电路可运行包/README.md").decode("utf-8")
                for statement in (
                    "Windows x64", "建议使用 7-Zip", "离线模板", "CircuitVision 支持离线", "无需 API key",
                    "HCD 未授权分发", "格式适配功能仍然可用", "2026-10-14 状态快照",
                    "其他电脑首次运行前", "acceptance 证据", "选择本地 CircuitVision",
                    "每个引脚的网络", "未知参数不会被猜测补齐",
                ):
                    self.assertIn(statement, readme)
                self.assertEqual(archive.getinfo(entry.archive_path).extract_version, 45)
                self.assertEqual(archive.getinfo(entry.archive_path).compress_type, zipfile.ZIP_DEFLATED)
                self.assertEqual(archive.getinfo(image_entry.archive_path).compress_type, zipfile.ZIP_STORED)
                inner_manifest = json.loads(archive.read("智绘电路可运行包/manifest.sha256.json"))
                self.assertEqual(inner_manifest[entry.archive_path], hashlib.sha256(source.read_bytes()).hexdigest())
                self.assertEqual(
                    archive.getinfo("智绘电路可运行包/README.md").compress_type,
                    zipfile.ZIP_DEFLATED,
                )
                bootstrap = archive.read("智绘电路可运行包/首次解压.ps1").decode("utf-8-sig")
                self.assertIn("$PSScriptRoot", bootstrap)
                self.assertIn("pyvenv.cfg", bootstrap)
                self.assertIn("shared-vision-site.pth", bootstrap)
                self.assertIn("sys.prefix", bootstrap)
                self.assertIn("import server", bootstrap)
                self.assertIn("import streamlit,ultralytics,sam2", bootstrap)
                self.assertIn("[switch]$CheckOnly", bootstrap)
                self.assertIn("[switch]$NoBrowser", bootstrap)
                self.assertIn("[int]$Port", bootstrap)
                batch_text = archive.read("智绘电路可运行包/首次解压.bat").decode("utf-8")
                self.assertIn("%*", batch_text)
                self.assertIn("chcp 65001>nul", batch_text)
                self.assertNotIn("'User'", bootstrap)
                self.assertNotIn("'Machine'", bootstrap)
                self.assertIn("'Process'", bootstrap)
                self.assertIn("pathlib.Path(sys.prefix).parents[2]", bootstrap)
                self.assertIn('"vision-python"/"Lib"/"site-packages"', bootstrap)
                self.assertIn("utf8", bootstrap.casefold())
                self.assertIn("TcpListener", bootstrap)
                self.assertIn("$offset -lt 16", bootstrap)
                self.assertIn("open-source\\tscircuit", bootstrap)
                self.assertIn("Start-Job", bootstrap)
                self.assertIn("WriteAllText", bootstrap)
                self.assertNotIn("Set-Content -LiteralPath $cfg -Encoding ascii", bootstrap)
                self.assertTrue(archive.read("智绘电路可运行包/首次解压.ps1").startswith(b"\xef\xbb\xbf"))

    def test_manifest_summary_writes_future_build_evidence_without_building(self):
        self.assertTrue(SCRIPT.is_file(), "runnable package builder has not been implemented")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "智绘电路-企业命题演示包" / "智绘电路"
            toolchain = root / "智绘电路工具链"
            project.mkdir(parents=True)
            toolchain.mkdir()

            manifest = BUILDER.collect_manifest(project, toolchain)
            summary = BUILDER.summarize_manifest(manifest)

            self.assertEqual(summary["file_count"], len(manifest))
            self.assertEqual(summary["byte_count"], sum(entry.size for entry in manifest))
            self.assertIn("manifest_counts", summary)
            self.assertIn("longest_zip_entry_path", summary)
            json.dumps(summary)

    def test_bootstrap_bat_forwards_checkonly_and_user_port_through_cmd(self):
        self.assertTrue(SCRIPT.is_file(), "runnable package builder has not been implemented")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "首次解压.ps1").write_text(
                "[CmdletBinding()]\nparam([switch]$CheckOnly,[switch]$NoBrowser,[int]$Port=8765)\n"
                "if ($CheckOnly) { Write-Output ('CHECK_OK:' + $NoBrowser + ':' + $Port); exit 0 }\n",
                encoding="utf-8-sig",
            )
            batch = root / "首次解压.bat"
            batch.write_bytes(BUILDER.render_bootstrap_bat().encode("utf-8"))
            command = f'{batch} -CheckOnly -NoBrowser -Port 9876'
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("CHECK_OK:True:9876", result.stdout)

    def test_bootstrap_powershell_template_parses_on_windows_powershell(self):
        self.assertTrue(SCRIPT.is_file(), "runnable package builder has not been implemented")
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "首次解压.ps1"
            script.write_bytes(BUILDER.render_bootstrap_ps1().encode("utf-8"))
            quoted_path = str(script).replace("'", "''")
            command = (
                "$tokens=$null;$errors=$null;"
                f"[System.Management.Automation.Language.Parser]::ParseFile('{quoted_path}',[ref]$tokens,[ref]$errors)|Out-Null;"
                "if($errors.Count){$errors|ForEach-Object{Write-Error $_};exit 1};exit 0"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class RunUpstreamRelocationTests(unittest.TestCase):
    def test_default_python_path_is_relative_to_script(self):
        run_script = SCRIPT.parent / "run_upstream.ps1"
        text = run_script.read_text(encoding="utf-8")
        self.assertIn("$PSScriptRoot", text)
        self.assertNotIn("D:\\智绘电路\\智绘电路工具链\\open-source\\circuitvision", text)
        self.assertTrue(run_script.read_bytes().startswith(b"\xef\xbb\xbf"))


@unittest.skipUnless(sys.platform == "win32", "Windows launcher integration")
class BootstrapEnvironmentTests(unittest.TestCase):
    """Exercise generated launchers with real Python in a moved, Unicode path.

    The venvs use the test interpreter's base runtime. Stub imports keep startup
    tests independent of model inference and third-party installs; full runtime
    relocation is covered by the extracted-package acceptance check.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=SCRIPT.parents[1], prefix="启动 环境测试-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / BUILDER.ENTERPRISE_DIR / BUILDER.PROJECT_DIR
        self.project.mkdir(parents=True)
        self.toolchain = self.root / BUILDER.TOOLCHAIN_DIR
        for relative in ("vision-python", "open-source/circuitvision/venv"):
            venv = self.toolchain / relative
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)],
                           capture_output=True, check=True, timeout=30)
        site = self.toolchain / "open-source/circuitvision/venv/Lib/site-packages"
        for module in ("streamlit", "ultralytics", "sam2"):
            (site / (module + ".py")).write_text("", encoding="utf-8")
        (self.project / "tools").mkdir()
        (self.project / "tools/activate_toolchain.ps1").write_text("# no extra activation\n", encoding="utf-8")
        (self.project / "server.py").write_text("pass\n", encoding="utf-8")
        self.script = self.root / "首次解压.ps1"
        template = BUILDER.render_bootstrap_ps1().replace(
            "$pythonHome = Join-Path $toolchainRoot 'kicad\\bin'",
            "$pythonHome = '" + str(Path(sys.base_prefix)).replace("'", "''") + "'")
        self.script.write_bytes(template.encode("utf-8"))

    def run_launcher(self, *arguments, extra_env=None):
        env = dict(os.environ, PYTHONUTF8="1")
        env.update(extra_env or {})
        return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                               "-File", str(self.script), "-NoBrowser", *arguments],
                              cwd=SCRIPT.parents[1], env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=40)

    def test_inherited_old_tscircuit_prefix_does_not_disable_packaged_runtime(self):
        prefix = self.toolchain / "open-source/tscircuit"
        (prefix / "node_modules/@tscircuit/eval").mkdir(parents=True)
        (prefix / "node_modules/@tscircuit/eval/package.json").write_text("{}")
        (prefix / "node.exe").touch()
        # Use the real runtime resolver: the stale environment currently raises
        # runtime_unavailable even though this package contains a valid prefix.
        (self.project / "server.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(SCRIPT.parents[1])!r})\n"
            "from core.tscircuit_engine import _runtime_paths\n"
            f"assert _runtime_paths()[1] == Path({str(prefix)!r}).resolve()\n"
            "print('PACKAGED_TSCIRCUIT_OK')\n", encoding="utf-8")
        result = self.run_launcher(extra_env={"TSCIRCUIT_PREFIX": str(self.root / "deleted old runtime")})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PACKAGED_TSCIRCUIT_OK", result.stdout)

    def test_direct_powershell_start_in_unicode_path_reads_prefix_as_utf8(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_inherited_pythonhome_and_pythonpath_do_not_break_package_python(self):
        (self.project / "server.py").write_text(
            "import os,sys\nassert not os.environ.get('PYTHONHOME')\n"
            "assert not os.environ.get('PYTHONPATH')\nprint('ISOLATED_PYTHON_OK')\n", encoding="utf-8")
        result = self.run_launcher(extra_env={"PYTHONHOME": str(Path(sys.base_prefix)),
                                              "PYTHONPATH": str(self.root / "foreign site")})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ISOLATED_PYTHON_OK", result.stdout)

    def test_checkonly_imports_share_offline_environment_with_normal_start(self):
        (self.project / "server.py").write_text(
            "import os\nassert not os.environ.get('OPENAI_API_KEY')\n"
            "assert os.environ.get('PYTHON_DOTENV_DISABLED') == '1'\n", encoding="utf-8")
        result = self.run_launcher("-CheckOnly", extra_env={"OPENAI_API_KEY": "test-placeholder"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CHECK_OK", result.stdout)

    def test_failed_double_click_keeps_diagnostic_and_preserves_exit_code(self):
        batch = self.root / "首次解压.bat"
        batch.write_bytes(BUILDER.render_bootstrap_bat().encode("utf-8"))
        self.script.write_text("Write-Output 'fixture startup failure'; exit 23\n", encoding="utf-8-sig")
        result = subprocess.run(["cmd.exe", "/d", "/c", str(batch)], input="\n",
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertIn("Launch failed (exit 23)", result.stdout)


if __name__ == "__main__":
    unittest.main()
