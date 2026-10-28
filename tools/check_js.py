"""Vérifie la syntaxe JavaScript de la console (extrait le bloc <script>)."""

import pathlib
import subprocess
import sys

root = pathlib.Path(__file__).resolve().parents[1]
html_path = root / "src" / "icyloom" / "static" / "dashboard.html"
html = html_path.read_text(encoding="utf-8")

start = html.find("<script>")
end = html.find("</script>")
if start < 0 or end < 0:
    raise SystemExit("bloc <script> introuvable")

code = html[start + len("<script>"):end]
tmp = root / "var" / "_check.js"
tmp.write_text(code, encoding="utf-8")

node = None
for candidate in ("node", "node.exe"):
    try:
        subprocess.run([candidate, "--version"], capture_output=True, check=True)
        node = candidate
        break
    except (FileNotFoundError, subprocess.CalledProcessError):
        continue

if node is None:
    print("node absent : verification syntaxique impossible")
    tmp.unlink(missing_ok=True)
    sys.exit(0)

result = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True)
print(result.stdout or "", end="")
print(result.stderr or "", end="")
print("SYNTAXE OK" if result.returncode == 0 else f"ERREUR DE SYNTAXE (code {result.returncode})")
tmp.unlink(missing_ok=True)
sys.exit(result.returncode)
