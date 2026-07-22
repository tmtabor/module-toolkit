"""
Unit tests for the extracted side-effect functions in agents/effects.py.

Filesystem effects use tmp_path; HTTP and subprocess effects are patched so no
network or docker is touched. These are pure and fast (no LLM, no live marker).
"""
import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents import effects
from agents.effects_models import (
    BuildResult, DownloadResult, PushResult, UploadResult, ValidationResult, ZipResult,
)


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------

class TestFilesystemEffects:

    def test_make_module_dir_uses_injected_timestamp(self, tmp_path):
        path = effects.make_module_dir(str(tmp_path), "My Tool", "20260101_120000")
        assert path.endswith("my_tool_20260101_120000")
        from pathlib import Path
        assert Path(path).is_dir()

    def test_make_module_dir_honors_explicit_module_dir(self, tmp_path):
        target = tmp_path / "explicit"
        path = effects.make_module_dir(str(tmp_path), "tool", "ts", module_dir=str(target))
        assert path == str(target)
        assert target.is_dir()

    def test_write_then_read_round_trip(self, tmp_path):
        p = tmp_path / "sub" / "f.txt"
        effects.write_text_file(str(p), "hello")
        assert effects.read_text_file(str(p)) == "hello"

    def test_read_missing_returns_none(self, tmp_path):
        assert effects.read_text_file(str(tmp_path / "nope.txt")) is None

    def test_file_exists_true_for_present_file(self, tmp_path):
        p = tmp_path / "f.txt"
        p.write_text("hello")
        assert effects.file_exists(str(p)) is True

    def test_file_exists_false_for_missing_file(self, tmp_path):
        assert effects.file_exists(str(tmp_path / "nope.txt")) is False

    def test_remove_dir_removes_and_is_idempotent(self, tmp_path):
        d = tmp_path / "data"
        d.mkdir()
        (d / "x").write_text("x")
        effects.remove_dir(str(d))
        assert not d.exists()
        effects.remove_dir(str(d))  # no error on missing

    def test_find_wrapper_file_prefers_wrapper_then_run(self, tmp_path):
        (tmp_path / "run_tool.py").write_text("")
        (tmp_path / "wrapper.py").write_text("")
        (tmp_path / "manage.py").write_text("")  # excluded
        assert effects.find_wrapper_file(str(tmp_path)) == "wrapper.py"

    def test_find_wrapper_file_falls_back_to_run(self, tmp_path):
        (tmp_path / "manage.py").write_text("")
        (tmp_path / "run_tool.py").write_text("")
        assert effects.find_wrapper_file(str(tmp_path)) == "run_tool.py"

    def test_find_wrapper_file_none_when_empty(self, tmp_path):
        assert effects.find_wrapper_file(str(tmp_path)) is None

    def test_read_manifest_docker_image_unescapes_colon(self, tmp_path):
        (tmp_path / "manifest").write_text("job.docker.image=genepattern/foo\\:1.0\n")
        assert effects.read_manifest_docker_image(str(tmp_path)) == "genepattern/foo:1.0"

    def test_read_manifest_docker_image_none_when_absent(self, tmp_path):
        (tmp_path / "manifest").write_text("commandLine=echo hi\n")
        assert effects.read_manifest_docker_image(str(tmp_path)) is None

    def test_make_module_dir_bumps_suffix_on_name_collision(self, tmp_path):
        """Same tool_name+timestamp (two runs starting in the same wall-clock
        second, temporal/PHASE5.md Workstream E) must not silently share a dir."""
        first = effects.make_module_dir(str(tmp_path), "tool", "20260101_120000")
        second = effects.make_module_dir(str(tmp_path), "tool", "20260101_120000")
        assert first != second
        assert Path(first).is_dir() and Path(second).is_dir()

    def test_make_module_dir_concurrent_same_name_all_unique(self, tmp_path):
        """N threads racing to create the same-named dir at once (the actual
        failure mode -- a sequential test can't rule out a TOCTOU race)."""
        n = 8
        with ThreadPoolExecutor(max_workers=n) as pool:
            paths = list(pool.map(
                lambda _: effects.make_module_dir(str(tmp_path), "tool", "20260101_120000"),
                range(n),
            ))
        assert len(set(paths)) == n
        assert all(Path(p).is_dir() for p in paths)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class TestHttpEffects:

    def test_download_one_success(self, tmp_path):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.iter_content = lambda chunk_size: [b"abc", b"def"]
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: False
        with patch("agents.effects.requests.get", return_value=resp):
            result = effects.download_one("http://x/f.bam", str(tmp_path), "f.bam")
        assert isinstance(result, DownloadResult)
        assert result.ok and result.local_path.endswith("f.bam")
        assert (tmp_path / "f.bam").read_bytes() == b"abcdef"

    def test_download_one_failure_cleans_up(self, tmp_path):
        with patch("agents.effects.requests.get", side_effect=RuntimeError("boom")):
            result = effects.download_one("http://x/f.bam", str(tmp_path), "f.bam")
        assert not result.ok
        assert result.local_path is None
        assert "boom" in result.error
        assert not (tmp_path / "f.bam").exists()

    def test_upload_module_success_json(self, tmp_path):
        zp = tmp_path / "m.zip"
        zp.write_bytes(b"zip")
        resp = MagicMock(status_code=200)
        resp.json = lambda: {"status": "success", "message": "installed"}
        with patch("agents.effects.requests.post", return_value=resp):
            result = effects.upload_module(str(zp), "https://gp/", "u", "p")
        assert isinstance(result, UploadResult)
        assert result.success and result.message == "installed"

    def test_upload_module_http_ok_without_json(self, tmp_path):
        zp = tmp_path / "m.zip"
        zp.write_bytes(b"zip")
        resp = MagicMock(status_code=201, text="")
        resp.json = MagicMock(side_effect=ValueError("no json"))
        with patch("agents.effects.requests.post", return_value=resp):
            result = effects.upload_module(str(zp), "https://gp", "u", "p")
        assert result.success

    def test_upload_module_failure(self, tmp_path):
        zp = tmp_path / "m.zip"
        zp.write_bytes(b"zip")
        resp = MagicMock(status_code=500, text="err")
        resp.json = lambda: {"status": "failed", "message": "bad"}
        with patch("agents.effects.requests.post", return_value=resp):
            result = effects.upload_module(str(zp), "https://gp", "u", "p")
        assert not result.success and result.message == "bad"


# ---------------------------------------------------------------------------
# Archive + subprocess
# ---------------------------------------------------------------------------

class TestArchiveAndSubprocess:

    def _make_members(self, d):
        (d / "manifest").write_text("m")
        (d / "wrapper.py").write_text("w")
        (d / "ignored.txt").write_text("nope")

    def test_zip_includes_only_requested_members(self, tmp_path):
        self._make_members(tmp_path)
        result = effects.zip_artifacts(str(tmp_path), "tool.zip", ["manifest", "wrapper.py"])
        assert isinstance(result, ZipResult)
        assert result.ok and result.zip_path.endswith("tool.zip")
        with zipfile.ZipFile(result.zip_path) as zf:
            assert set(zf.namelist()) == {"manifest", "wrapper.py"}

    def test_zip_only_deletes_members(self, tmp_path):
        self._make_members(tmp_path)
        effects.zip_artifacts(str(tmp_path), "tool.zip", ["manifest", "wrapper.py"], zip_only=True)
        assert not (tmp_path / "manifest").exists()
        assert not (tmp_path / "wrapper.py").exists()
        assert (tmp_path / "tool.zip").exists()

    def test_zip_no_members_present(self, tmp_path):
        result = effects.zip_artifacts(str(tmp_path), "tool.zip", ["manifest"])
        assert not result.ok and result.zip_path is None

    def test_docker_push_success(self):
        proc = MagicMock(returncode=0)
        proc.stdout = io.StringIO("pushing...\ndone\n")
        proc.wait = MagicMock()
        with patch("agents.effects.subprocess.Popen", return_value=proc):
            result = effects.docker_push("genepattern/x:1")
        assert isinstance(result, PushResult)
        assert result.success

    def test_docker_push_nonzero(self):
        proc = MagicMock(returncode=1)
        proc.stdout = io.StringIO("denied\n")
        proc.wait = MagicMock()
        with patch("agents.effects.subprocess.Popen", return_value=proc):
            result = effects.docker_push("genepattern/x:1")
        assert not result.success

    def test_docker_push_no_docker_cli(self):
        with patch("agents.effects.subprocess.Popen", side_effect=FileNotFoundError()):
            result = effects.docker_push("genepattern/x:1")
        assert not result.success
        assert any("Docker CLI not found" in line for line in result.log)


# ---------------------------------------------------------------------------
# Linter dispatch + docker build
# ---------------------------------------------------------------------------

class TestLinterEffects:

    def test_run_linter_valid_wrapper(self):
        result = effects.run_linter(
            "validate_wrapper", "wrapper/examples/valid/sample_python_wrapper.py"
        )
        assert isinstance(result, ValidationResult)
        assert result.success is True
        assert result.output

    def test_run_linter_unknown_tool(self):
        result = effects.run_linter("validate_bogus", "x")
        assert not result.ok
        assert "Unknown validation tool" in result.output

    def test_build_and_test_image_wraps_dockerfile_linter(self):
        """build_and_test_image dispatches validate_dockerfile; patch the linter so no docker runs."""
        fake_linter = MagicMock()

        def _main(argv):
            print("Dockerfile validation PASSED")
            return 0

        fake_linter.main = _main
        with patch("agents.effects.importlib.import_module", return_value=fake_linter):
            result = effects.build_and_test_image("Dockerfile", ["-t", "x:1"])
        assert isinstance(result, BuildResult)
        assert result.success is True
