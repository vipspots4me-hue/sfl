import os
import re
import sys
import time
import tarfile
import shutil
import signal
import platform
import subprocess
import threading
import urllib.request
from pathlib import Path

import streamlit as st


# ============================================================
# CONFIG
# ============================================================

APP_DIR = Path.cwd()

# Everything generated at runtime goes here.
# It is NOT expected to survive Streamlit Reboot.
RUNTIME_DIR = Path("/tmp/shl-runtime")

FREEROOT_DIR = RUNTIME_DIR / "freeroot"
ROOTFS_DIR = FREEROOT_DIR

SSHX_DIR = Path.home() / ".local" / "bin"
SSHX_PATH = SSHX_DIR / "sshx"

SSHX_URL = (
    "https://s3.amazonaws.com/sshx/"
    "sshx-x86_64-unknown-linux-musl.tar.gz"
)

FREEROOT_URL = (
    "https://raw.githubusercontent.com/"
    "Mytai20100/freeroot/main/noninteractive.sh"
)

STATE_DIR = RUNTIME_DIR / "state"
LOG_DIR = RUNTIME_DIR / "logs"

LOCK_FILE = STATE_DIR / "bootstrap.lock"


# ============================================================
# HELPERS
# ============================================================

def log(msg):
    print(f"[SHL] {msg}", flush=True)


def command_exists(name):
    return shutil.which(name) is not None


def run_cmd(cmd, cwd=None, timeout=None, env=None):
    log("$ " + " ".join(map(str, cmd)))

    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )


def download(url, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    tmp = destination.with_suffix(destination.suffix + ".tmp")

    log(f"Downloading: {url}")

    urllib.request.urlretrieve(url, tmp)

    if not tmp.exists() or tmp.stat().st_size < 1024:
        raise RuntimeError(f"Download failed: {url}")

    tmp.replace(destination)


# ============================================================
# FREEROOT
# ============================================================

def prepare_freeroot():

    FREEROOT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    installed_marker = FREEROOT_DIR / ".installed"

    if installed_marker.exists():
        log("FreeRoot already installed in this runtime.")
        return True

    log("Installing FreeRoot...")

    script = FREEROOT_DIR / "noninteractive.sh"

    download(FREEROOT_URL, script)
    script.chmod(0o755)

    # FreeRoot expects to be executed from its own directory.
    result = run_cmd(
        ["sh", str(script)],
        cwd=FREEROOT_DIR,
        timeout=900,
    )

    output = result.stdout or ""

    (LOG_DIR / "freeroot-install.log").write_text(
        output,
        errors="ignore",
    )

    if result.returncode != 0:
        log("FreeRoot installation failed.")
        log(output[-5000:])
        return False

    # noninteractive.sh normally creates this itself.
    if not installed_marker.exists():
        installed_marker.touch()

    log("FreeRoot installation completed.")

    return True


# ============================================================
# PROOT COMMAND
# ============================================================

def proot_binary():

    candidates = [
        FREEROOT_DIR / "usr" / "local" / "bin" / "apk",
        FREEROOT_DIR / "proot-x86_64",
        FREEROOT_DIR / "proot-amd64",
    ]

    for path in candidates:
        if path.exists() and os.access(path, os.X_OK):
            return path

    return None


def root_command(command):

    proot = proot_binary()

    if not proot:
        raise RuntimeError("FreeRoot Proot binary not found.")

    config = FREEROOT_DIR / "usr" / "local" / ".config" / "proot.yml"

    env = os.environ.copy()
    env["PROOT_CONFIG"] = str(config)

    return subprocess.Popen(
        [str(proot), *command],
        cwd=str(FREEROOT_DIR),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


# ============================================================
# UBUNTU TEST
# ============================================================

@st.cache_resource
def start_ubuntu():

    log("Starting Ubuntu/FreeRoot...")

    process = root_command([
        "/bin/bash",
        "-lc",
        "echo 'SHL_UBUNTU_READY'; exec sleep infinity",
    ])

    return process


def ubuntu_alive(process):

    return (
        process is not None
        and process.poll() is None
    )


# ============================================================
# SSHX
# ============================================================

def install_sshx():

    SSHX_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if SSHX_PATH.exists():
        SSHX_PATH.chmod(0o755)
        return True

    archive = Path("/tmp/sshx.tar.gz")
    extract_dir = Path("/tmp/sshx_extract")

    try:

        log("Downloading SSHX...")

        urllib.request.urlretrieve(
            SSHX_URL,
            archive,
        )

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
            tar.extractall(extract_dir)

        found = None

        for p in extract_dir.rglob("sshx"):
            if p.is_file():
                found = p
                break

        if found is None:
            raise RuntimeError(
                "SSHX executable not found in archive."
            )

        shutil.copy2(
            found,
            SSHX_PATH,
        )

        SSHX_PATH.chmod(0o755)

        log(f"SSHX installed: {SSHX_PATH}")

        return True

    except Exception as e:

        log(f"SSHX installation error: {e}")

        return False

    finally:

        try:
            archive.unlink(
                missing_ok=True,
            )
        except Exception:
            pass


class SSHXManager:

    def __init__(self):

        self.process = None
        self.url = None
        self.lock = threading.Lock()

    def start(self):

        with self.lock:

            if (
                self.process
                and self.process.poll() is None
            ):
                return

            if not install_sshx():
                return

            log("Starting SSHX...")

            self.url = None

            self.process = subprocess.Popen(
                [str(SSHX_PATH)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                start_new_session=True,
            )

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

                log("[SSHX] " + line)

                urls = re.findall(
                    r"https?://[^\s]+",
                    line,
                )

                for url in urls:

                    url = url.rstrip(
                        ".,;)]}\"'"
                    )

                    if "sshx.io" in url:

                        self.url = url

                        log(
                            "SSHX URL: "
                            + url
                        )

        except Exception as e:

            log(
                f"SSHX reader error: {e}"
            )

    def alive(self):

        return (
            self.process is not None
            and self.process.poll() is None
        )


@st.cache_resource
def get_sshx():

    manager = SSHXManager()
    manager.start()

    return manager


# ============================================================
# SERVICE PLACEHOLDERS
# ============================================================

def service_status():

    """
    This is intentionally separated from FreeRoot.

    Xray / Argo / SPMA will be attached here after
    their exact existing commands/configuration are
    confirmed.

    We do NOT invent configurations or overwrite them.
    """

    return {
        "Ubuntu / FreeRoot": True,
        "SSHX": False,
        "Xray": False,
        "Argo": False,
        "SPMA": False,
    }


# ============================================================
# STREAMLIT UI
# ============================================================

st.set_page_config(
    page_title="SHL Server",
    page_icon="🖥️",
    layout="wide",
)

st.title("🖥️ SHL Runtime Server")

st.caption(
    "FreeRoot + Ubuntu + SSHX bootstrap"
)

# ------------------------------------------------------------
# FreeRoot
# ------------------------------------------------------------

with st.spinner(
    "Preparing Ubuntu / FreeRoot..."
):

    freeroot_ok = prepare_freeroot()

if not freeroot_ok:

    st.error(
        "FreeRoot installation failed."
    )

    st.stop()


ubuntu = start_ubuntu()

if ubuntu_alive(ubuntu):

    st.success(
        "Ubuntu / FreeRoot: RUNNING"
    )

else:

    st.error(
        "Ubuntu / FreeRoot: STOPPED"
    )


# ------------------------------------------------------------
# SSHX
# ------------------------------------------------------------

manager = get_sshx()

if manager.alive():

    st.success(
        "SSHX: RUNNING"
    )

else:

    st.error(
        "SSHX: STOPPED"
    )

url = manager.url

if url:

    st.subheader(
        "SSHX Public URL"
    )

    st.code(
        url,
        language="text",
    )

    st.markdown(
        f"[Open SSHX terminal]({url})"
    )

else:

    st.info(
        "Waiting for SSHX public URL..."
    )


# ------------------------------------------------------------
# STATUS
# ------------------------------------------------------------

st.divider()

st.subheader(
    "Server components"
)

status = service_status()

for name, running in status.items():

    if name == "SSHX":
        running = manager.alive()

    if name == "Ubuntu / FreeRoot":
        running = ubuntu_alive(ubuntu)

    if running:
        st.write(
            f"🟢 {name}: RUNNING"
        )
    else:
        st.write(
            f"⚪ {name}: NOT CONFIGURED / STOPPED"
        )


# ------------------------------------------------------------
# INFORMATION
# ------------------------------------------------------------

st.divider()

st.info(
    """
Streamlit Community Cloud runtime is ephemeral.

This application therefore rebuilds its runtime environment
automatically after a fresh deployment/reboot.

Project source remains in GitHub.
Runtime-generated files are not treated as permanent storage.
"""
)


# ------------------------------------------------------------
# DEBUG
# ------------------------------------------------------------

with st.expander("Runtime information"):

    st.code(
        "\n".join([
            f"Python: {sys.version}",
            f"Platform: {platform.platform()}",
            f"Architecture: {platform.machine()}",
            f"Working directory: {APP_DIR}",
            f"Runtime directory: {RUNTIME_DIR}",
            f"FreeRoot directory: {FREEROOT_DIR}",
            f"SSHX: {SSHX_PATH}",
        ]),
        language="text",
    )
