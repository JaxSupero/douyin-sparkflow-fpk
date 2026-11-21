"""Regression checks for the fnOS browser patch; no Playwright install needed.

Run: python3 -m unittest discover -s tests -v
"""
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "patch-src.py"
PATCHER = runpy.run_path(str(SCRIPT))

# Upstream 87a08ce: retain the keyword-only engine selection and its overrides.
BROWSER_SOURCE = '''\
def _browser_launch_options(GUI=False, network_mode=None, *, engine_selection=None):
    args = _browser_args()
    proxy = _douyin_browser_proxy(network_mode=network_mode)
    if proxy:
        options = {
            "headless": _headless_for(GUI),
            "args": args,
            "proxy": {"server": proxy},
        }
    else:
        args.append("--no-proxy-server")
        options = {
            "headless": _headless_for(GUI),
            "args": args,
        }
    if engine_selection is not None:
        overrides = dict(engine_selection.launch_overrides())
        options["args"].extend(list(overrides.pop("args", [])))
        options.update(overrides)
    return options
'''


def patch_browser(source=BROWSER_SOURCE):
    for old, new, expect in PATCHER["PATCHES"]["core/browser.py"]:
        source = PATCHER["replace_once"](
            source, old, new, file="core/browser.py", expect=expect
        )
    return source


def launcher(source=None, *, headful=False):
    scope = {
        "_browser_args": lambda: ["--disable-dev-shm-usage", "--no-sandbox"],
        "_headless_for": lambda gui: False if headful else not gui,
        "_douyin_browser_proxy": lambda network_mode: (
            "http://127.0.0.1:7890" if network_mode == "mihomo" else None
        ),
    }
    exec(compile(source if source is not None else patch_browser(),
                 "core/browser.py", "exec"), scope)
    return scope["_browser_launch_options"]


class Engine:
    def __init__(self, **overrides):
        self.overrides = overrides

    def launch_overrides(self):
        return self.overrides


class BrowserPatchTests(unittest.TestCase):
    def test_default_headless_uses_bundled_chromium(self):
        options = launcher()()
        self.assertTrue(options["headless"])
        self.assertEqual(options["channel"], "chromium")
        self.assertEqual(options["args"], ["--disable-dev-shm-usage",
                                          "--no-sandbox", "--no-proxy-server"])
        self.assertNotIn("proxy", options)

    def test_headful_login_keeps_upstream_behavior(self):
        self.assertEqual(launcher()(GUI=True), launcher(BROWSER_SOURCE)(GUI=True))

    def test_forced_headful_does_not_add_channel(self):
        options = launcher(headful=True)()
        self.assertFalse(options["headless"])
        self.assertNotIn("channel", options)

    def test_proxy_is_preserved(self):
        options = launcher()(network_mode="mihomo")
        self.assertEqual(options["proxy"], {"server": "http://127.0.0.1:7890"})
        self.assertNotIn("--no-proxy-server", options["args"])
        self.assertEqual(options["channel"], "chromium")

    def test_stock_engine_keeps_bundled_channel(self):
        self.assertEqual(launcher()(engine_selection=Engine())["channel"], "chromium")

    def test_custom_engine_executable_and_arguments_are_unchanged(self):
        engine = Engine(executable_path="/verified/fingerprint/chrome",
                        args=["--fingerprint=123"])
        for gui in (False, True):
            for route in ("direct", "mihomo"):
                with self.subTest(gui=gui, route=route):
                    kwargs = dict(GUI=gui, network_mode=route, engine_selection=engine)
                    self.assertEqual(launcher()(**kwargs), launcher(BROWSER_SOURCE)(**kwargs))
        self.assertEqual(engine.overrides["args"], ["--fingerprint=123"])

    def test_explicit_channel_is_not_overwritten(self):
        options = launcher()(engine_selection=Engine(channel="chrome"))
        self.assertEqual(options["channel"], "chrome")

    def test_final_engine_headless_override_is_respected(self):
        for headless in (False, True):
            with self.subTest(headless=headless):
                options = launcher()(GUI=True, engine_selection=Engine(headless=headless))
                self.assertEqual(options["headless"], headless)
                self.assertEqual("channel" in options, headless)

    def test_engine_override_mapping_is_not_mutated(self):
        engine = Engine(args=["--engine-flag"], timeout=4321)
        options = launcher()(engine_selection=engine)
        self.assertEqual(options["timeout"], 4321)
        self.assertIn("--engine-flag", options["args"])
        self.assertEqual(engine.overrides, {"args": ["--engine-flag"], "timeout": 4321})

    def test_missing_anchor_still_fails_closed(self):
        with self.assertRaises(SystemExit):
            patch_browser("def different_upstream():\n    return {}\n")

    def test_ambiguous_anchor_still_fails_closed(self):
        with self.assertRaises(SystemExit):
            patch_browser(BROWSER_SOURCE + BROWSER_SOURCE)

    def test_cli_check_is_read_only_and_apply_updates_all_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = {}
            for rel, operations in PATCHER["PATCHES"].items():
                text = BROWSER_SOURCE if rel == "core/browser.py" else "\n".join(
                    old * expect for old, _new, expect in operations
                )
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                original[rel] = path.read_bytes()
            checked = subprocess.run([sys.executable, str(SCRIPT), str(root), "--check"],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertEqual(original, {rel: (root / rel).read_bytes() for rel in original})
            applied = subprocess.run([sys.executable, str(SCRIPT), str(root)],
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            for rel, before in original.items():
                self.assertNotEqual((root / rel).read_bytes(), before)
            actual_browser = (root / "core/browser.py").read_text(encoding="utf-8")
            self.assertEqual(launcher(actual_browser)()["channel"], "chromium")


if __name__ == "__main__":
    unittest.main()
