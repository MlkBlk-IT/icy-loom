"""Met à jour une configuration YAML : ajoute l'allowlist cache_tools."""
import sys
from pathlib import Path

import yaml

path = Path(sys.argv[1])
payload = yaml.safe_load(path.read_text(encoding="utf-8"))

allow = {
    "list_metrics": True,
    "read_notes": True,
    "read_file": True,
    "list_dir": True,
    "search_code": True,
    "git_log": True,
}
servers = payload.get("tool_proxy", {}).get("servers", [])
for server in servers:
    server["cache_enabled"] = True
    server.setdefault("cache_tools", {}).update(allow)

path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
                encoding="utf-8")
print(f"{path} mis a jour : {len(servers)} serveur(s), "
      f"allowlist={sorted(allow)}")
