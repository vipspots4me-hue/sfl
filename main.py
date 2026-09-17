```python
import os
import re
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
# SHL - SELF REBUILDING UBUNTU / PROOT / SSHX
# ============================================================

BASE_DIR = Path("/tmp/shl-runtime")
ROOTFS_DIR = BASE_DIR / "ubuntu"
PROOT_DIR = BASE_DIR / "proot"
PROOT_PATH = PROOT_DIR / "proot"

LOCK_FILE = BASE_DIR / ".bootstrap.lock"
STATE_FILE = BASE_DIR / ".bootstrap.ok"

UBUNTU_VERSION = "22.04.5"

UBUNTU_URL = (
    "https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/"
    "ubuntu-base-22.04.5-base-amd64.tar.gz"
)

PROOT_URL = (
    "https://github.com/Mytai20100/freeproot/releases/latest/download/"
    "proot-amd64"
)

SSHX_DIR = Path.home() / ".local" / "bin"
SSHX_PATH = SSHX_DIR / "sshx"

SSHX_URL = (
    "https://s3.amazonaws.com/sshx/"
    "sshx-x86_64-unknown-linux-musl.tar.gz"
)


_bootstrap_lock = threading.Lock()
_sshx_process = None
_sshx_link = None


# ============================================================
# LOG
# ============================================================

def log(message):
    print(f"[SHL] {message}", flush=True)


# ============================================================
# COMMAND
# ============================================================

def run_command(command, cwd=None, timeout=None, env=None):
    command = [str(x) for x in command]

    log("$ " + " ".join(command))

    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


# ============================================================
# DOWNLOAD
# ============================================================

def download_file(url, destination, label):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    temp = destination.with_name(destination.name + ".part")

    if temp.exists():
        temp.unlink()

    log(f"Downloading {label}: {url}")

    def progress(block_count, block_size, total_size):
        if total_size > 0:
            percent = min(
                100.0,
                block_count * block_size * 100.0 / total_size,
            )

            print(
                f"[SHL] {label}: {percent:.1f}%",
                flush=True,
            )

    urllib.request.urlretrieve(
        url,
        temp,
        progress,
    )

    if not temp.exists():
        raise RuntimeError(
            f"{label}: download produced no file"
        )

    size = temp.stat().st_size

    if size <= 0:
        temp.unlink(missing_ok=True)

        raise RuntimeError(
            f"{label}: downloaded file is empty"
        )

    temp.replace(destination)

    return destination


# ============================================================
# ARCH
# ============================================================

def detect_arch():
    machine = platform.machine().lower()

    if machine in ("x86_64", "amd64"):
        arch = "amd64"

    elif machine in ("aarch64", "arm64"):
        arch = "arm64"

    else:
        raise RuntimeError(
            f"Unsupported architecture: {machine}"
        )

    log(f"Detected architecture: {machine}")
    log(f"Architecture selected: {arch}")

    return arch


# ============================================================
# LOCK
# ============================================================

def acquire_bootstrap_lock():
    BASE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    for _ in range(180):

        try:
            fd = os.open(
                LOCK_FILE,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY,
            )

            os.write(
                fd,
                str(os.getpid()).encode(),
            )

            os.close(fd)

            return True

        except FileExistsError:
            time.sleep(0.5)

    return False


def release_bootstrap_lock():
    try:
        LOCK_FILE.unlink()

    except FileNotFoundError:
        pass


# ============================================================
# SAFE TAR EXTRACTION
# ============================================================

def safe_extract(tar_path, destination):
    destination = Path(destination).resolve()

    with tarfile.open(
        tar_path,
        "r:gz",
    ) as archive:

        for member in archive.getmembers():

            target = (
                destination / member.name
            ).resolve()

            if not str(target).startswith(
                str(destination) + os.sep
            ):
                raise RuntimeError(
                    "Unsafe archive path detected"
                )

        archive.extractall(destination)


# ============================================================
# UBUNTU ROOTFS
# ============================================================

def install_ubuntu_rootfs():

    BASE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if (
        ROOTFS_DIR.exists()
        and (ROOTFS_DIR / "bin/bash").exists()
    ):
        log("Ubuntu rootfs already exists.")
        return True

    log("Ubuntu rootfs is not installed.")

    archive = (
        BASE_DIR
        / f"ubuntu-{UBUNTU_VERSION}.tar.gz"
    )

    extracting = (
        BASE_DIR
        / "ubuntu.extracting"
    )

    try:

        if archive.exists():
            if archive.stat().st_size <= 0:
                archive.unlink()

        download_file(
            UBUNTU_URL,
            archive,
            f"Ubuntu {UBUNTU_VERSION}",
        )

        if extracting.exists():
            shutil.rmtree(
                extracting,
                ignore_errors=True,
            )

        extracting.mkdir(
            parents=True,
            exist_ok=True,
        )

        log("Extracting Ubuntu rootfs...")

        safe_extract(
            archive,
            extracting,
        )

        bash_path = (
            extracting / "bin/bash"
        )

        if not bash_path.exists():
            raise RuntimeError(
                "Ubuntu rootfs extraction incomplete"
            )

        if ROOTFS_DIR.exists():
            shutil.rmtree(
                ROOTFS_DIR,
                ignore_errors=True,
            )

        extracting.rename(
            ROOTFS_DIR
        )

        # ----------------------------------------------------
        # DNS
        # ----------------------------------------------------

        resolv_conf = (
            ROOTFS_DIR
            / "etc/resolv.conf"
        )

        resolv_conf.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        try:
            if (
                resolv_conf.is_symlink()
                or resolv_conf.exists()
            ):
                resolv_conf.unlink()

        except Exception:
            pass

        resolv_conf.write_text(
            "nameserver 1.1.1.1\n"
            "nameserver 1.0.0.1\n"
        )

        # ----------------------------------------------------
        # HOSTNAME
        # ----------------------------------------------------

        (
            ROOTFS_DIR / "etc/hostname"
        ).write_text(
            "shl-ubuntu\n"
        )

        # ----------------------------------------------------
        # HOSTS
        # ----------------------------------------------------

        (
            ROOTFS_DIR / "etc/hosts"
        ).write_text(
            "127.0.0.1 localhost\n"
            "127.0.1.1 shl-ubuntu\n"
            "::1 localhost ip6-localhost "
            "ip6-loopback\n"
        )

        log(
            "Ubuntu rootfs installed successfully."
        )

        return True

    except Exception as error:

        log(
            f"Ubuntu rootfs installation failed: "
            f"{error}"
        )

        if extracting.exists():
            shutil.rmtree(
                extracting,
                ignore_errors=True,
            )

        if archive.exists():
            try:
                if archive.stat().st_size <= 0:
                    archive.unlink()
            except Exception:
                pass

        return False


# ============================================================
# PROOT
# ============================================================

def install_proot():

    if (
        PROOT_PATH.exists()
        and os.access(
            PROOT_PATH,
            os.X_OK,
        )
    ):
        log("PRoot already installed.")
        return True

    PROOT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp = (
        PROOT_DIR
        / "proot.part"
    )

    if temp.exists():
        temp.unlink()

    try:

        download_file(
            PROOT_URL,
            temp,
            "PRoot amd64",
        )

        if temp.stat().st_size < 100000:
            raise RuntimeError(
                "PRoot binary is unexpectedly small"
            )

        temp.chmod(0o755)

        temp.replace(
            PROOT_PATH
        )

        test = run_command(
            [
                PROOT_PATH,
                "--help",
            ],
            timeout=30,
        )

        if test.returncode not in (0, 1):
            raise RuntimeError(
                "PRoot executable test failed\n"
                + test.stdout
            )

        log(
            "PRoot installed successfully."
        )

        return True

    except Exception as error:

        log(
            f"PRoot installation failed: "
            f"{error}"
        )

        temp.unlink(
            missing_ok=True
        )

        return False


# ============================================================
# UBUNTU COMMAND
# ============================================================

def ubuntu_command(
    command,
    timeout=None,
):

    if not ROOTFS_DIR.exists():
        raise RuntimeError(
            "Ubuntu rootfs does not exist"
        )

    if not PROOT_PATH.exists():
        raise RuntimeError(
            "PRoot does not exist"
        )

    command = str(command)

    cmd = [
        PROOT_PATH,

        "-r",
        ROOTFS_DIR,

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
        f"{ROOTFS_DIR}/etc/resolv.conf:"
        "/etc/resolv.conf",

        "/bin/bash",

        "-lc",

        command,
    ]

    return run_command(
        cmd,
        timeout=timeout,
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
""",
        timeout=60,
    )

    print(
        result.stdout,
        end="",
        flush=True,
    )

    if (
        result.returncode != 0
        or "SHL_UBUNTU_OK"
        not in result.stdout
    ):
        raise RuntimeError(
            "Ubuntu/PRoot test failed"
        )

    return True


# ============================================================
# UBUNTU TOOLS
# ============================================================

def install_ubuntu_tools():

    marker = (
        ROOTFS_DIR
        / "root/.shl_tools_ready"
    )

    if marker.exists():
        log(
            "Ubuntu basic tools already installed."
        )
        return True

    log(
        "Installing basic Ubuntu tools..."
    )

    command = """
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C
export LANG=C

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
    python3-pip \
    git \
    openssl \
    netcat-openbsd

mkdir -p /root

touch /root/.shl_tools_ready
"""

    result = ubuntu_command(
        command,
        timeout=900,
    )

    print(
        result.stdout,
        end="",
        flush=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Ubuntu tools installation failed"
        )

    log(
        "Ubuntu tools installed."
    )

    return True


# ============================================================
# SSHX FIND
# ============================================================

def find_sshx():

    candidates = [
        SSHX_PATH,
        Path("/usr/local/bin/sshx"),
        Path("/usr/bin/sshx"),
        Path("/root/.local/bin/sshx"),
    ]

    for path in candidates:

        if (
            path.exists()
            and os.access(
                path,
                os.X_OK,
            )
        ):
            return path

    found = shutil.which("sshx")

    if found:
        return Path(found)

    return None


# ============================================================
# SSHX INSTALL
# ============================================================

def install_sshx():

    global SSHX_PATH

    existing = find_sshx()

    if existing:

        SSHX_PATH = existing

        log(
            f"SSHX already installed: "
            f"{SSHX_PATH}"
        )

        return SSHX_PATH

    SSHX_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    archive = (
        BASE_DIR / "sshx.tar.gz"
    )

    extract_dir = (
        BASE_DIR / "sshx-extract"
    )

    try:

        download_file(
            SSHX_URL,
            archive,
            "SSHX",
        )

        if extract_dir.exists():
            shutil.rmtree(
                extract_dir,
                ignore_errors=True,
            )

        extract_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        with tarfile.open(
            archive,
            "r:gz",
        ) as tar:
            tar.extractall(
                extract_dir
            )

        binary = None

        for path in extract_dir.rglob(
            "sshx"
        ):

            if path.is_file():
                binary = path
                break

        if binary is None:
            raise RuntimeError(
                "sshx binary not found"
            )

        shutil.copy2(
            binary,
            SSHX_PATH,
        )

        SSHX_PATH.chmod(
            0o755
        )

        log(
            f"SSHX installed: "
            f"{SSHX_PATH}"
        )

        return SSHX_PATH

    except Exception as error:

        log(
            f"SSHX installation failed: "
            f"{error}"
        )

        raise


# ============================================================
# SSHX START
# ============================================================

def start_sshx():

    global _sshx_process
    global _sshx_link

    with _bootstrap_lock:

        if (
            _sshx_process is not None
            and _sshx_process.poll() is None
        ):
            return _sshx_link

        path = install_sshx()

        log("Starting SSHX...")

        env = os.environ.copy()

        env["PATH"] = (
            f"{path.parent}:"
            f"{env.get('PATH', '')}"
        )

        _sshx_process = subprocess.Popen(
            [
                str(path),
                "--quiet",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
        )

        link = None

        start_time = time.time()

        while (
            time.time() - start_time
            < 45
        ):

            if (
                _sshx_process.poll()
                is not None
            ):
                break

            line = (
                _sshx_process.stdout.readline()
            )

            if not line:
                time.sleep(0.1)
                continue

            line = line.rstrip()

            log(
                "[SSHX] " + line
            )

            match = re.search(
                r"https://sshx\.io/s/"
                r"[A-Za-z0-9_-]+#"
                r"[A-Za-z0-9_-]+",
                line,
            )

            if match:
                link = match.group(0)
                break

        if not link:
            raise RuntimeError(
                "SSHX URL was not generated"
            )

        _sshx_link = link

        log(
            f"SSHX URL: {_sshx_link}"
        )

        return _sshx_link


# ============================================================
# BOOTSTRAP
# ============================================================

def bootstrap():

    # --------------------------------------------------------
    # Already initialized
    # --------------------------------------------------------

    if (
        STATE_FILE.exists()
        and ROOTFS_DIR.exists()
        and PROOT_PATH.exists()
    ):

        log(
            "Existing SHL bootstrap detected."
        )

        try:
            test_ubuntu()
            install_ubuntu_tools()

            return True

        except Exception as error:

            log(
                "Existing bootstrap is invalid: "
                f"{error}"
            )

            STATE_FILE.unlink(
                missing_ok=True
            )

    # --------------------------------------------------------
    # Lock
    # --------------------------------------------------------

    if not acquire_bootstrap_lock():

        log(
            "Another bootstrap process is "
            "running. Waiting..."
        )

        for _ in range(180):

            if (
                STATE_FILE.exists()
                and ROOTFS_DIR.exists()
                and PROOT_PATH.exists()
            ):
                return True

            time.sleep(1)

        raise RuntimeError(
            "Bootstrap lock timeout"
        )

    try:

        # ----------------------------------------------------
        # Double check after acquiring lock
        # ----------------------------------------------------

        if (
            STATE_FILE.exists()
            and ROOTFS_DIR.exists()
            and PROOT_PATH.exists()
        ):
            return True

        # ----------------------------------------------------
        # Architecture
        # ----------------------------------------------------

        arch = detect_arch()

        if arch != "amd64":
            raise RuntimeError(
                "This version supports amd64 only."
            )

        # ----------------------------------------------------
        # Ubuntu
        # ----------------------------------------------------

        if not install_ubuntu_rootfs():
            raise RuntimeError(
                "Ubuntu rootfs installation failed"
            )

        # ----------------------------------------------------
        # PRoot
        # ----------------------------------------------------

        if not install_proot():
            raise RuntimeError(
                "PRoot installation failed"
            )

        # ----------------------------------------------------
        # Test
        # ----------------------------------------------------

        test_ubuntu()

        # ----------------------------------------------------
        # Tools
        # ----------------------------------------------------

        install_ubuntu_tools()

        # ----------------------------------------------------
        # State
        # ----------------------------------------------------

        STATE_FILE.write_text(
            "SHL_BOOTSTRAP_OK\n"
            f"pid={os.getpid()}\n"
            f"time={time.time()}\n"
        )

        log(
            "SHL bootstrap completed."
        )

        return True

    finally:

        release_bootstrap_lock()


# ============================================================
# STREAMLIT
# ============================================================

st.set_page_config(
    page_title="SHL Server",
    page_icon="🖥️",
    layout="wide",
)


st.title("SHL Server")


# ============================================================
# BOOTSTRAP
# ============================================================

if "bootstrap_done" not in st.session_state:

    st.session_state.bootstrap_done = False


if not st.session_state.bootstrap_done:

    try:

        with st.spinner(
            "Starting SHL runtime..."
        ):

            bootstrap()

        st.session_state.bootstrap_done = True

        st.success(
            "Ubuntu + PRoot آماده است."
        )

    except Exception as error:

        st.error(
            f"Bootstrap error: {error}"
        )

        st.code(
            "\n".join(
                [
                    f"BASE_DIR={BASE_DIR}",
                    f"ROOTFS_DIR={ROOTFS_DIR}",
                    f"PROOT_PATH={PROOT_PATH}",
                ]
            )
        )

        st.stop()


# ============================================================
# SSHX
# ============================================================

try:

    link = start_sshx()

    st.subheader("SSHX")

    st.code(link)

    st.markdown(
        f"### [Open SSHX]({link})"
    )

except Exception as error:

    st.error(
        f"SSHX error: {error}"
    )


# ============================================================
# UBUNTU TEST
# ============================================================

st.subheader("Ubuntu")


col1, col2 = st.columns(2)


with col1:

    if st.button(
        "Test Ubuntu",
        use_container_width=True,
    ):

        result = ubuntu_command(
            """
echo "USER=$(id -un)"
echo "UID=$(id -u)"
echo "OS=$(grep PRETTY_NAME /etc/os-release 2>/dev/null)"
echo "ARCH=$(uname -m)"
echo
echo "ROOT:"
df -h /
echo
echo "MEMORY:"
free -h
""",
            timeout=60,
        )

        st.code(
            result.stdout
        )


with col2:

    if st.button(
        "Restart SSHX",
        use_container_width=True,
    ):

        if (
            _sshx_process is not None
        ):

            try:
                _sshx_process.terminate()

            except Exception:
                pass

        _sshx_process = None
        _sshx_link = None

        st.rerun()


# ============================================================
# STATUS
# ============================================================

st.divider()

st.caption(
    f"Ubuntu: {ROOTFS_DIR}"
)

st.caption(
    f"PRoot: {PROOT_PATH}"
)

st.caption(
    f"Architecture: {platform.machine()}"
)
```
