import re
import sys
from pathlib import Path

# Usage: rewrite_images_for_offline.py <registry-host> [<manifest.yaml> ...]
# Same tool as 17-spire-cross-cluster-mtls/rewrite_images_for_offline.py,
# copied here (not imported) so this directory stays self-contained —
# re-hosts every `image: <original-registry>/<path>:<tag>` line under
# <registry-host>, keeping path+tag, writing output into manifests-offline/
# alongside manifests/ (left untouched for online use).

DEFAULT_FILES = [
    "manifests/spire-1152-cluster1.yaml",
    "manifests/spire-1152-cluster2.yaml",
    "manifests/clusterspiffeids.yaml",
    "manifests/peer-client.yaml",
    "manifests/peer-server.yaml",
    "manifests/crds/clusterspiffeids.yaml",
    "manifests/crds/clusterfederatedtrustdomains.yaml",
]

IMAGE_LINE = re.compile(r"^(\s*image:\s*)([^\s]+)(\s*)$")


def rewrite_image_ref(ref: str, registry_host: str) -> str:
    parts = ref.split("/")
    if len(parts) >= 2 and ("." in parts[0] or ":" in parts[0]):
        rest = "/".join(parts[1:])
    else:
        rest = ref
    return f"{registry_host}/{rest}"


def main():
    if len(sys.argv) < 2:
        print("usage: rewrite_images_for_offline.py <registry-host> [<manifest.yaml> ...]")
        sys.exit(1)

    registry_host = sys.argv[1]
    files = sys.argv[2:] if len(sys.argv) > 2 else DEFAULT_FILES

    out_dir = Path("manifests-offline")
    for f in files:
        src = Path(f)
        rel = src.relative_to("manifests") if str(src).startswith("manifests/") else src
        dst = out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)

        lines = src.read_text().splitlines(keepends=True)
        out_lines = []
        changed = 0
        for line in lines:
            m = IMAGE_LINE.match(line)
            if m:
                prefix, ref, suffix = m.groups()
                new_ref = rewrite_image_ref(ref, registry_host)
                out_lines.append(f"{prefix}{new_ref}{suffix}\n" if not line.endswith("\n") else f"{prefix}{new_ref}{suffix}")
                if new_ref != ref:
                    changed += 1
            else:
                out_lines.append(line)
        dst.write_text("".join(out_lines))
        print(f"{f} -> {dst}  ({changed} image ref(s) rewritten)")


if __name__ == "__main__":
    main()
