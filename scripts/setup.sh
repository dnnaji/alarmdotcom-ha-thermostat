#!/usr/bin/env bash

# ─── Fail Fast Settings ────────────────────────────────────────────────────────
set -Eeuo pipefail
trap 'echo -e "\n❌ Error on line $LINENO. Exiting."; exit 1' ERR

# ─── Command Wrapper ───────────────────────────────────────────────────────────
check_command() {
    echo -e "\n🔹 Running: \033[1;36m$*\033[0m"
    "$@"
}

check_ha_version() {
    local req_file="$1"
    echo -e "\n\033[1;34m==> Checking Home Assistant version...\033[0m"

    # Get latest stable version from PyPI
    latest_version=$(pip index versions homeassistant | grep -m 1 'Available versions:' | grep -oE '[0-9]{4}\.[0-9]+\.[0-9]+' | head -n 1)

    if [ -z "$latest_version" ]; then
        echo "❌ Failed to fetch latest Home Assistant version"
        return 1
    fi

    # Update version in requirements file if needed
    if grep -q "homeassistant==" "$req_file"; then
        current_version=$(grep "homeassistant==" "$req_file" | cut -d'=' -f3)
        if [ "$current_version" != "$latest_version" ]; then
            echo "📝 Updating Home Assistant from $current_version to $latest_version"
            sed -i "s/homeassistant==.*/homeassistant==$latest_version/" "$req_file"
        else
            echo "✓ Home Assistant version $current_version is up to date"
        fi
    else
        echo "❌ Could not find Home Assistant in requirements file"
        return 1
    fi
}

# ─── Input Validation ──────────────────────────────────────────────────────────
if [[ -z "${LIBRARY_NAME}" || -z "${LIBRARY_GIT_URL}" || -z "${WORKSPACE_DIRECTORY}" ]]; then
    echo "❌ One or more required environment variables are missing: LIBRARY_NAME, LIBRARY_GIT_URL, WORKSPACE_DIRECTORY"
    exit 1
fi

library_name="${LIBRARY_NAME}"
repo_url="${LIBRARY_GIT_URL}"
lib_dir="/workspaces/$library_name"
workspace_dir="${WORKSPACE_DIRECTORY}"
go2rtc_version="v1.9.14"
go2rtc_binary="go2rtc_linux_amd64"
go2rtc_sha256="32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6"
go2rtc_url="https://github.com/AlexxIT/go2rtc/releases/download/${go2rtc_version}/${go2rtc_binary}"

# ─── Home Assistant Prerequisites ──────────────────────────────────────────────
echo -e "\n\033[1;34m==> Installing Home Assistant prerequisites...\033[0m"

check_command sudo apt-get update
check_command sudo apt-get upgrade -y
check_command sudo apt-get install -y \
    python3 python3-dev python3-venv python3-pip \
    bluez libffi-dev libssl-dev libjpeg-dev zlib1g-dev autoconf build-essential \
    libopenjp2-7 libtiff6 libturbojpeg0-dev tzdata ffmpeg liblapack3 \
    liblapack-dev libatlas-base-dev

# ─── Optional: go2rtc binary (for streaming support) ───────────────────────────
echo -e "\n\033[1;34m==> Installing go2rtc for optional streaming support...\033[0m"

check_command wget -O "$go2rtc_binary" "$go2rtc_url"
echo -e "\n🔹 Verifying: \033[1;36m$go2rtc_binary sha256\033[0m"
echo "$go2rtc_sha256  $go2rtc_binary" | sha256sum --check -
check_command chmod +x "$go2rtc_binary"
check_command sudo mv "$go2rtc_binary" /usr/local/bin/go2rtc
check_command go2rtc --version

# ─── Dev Requirements ──────────────────────────────────────────────────────────
echo -e "\n\033[1;34m==> Installing Python dev requirements...\033[0m"

check_command pip install --upgrade pip

# Check and update Home Assistant version before installing requirements
check_ha_version "$workspace_dir/requirements-dev.txt"

check_command pip install -r "$workspace_dir/requirements-dev.txt"

# Make sync versions script executable and run it
check_command chmod +x "$workspace_dir/scripts/sync_versions.py"
check_command "$workspace_dir/scripts/sync_versions.py"

# # ─── Pyright ────────────────────────────────────────────────────────
# echo -e "\n\033[1;34m==> Installing NodeJS...\033[0m"

# check_command npm install -g pyright

# ─── Home Assistant Script ─────────────────────────────────────────────────────
echo -e "\n\033[1;34m==> Making Home Assistant script executable...\033[0m"

check_command chmod +x "$workspace_dir/scripts/run-ha.sh"

# ─── Initialize Library ────────────────────────────────────────────────────────
echo -e "\n\033[1;34m==> Initializing library: $library_name\033[0m"

if [ ! -d "$lib_dir" ]; then
    echo -e "\n📥 Cloning $library_name repository..."
    check_command git clone "$repo_url" "$lib_dir"
else
    echo -e "\n📁 $library_name repository directory already exists."
fi

check_command pip install --editable "$lib_dir" --config-settings editable_mode=strict
check_command pip install -r "$lib_dir/requirements-dev.txt"

# ─── Check and Update Home Assistant Version ───────────────────────────────────
check_ha_version "$workspace_dir/requirements-dev.txt"

# ─── Pre-commit Hooks ──────────────────────────────────────────────────────────
echo -e "\n\033[1;34m==> Installing pre-commit hooks...\033[0m"

check_command pre-commit install
cd "$lib_dir"
check_command pre-commit install

# ─── Done ──────────────────────────────────────────────────────────────────────
echo -e "\n\033[1;32m✅ Setup complete.\033[0m"

exit 0
