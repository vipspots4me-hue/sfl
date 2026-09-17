import os
import re
import sys
import time
import tarfile
import shutil
import platform
import subprocess
import threading
import urllib.request
from pathlib import Path

import streamlit as st


# ============================================================
# SHL SERVER
# FreeRoot-style Ubuntu + PRoot + SSHX
# ============================================================

st.set_page_config(
    page_title="SHL Server",
    page_icon="🖥️",
    layout="wide",
)


# ============================================================
# PATHS
# ============================================================

RUNTIME = Path("/tmp/shl-runtime")

ROOTFS = RUNTIME / "ubuntu"

PROOT_DIR = RUNTIME / "proot"

PROOT = PROOT_DIR / "proot"

LOG_DIR = RUNTIME / "logs"

STATE_DIR = RUNTIME / "state"

SSHX_DIR = Path.home() / ".local" / "bin"

SSHX = SSHX_DIR / "sshx"


# ============================================================
# URLS
# ============================================================

UBUNTU_BASE_URL = (
    "https://cdimage.ubuntu.com/"
    "ubuntu-base/releases/22.04/release/"
    "ubuntu-base-22.04.5-base-amd64.tar.gz"
)

PROOT_URL = (
    "https://github.com/"
    "Mytai20100/freeproot/releases/latest/download/"
    "proot-amd64"
)

SSHX_URL = (
    "https://s3.amazonaws.com/sshx/"
    "sshx-x86_64-unknown-linux-musl.tar.gz"
)


# ============================================================
# MARKERS
# ============================================================

ROOTFS_MARKER = ROOTFS / ".shl_rootfs_ready"

PROOT_MARKER = PROOT_DIR / ".shl_proot_ready"

SSHX_MARKER = STATE_DIR / ".sshx_ready"


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(
        f"[SHL] {message}",
        flush=True,
    )


def write_log(name, text):

    LOG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        (LOG_DIR / name).write_text(
            text,
            errors="ignore",
        )
    except Exception:
        pass


# ============================================================
# DOWNLOAD
# ============================================================

def download_file(
    url,
    destination,
    label="file",
):

    destination = Path(destination)

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = destination.with_suffix(
        destination.suffix + ".download"
    )

    if temporary.exists():
        temporary.unlink()

    log(
        f"Downloading {label}: {url}"
    )

    try:

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent":
                    "Mozilla/5.0 SHL-Server"
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=60,
        ) as response:

            total = response.headers.get(
                "Content-Length"
            )

            total = (
                int(total)
                if total
                else None
            )

            downloaded = 0

            with open(
                temporary,
                "wb",
            ) as output:

                while True:

                    chunk = response.read(
                        1024 * 1024
                    )

                    if not chunk:
                        break

                    output.write(chunk)

                    downloaded += len(
                        chunk
                    )

                    if total:

                        percent = (
                            downloaded
                            * 100
                            / total
                        )

                        log(
                            f"{label}: "
                            f"{percent:.1f}%"
                        )

        if not temporary.exists():
            raise RuntimeError(
                f"{label} download produced no file"
            )

        if temporary.stat().st_size < 1024:

            raise RuntimeError(
                f"{label} download is too small"
            )

        temporary.replace(
            destination
        )

        return True

    except Exception as e:

        log(
            f"{label} download failed: {e}"
        )

        try:
            temporary.unlink(
                missing_ok=True
            )
        except Exception:
            pass

        return False


# ============================================================
# ARCHITECTURE
# ============================================================

def check_architecture():

    arch = platform.machine().lower()

    log(
        f"Detected architecture: {arch}"
    )

    if arch not in (
        "x86_64",
        "amd64",
    ):

        raise RuntimeError(
            "This version currently supports "
            "x86_64/amd64 only."
        )

    return "amd64"


# ============================================================
# PREPARE DIRECTORIES
# ============================================================

def prepare_directories():

    RUNTIME.mkdir(
        parents=True,
        exist_ok=True,
    )

    ROOTFS.mkdir(
        parents=True,
        exist_ok=True,
    )

    PROOT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    LOG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )


# ============================================================
# UBUNTU ROOTFS
# ============================================================

def install_ubuntu():

    if ROOTFS_MARKER.exists():

        log(
            "Ubuntu rootfs already exists."
        )

        return True

    archive = (
        RUNTIME
        / "ubuntu-base.tar.gz"
    )

    log(
        "Ubuntu rootfs is not installed."
    )

    if not download_file(
        UBUNTU_BASE_URL,
        archive,
        "Ubuntu 22.04.5",
    ):

        return False

    log(
        "Extracting Ubuntu rootfs..."
    )

    # Clean partial installation.
    for item in ROOTFS.iterdir():

        try:

            if item.is_dir():
                shutil.rmtree(
                    item
                )

            else:
                item.unlink()

        except Exception as e:

            log(
                f"Could not remove {item}: {e}"
            )

    try:

        with tarfile.open(
            archive,
            "r:gz",
        ) as tar:

            tar.extractall(
                ROOTFS
            )

    except Exception as e:

        log(
            f"Ubuntu extraction failed: {e}"
        )

        return False

    # DNS
    etc = ROOTFS / "etc"

    etc.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:

        resolv = (
            "nameserver 1.1.1.1\n"
            "nameserver 1.0.0.1\n"
        )

        (etc / "resolv.conf").write_text(
            resolv
        )

    except Exception as e:

        log(
            f"DNS configuration warning: {e}"
        )

    # hostname
    try:

        (
            etc / "hostname"
        ).write_text(
            "shl"
        )

        (
            etc / "hosts"
        ).write_text(
            "127.0.0.1 localhost\n"
            "127.0.1.1 shl\n"
            "::1 localhost ip6-localhost "
            "ip6-loopback\n"
        )

    except Exception:
        pass

    try:

        archive.unlink(
            missing_ok=True
        )

    except Exception:
        pass

    ROOTFS_MARKER.touch()

    log(
        "Ubuntu rootfs installed."
    )

    return True


# ============================================================
# PROOT
# ============================================================

def install_proot():

    if (
        PROOT.exists()
        and PROOT_MARKER.exists()
    ):

        log(
            "PRoot already installed."
        )

        return True

    log(
        "Downloading PRoot..."
    )

    if not download_file(
        PROOT_URL,
        PROOT,
        "PRoot amd64",
    ):

        return False

    try:

        PROOT.chmod(
            0o755
        )

    except Exception as e:

        log(
            f"Could not chmod PRoot: {e}"
        )

        return False

    # Verify executable.
    result = subprocess.run(
        [
            str(PROOT),
            "--help",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=20,
    )

    write_log(
        "proot-help.log",
        result.stdout or "",
    )

    if result.returncode not in (
        0,
        1,
    ):

        log(
            "PRoot executable verification failed."
        )

        log(
            result.stdout[-3000:]
        )

        return False

    PROOT_MARKER.touch()

    log(
        "PRoot installed successfully."
    )

    return True


# ============================================================
# UBUNTU COMMAND
# ============================================================

def ubuntu_command(
    command,
    background=False,
):

    if isinstance(
        command,
        str,
    ):

        shell_command = command

    else:

        shell_command = " ".join(
            subprocess.list2cmdline(
                [str(x)]
            )
            for x in command
        )

    # We deliberately use the standard PRoot
    # command-line interface.
    #
    # -r ROOTFS       guest root filesystem
    # -0               fake root UID/GID
    # -w /root        working directory
    # -b /dev         expose devices
    # -b /proc        expose proc
    # -b /sys         expose sys
    # -b /dev/pts     expose PTY
    # -b resolv.conf  DNS
    #
    # PRoot does not create a real kernel VM/root.
    # It translates filesystem/process operations
    # in user space.

    cmd = [
        str(PROOT),

        "-r",
        str(ROOTFS),

        "-0",

        "-w",
        "/root",

        "-b",
        "/dev",

        "-b",
        "/dev/pts",

        "-b",
        "/proc",

        "-b",
        "/sys",

        "-b",
        f"{ROOTFS}/etc/resolv.conf:/etc/resolv.conf",

        "/bin/bash",

        "-lc",

        shell_command,
    ]

    log(
        "Ubuntu command: "
        + " ".join(cmd)
    )

    if background:

        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=300,
        check=False,
    )


# ============================================================
# TEST UBUNTU
# ============================================================

def test_ubuntu():

    result = ubuntu_command(
        """
        echo SHL_UBUNTU_OK
        echo "USER=$(id -un)"
        echo "UID=$(id -u)"
        echo "OS=$(grep PRETTY_NAME /etc/os-release 2>/dev/null)"
        echo "ARCH=$(uname -m)"
        """
    )

    output = result.stdout or ""

    write_log(
        "ubuntu-test.log",
        output,
    )

    log(
        output[-5000:]
    )

    return (
        "SHL_UBUNTU_OK" in output
        and result.returncode == 0
    )


# ============================================================
# INSTALL BASIC TOOLS IN UBUNTU
# ============================================================

def prepare_ubuntu_tools():

    marker = (
        ROOTFS
        / "root"
        / ".shl_tools_ready"
    )

    if marker.exists():

        return True

    log(
        "Installing basic Ubuntu tools..."
    )

    result = ubuntu_command(
        """
        export DEBIAN_FRONTEND=noninteractive

        apt-get update

        apt-get install -y \
            bash \
            ca-certificates \
            curl \
            wget \
            unzip \
            tar \
            gzip \
            procps \
            iproute2 \
            iputils-ping \
            net-tools \
            python3 \
            python3-pip

        touch /root/.shl_tools_ready
        """
    )

    output = result.stdout or ""

    write_log(
        "ubuntu-tools.log",
        output,
    )

    if result.returncode != 0:

        log(
            "Ubuntu tools installation failed."
        )

        log(
            output[-5000:]
        )

        return False

    log(
        "Ubuntu tools installed."
    )

    return True


# ============================================================
# SSHX
# ============================================================

def install_sshx():

    SSHX_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if SSHX.exists():

        try:
            SSHX.chmod(0o755)
        except Exception:
            pass

        return True

    archive = (
        RUNTIME
        / "sshx.tar.gz"
    )

    extract = (
        RUNTIME
        / "sshx-extract"
    )

    if not download_file(
        SSHX_URL,
        archive,
        "SSHX",
    ):

        return False

    shutil.rmtree(
        extract,
        ignore_errors=True,
    )

    extract.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:

        with tarfile.open(
            archive,
            "r:gz",
        ) as tar:

            tar.extractall(
                extract
            )

    except Exception as e:

        log(
            f"SSHX extraction failed: {e}"
        )

        return False

    found = None

    for path in extract.rglob(
        "sshx"
    ):

        if path.is_file():

            found = path

            break

    if found is None:

        log(
            "SSHX executable not found."
        )

        return False

    try:

        shutil.copy2(
            found,
            SSHX,
        )

        SSHX.chmod(
            0o755
        )

    except Exception as e:

        log(
            f"SSHX installation failed: {e}"
        )

        return False

    SSHX_MARKER.touch()

    try:
        archive.unlink(
            missing_ok=True
        )
    except Exception:
        pass

    shutil.rmtree(
        extract,
        ignore_errors=True,
    )

    log(
        f"SSHX installed at {SSHX}"
    )

    return True


# ============================================================
# SSHX MANAGER
# ============================================================

class SSHXManager:

    def __init__(self):

        self.process = None

        self.url = None

        self.lock = threading.Lock()

    def start(self):

        with self.lock:

            if (
                self.process
                and self.process.poll()
                is None
            ):

                return

            if not install_sshx():

                return

            log(
                "Starting SSHX..."
            )

            self.url = None

            try:

                self.process = subprocess.Popen(
                    [
                        str(SSHX)
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )

            except Exception as e:

                log(
                    f"SSHX start error: {e}"
                )

                return

            threading.Thread(
                target=self.read_output,
                daemon=True,
            ).start()

    def read_output(self):

        process = self.process

        if not process:
            return

        try:

            for line in iter(
                process.stdout.readline,
                "",
            ):

                if not line:
                    break

                line = line.strip()

                if not line:
                    continue

                log(
                    "[SSHX] "
                    + line
                )

                matches = re.findall(
                    r"https?://[^\s]+",
                    line,
                )

                for url in matches:

                    url = url.rstrip(
                        ".,;)]}\"'"
                    )

                    if (
                        "sshx.io"
                        in url
                    ):

                        self.url = url

                        log(
                            "SSHX URL: "
                            + url
                        )

        except Exception as e:

            log(
                f"SSHX output error: {e}"
            )

    def alive(self):

        return (
            self.process is not None
            and self.process.poll()
            is None
        )


@st.cache_resource
def get_sshx():

    manager = SSHXManager()

    manager.start()

    return manager


# ============================================================
# INITIALIZATION
# ============================================================

def initialize():

    prepare_directories()

    arch = check_architecture()

    log(
        f"Architecture selected: {arch}"
    )

    # --------------------------------------------------------
    # Ubuntu
    # --------------------------------------------------------

    if not install_ubuntu():

        return False, "Ubuntu installation failed."

    # --------------------------------------------------------
    # PRoot
    # --------------------------------------------------------

    if not install_proot():

        return False, "PRoot installation failed."

    # --------------------------------------------------------
    # Test Ubuntu
    # --------------------------------------------------------

    if not test_ubuntu():

        return False, "Ubuntu/PRoot test failed."

    # --------------------------------------------------------
    # Ubuntu tools
    # --------------------------------------------------------

    if not prepare_ubuntu_tools():

        return False, (
            "Ubuntu package installation failed."
        )

    return True, "OK"


# ============================================================
# UI
# ============================================================

st.title(
    "🖥️ SHL Personal Server"
)

st.caption(
    "Ubuntu 22.04.5 + PRoot + SSHX"
)


# ============================================================
# BOOTSTRAP
# ============================================================

with st.status(
    "Preparing server...",
    expanded=True,
) as status:

    try:

        ok, message = initialize()

        if ok:

            status.update(
                label="Server ready",
                state="complete",
                expanded=False,
            )

        else:

            status.update(
                label=message,
                state="error",
                expanded=True,
            )

            st.stop()

    except Exception as e:

        status.update(
            label="Bootstrap failed",
            state="error",
            expanded=True,
        )

        st.exception(e)

        st.stop()


# ============================================================
# SERVER STATUS
# ============================================================

st.divider()

st.subheader(
    "Server status"
)

col1, col2, col3 = st.columns(3)

with col1:

    st.success(
        "🟢 Ubuntu / PRoot"
    )

with col2:

    if PROOT.exists():

        st.success(
            "🟢 PRoot"
        )

    else:

        st.error(
            "🔴 PRoot"
        )

with col3:

    st.success(
        "🟢 Runtime"
    )


# ============================================================
# SSHX
# ============================================================

st.divider()

st.subheader(
    "SSHX"
)

manager = get_sshx()

if manager.alive():

    st.success(
        "🟢 SSHX is running"
    )

else:

    st.error(
        "🔴 SSHX is not running"
    )


# Wait for URL.
for _ in range(20):

    if manager.url:
        break

    time.sleep(0.25)


if manager.url:

    st.success(
        "SSHX public URL:"
    )

    st.code(
        manager.url,
        language="text",
    )

    st.markdown(
        f"[🔗 Open SSHX terminal]({manager.url})"
    )

else:

    st.warning(
        "SSHX is running, but the public URL "
        "has not appeared yet. Refresh the page."
    )


# ============================================================
# UBUNTU TEST
# ============================================================

st.divider()

st.subheader(
    "Ubuntu test"
)

if st.button(
    "Test Ubuntu shell"
):

    result = ubuntu_command(
        """
        echo "================================"
        echo "SHL UBUNTU"
        echo "================================"
        echo "User: $(id -un)"
        echo "UID:  $(id -u)"
        echo "Arch: $(uname -m)"
        echo
        cat /etc/os-release
        echo
        echo "Disk:"
        df -h /
        echo
        echo "Memory:"
        free -h
        """
    )

    st.code(
        result.stdout or "",
        language="text",
    )


# ============================================================
# FUTURE SERVICES
# ============================================================

st.divider()

st.subheader(
    "Services"
)

st.info(
    """
Xray / Argo / SPMA will be installed inside
the Ubuntu rootfs in the next stage.

They are intentionally not started yet so that
the base Ubuntu + PRoot environment can be verified
first.
"""
)


# ============================================================
# DEBUG
# ============================================================

with st.expander(
    "Runtime information"
):

    st.code(
        "\n".join([
            f"Python: {sys.version}",
            f"Host OS: {platform.platform()}",
            f"Host architecture: {platform.machine()}",
            f"Runtime: {RUNTIME}",
            f"Ubuntu rootfs: {ROOTFS}",
            f"PRoot: {PROOT}",
            f"SSHX: {SSHX}",
            f"SSHX URL: {manager.url}",
        ]),
        language="text",
    )
