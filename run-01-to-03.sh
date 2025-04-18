#!/bin/bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

run_scripts_in_folder() {
  local folder="$1"
  local folder_path="${BASE_DIR}/${folder}"

  echo "🔽 Entering directory: $folder"

  for script in $(ls "${folder_path}"/*.sh 2>/dev/null | sort); do
    script_name="$(basename "$script")"
    echo "🚀 Executing: ${script_name}"
    (
      cd "${folder_path}"
      bash "./${script_name}"
    )
    echo "✅ Done: ${script_name}"
    echo "------------------------------------"
  done
}

echo "📦 Starting full environment setup..."

run_scripts_in_folder "01-kind"
run_scripts_in_folder "02-istio"
run_scripts_in_folder "03-testing"

echo "🏁 All steps completed successfully!"
