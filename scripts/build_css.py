"""Build app/static/app.css. Run: python scripts/build_css.py [--watch]"""
import shutil
import subprocess
import sys

tailwind = shutil.which("tailwindcss") or ".venv/bin/tailwindcss"
cmd = [tailwind, "-i", "tailwind.css", "-o", "app/static/app.css", "--minify"]
if "--watch" in sys.argv:
    cmd.append("--watch")
subprocess.run(cmd, check=True)
