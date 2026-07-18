"""Build app/static/app.css. Run: python scripts/build_css.py [--watch]"""
import subprocess
import sys

cmd = ["tailwindcss", "-i", "tailwind.css", "-o", "app/static/app.css", "--minify"]
if "--watch" in sys.argv:
    cmd.append("--watch")
subprocess.run(cmd, check=True)
