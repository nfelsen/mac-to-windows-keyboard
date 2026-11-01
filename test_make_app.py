import plistlib
import tempfile
import unittest
from pathlib import Path

import make_app


class MakeAppTests(unittest.TestCase):
    def test_info_plist(self):
        info = make_app.info_plist()
        self.assertEqual(info["CFBundleName"], "Mac2Win KVM")
        self.assertEqual(info["CFBundleDisplayName"], "Mac2Win KVM")
        self.assertEqual(info["CFBundleExecutable"], "launcher")
        self.assertEqual(info["CFBundleIconFile"], "icon.icns")
        self.assertEqual(info["CFBundlePackageType"], "APPL")

    def test_launcher_runs_bundled_interpreter(self):
        script = make_app.launcher_script(Path("/repo dir"), Path("/bin/uv"))
        self.assertTrue(script.startswith("#!/bin/bash\n"))
        self.assertIn("REPO='/repo dir'", script)
        self.assertIn('"$UV" sync', script)
        self.assertIn('BIN="$CONTENTS/MacOS/Mac2Win KVM"', script)
        self.assertTrue(script.rstrip().endswith('exec "$BIN" "$REPO/kvm_app.py"'))

    def test_build(self):
        with tempfile.TemporaryDirectory() as dest:
            app = make_app.build(dest, uv=Path("/bin/uv"))
            self.assertEqual(app.name, "Mac2Win KVM.app")
            contents = app / "Contents"
            with open(contents / "Info.plist", "rb") as f:
                self.assertEqual(plistlib.load(f)["CFBundleIdentifier"], make_app.BUNDLE_ID)
            self.assertTrue((contents / "Resources" / "icon.icns").is_file())
            launcher = contents / "MacOS" / "launcher"
            self.assertTrue(launcher.stat().st_mode & 0o111)
            self.assertEqual(make_app.build(dest, uv=Path("/bin/uv")), app)  # rebuild replaces


if __name__ == "__main__":
    unittest.main()
