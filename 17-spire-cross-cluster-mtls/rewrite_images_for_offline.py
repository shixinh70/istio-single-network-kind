import re
import shutil
import sys
from pathlib import Path

# Usage: rewrite_images_for_offline.py <registry-host> [<manifest.yaml> ...]
# Rewrites every `image: <original-registry>/<path>:<tag>` line to
# `image: <registry-host>/<path>:<tag>` (strips the original registry host,
# keeps the rest of the path + tag), writing output into manifests-offline/
# alongside the input file's own name. Originals in manifests/ are left
# untouched so the plain (online) INSTALL_CONTROLLER_MANAGER_VERSION.md
# flow keeps working unmodified.
#
# If no manifest files are given, rewrites the fixed list of files that
# INSTALL_CONTROLLER_MANAGER_VERSION.md actually applies (the chosen
# architecture's install set — not the Phase 1-2 leftover files).

DEFAULT_FILES = [
    "manifests/spire-cluster1-134.yaml",
    "manifests/spire-cluster2-134.yaml",
    "manifests/spire-cluster2.yaml",
    "manifests/controller-manager-cluster1-134.yaml",
    "manifests/controller-manager-cluster2-134.yaml",
    "manifests/controller-manager-cluster2.yaml",
    "manifests/spiffe-csi-driver.yaml",
    "manifests/mcp-echo-spire.yaml",
    "manifests/agent-pod.yaml",
    # ClusterSPIFFEID / CRD files have no image: lines but are copied
    # through unchanged for a single self-contained offline manifest set.
    "manifests/clusterspiffeids-cluster1-134.yaml",
    "manifests/clusterspiffeids-cluster2-134.yaml",
    "manifests/clusterspiffeids-cluster2.yaml",
    "manifests/crds/clusterspiffeids.yaml",
    "manifests/crds/clusterfederatedtrustdomains.yaml",
]

IMAGE_LINE = re.compile(r"^(\s*image:\s*)([^\s]+)(\s*)$")


def rewrite_image_ref(ref: str, registry_host: str) -> str:
    # ref looks like "ghcr.io/spiffe/spire-server:1.11.2" or
    # "registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.6.0" or
    # "curlimages/curl:8.16.0" (no explicit registry host = implicit
    # docker.io). Strip whatever registry-like first segment is present
    # (contains a "." or ":") and re-host everything else under
    # registry_host, preserving the rest of the path + tag.
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
