"""Read and update a deploy environment file.

The file is plain KEY="value" lines so that bash can `source` it and python-dotenv
can load it, which is what lets every phase - shell, Bicep outputs, the agent
provisioning script, the evals - share one source of truth per environment.

    envfile.py FILE set KEY=VALUE ...          upsert
    envfile.py FILE default KEY=VALUE ...      set only if missing or empty
    envfile.py FILE outputs DEPLOYMENT.json    write every output of an az deployment
    envfile.py FILE get KEY                    print one value
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LINE = re.compile(r'^([A-Z][A-Z0-9_]*)="?(.*?)"?$')

# Characters that would change meaning when bash sources the file. Refused rather
# than escaped: every value here is generated or comes from Azure, so one of these
# appearing means something upstream is wrong.
UNSAFE = set('"$`\\\n')


def read(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            m = LINE.match(raw.strip())
            if m:
                values[m.group(1)] = m.group(2)
    return values


def write(path: Path, values: dict[str, str]) -> None:
    for key, value in values.items():
        bad = UNSAFE.intersection(value)
        if bad:
            sys.exit(f"refusing to write {key}: value contains {sorted(bad)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f'{k}="{v}"\n' for k, v in values.items())
    path.write_text("# Written by deploy/scripts/envfile.py - holds secrets, never commit.\n" + body,
                    encoding="utf-8")
    path.chmod(0o600)


def parse_pairs(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not LINE.match(f"{key}=x"):
            sys.exit(f"expected KEY=VALUE, got {pair!r}")
        out[key] = value
    return out


def main() -> int:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    path, command, args = Path(sys.argv[1]), sys.argv[2], sys.argv[3:]
    values = read(path)

    if command == "set":
        values.update(parse_pairs(args))
    elif command == "default":
        for key, value in parse_pairs(args).items():
            if not values.get(key):
                values[key] = value
    elif command == "outputs":
        deployment = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        outputs = deployment.get("properties", deployment).get("outputs", {})
        for key, out in outputs.items():
            value = out.get("value")
            # The az CLI camel-cases output names on the way out (APP_SERVICE_SKU
            # arrives as apP_SERVICE_SKU). Every output in main.bicep is upper
            # case by design, so upper() restores the name exactly.
            values[key.upper()] = value if isinstance(value, str) else json.dumps(value)
    elif command == "get":
        print(values.get(args[0], ""))
        return 0
    else:
        sys.exit(f"unknown command {command!r}")

    write(path, values)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
