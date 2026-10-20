# -*- coding: utf-8 -*-
"""部署预检与回滚合同（B0 quality 层）。

只读校验四类部署合同的静态事实：本机启动链、容器镜像、服务器更新回滚、
发布包敏感文件排除。不启动服务、不联网、不写项目文件。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import build_release as release  # noqa: E402


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


HAS_LAUNCHERS = (PACKAGE_ROOT / "START_DEMO.bat").is_file() and (
    PACKAGE_ROOT / "start_demo.ps1"
).is_file()


class LocalLauncherContractTests(unittest.TestCase):
    @unittest.skipUnless(HAS_LAUNCHERS, "演示包根启动脚本不存在（仓库单独检出时跳过）")
    def test_launcher_chain_points_at_server_py(self) -> None:
        bat = _read(PACKAGE_ROOT / "START_DEMO.bat")
        ps1 = _read(PACKAGE_ROOT / "start_demo.ps1")
        self.assertIn("start_demo.ps1", bat)
        self.assertIn("server.py", ps1)

    @unittest.skipUnless(HAS_LAUNCHERS, "演示包根启动脚本不存在（仓库单独检出时跳过）")
    def test_launcher_never_echoes_the_api_key_value(self) -> None:
        ps1 = _read(PACKAGE_ROOT / "start_demo.ps1")
        self.assertIsNone(
            re.search(r"(Write-Host|echo)[^\n]*\$env:DEEPSEEK_API_KEY", ps1),
            "启动脚本不得回显 API Key 的值",
        )

    @unittest.skipUnless(HAS_LAUNCHERS, "演示包根启动脚本不存在（仓库单独检出时跳过）")
    def test_launcher_defaults_to_loopback(self) -> None:
        ps1 = _read(PACKAGE_ROOT / "start_demo.ps1")
        self.assertIn('$env:LSQC_HOST = "127.0.0.1"', ps1)


class ContainerContractTests(unittest.TestCase):
    def test_dockerfile_runs_server_unprivileged_and_probes_health(self) -> None:
        text = _read(ROOT / "Dockerfile")
        self.assertIn('CMD ["python", "-B", "server.py"]', text)
        self.assertRegex(text, r"(?m)^USER ")
        self.assertIn("/api/health", text)

    def test_docker_build_context_excludes_local_env(self) -> None:
        names = {
            line.strip()
            for line in _read(ROOT / ".dockerignore").splitlines()
            if line.strip() and not line.startswith("#")
        }
        self.assertIn(".env", names, "构建上下文不得携带本机 Key")
        self.assertIn(".env.local", names)
        self.assertNotIn(".env.template", names, "模板必须保留在镜像中供配置")

    def test_compose_reads_env_from_host_at_runtime(self) -> None:
        text = _read(ROOT / "docker-compose.yml")
        self.assertIn("env_file:", text)
        self.assertIn(".env", text)


class ServerUpdateRollbackContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = _read(ROOT / "deploy" / "update.sh")

    def test_backup_is_taken_before_overwrite_and_rollback_is_offered(self) -> None:
        self.assertRegex(self.script, r"BACKUP=.*\.tar\.gz")
        backup_at = self.script.index('tar -czf "$BACKUP"')
        overwrite_at = self.script.index('tar -xzf "$PKG"')
        self.assertLess(backup_at, overwrite_at, "必须先备份再覆盖")
        self.assertIn("回滚", self.script)
        self.assertIn("$BACKUP", self.script)

    def test_update_script_only_stops_its_own_recorded_process(self) -> None:
        self.assertNotIn("pkill", self.script)
        self.assertNotIn("killall", self.script)
        self.assertIn('kill "$OLD_PID"', self.script)


class ReleaseSecretExclusionContractTests(unittest.TestCase):
    def test_env_files_never_enter_the_archive(self) -> None:
        for name in (".env", ".env.local", ".env.production"):
            self.assertTrue(release.is_excluded(Path(name)), name)
        self.assertFalse(release.is_excluded(Path(".env.template")))

    def test_required_archive_files_exist_in_repo(self) -> None:
        for name in sorted(release.REQUIRED_ARCHIVE_FILES):
            self.assertTrue((ROOT / name).is_file(), name)

    def test_env_template_ships_so_public_package_stays_configurable(self) -> None:
        self.assertTrue((ROOT / ".env.template").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
