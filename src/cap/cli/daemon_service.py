"""CAP Daemon OS Service Management.

Provides install/uninstall of the CAP daemon as a system service:
  - macOS: LaunchAgent plist (~/.config/LaunchAgents/ or ~/Library/LaunchAgents/)
  - Linux: systemd user service (~/.config/systemd/user/)

The service runs: python -m cap.harness.daemon
Auto-restarts on crash. Starts on login.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path
from textwrap import dedent


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICE_LABEL = "com.cap.daemon"
SERVICE_DESCRIPTION = "CAP Platform Background Daemon"

# Determine Python executable that should run the daemon
_PYTHON = sys.executable or shutil.which("python3") or "python3"


def _cap_home() -> Path:
    from cap.config import get_cap_home
    return get_cap_home()


# ---------------------------------------------------------------------------
# macOS: LaunchAgent plist
# ---------------------------------------------------------------------------

def _launchagent_dir() -> Path:
    """Return the LaunchAgents directory for the current user."""
    return Path.home() / "Library" / "LaunchAgents"


def _launchagent_path() -> Path:
    return _launchagent_dir() / f"{SERVICE_LABEL}.plist"


def _generate_plist() -> str:
    """Generate a LaunchAgent plist XML for the CAP daemon."""
    cap_home = _cap_home()
    log_path = cap_home / "logs" / "daemon.log"
    err_path = cap_home / "logs" / "daemon.err.log"

    return dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{SERVICE_LABEL}</string>

            <key>ProgramArguments</key>
            <array>
                <string>{_PYTHON}</string>
                <string>-m</string>
                <string>cap.harness.daemon</string>
            </array>

            <key>RunAtLoad</key>
            <true/>

            <key>KeepAlive</key>
            <dict>
                <key>SuccessfulExit</key>
                <false/>
            </dict>

            <key>ThrottleInterval</key>
            <integer>10</integer>

            <key>StandardOutPath</key>
            <string>{log_path}</string>

            <key>StandardErrorPath</key>
            <string>{err_path}</string>

            <key>EnvironmentVariables</key>
            <dict>
                <key>CAP_HOME</key>
                <string>{cap_home}</string>
                <key>PATH</key>
                <string>{os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}</string>
            </dict>

            <key>ProcessType</key>
            <string>Background</string>
        </dict>
        </plist>
    """)


def _install_macos() -> str:
    """Install LaunchAgent plist for macOS."""
    plist_dir = _launchagent_dir()
    plist_dir.mkdir(parents=True, exist_ok=True)

    # Ensure log directory exists
    log_dir = _cap_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    plist_path = _launchagent_path()
    plist_path.write_text(_generate_plist())

    # Load the service
    os.system(f"launchctl load -w {plist_path}")
    return str(plist_path)


def _uninstall_macos() -> bool:
    """Uninstall the LaunchAgent plist."""
    plist_path = _launchagent_path()
    if not plist_path.exists():
        return False

    os.system(f"launchctl unload -w {plist_path}")
    plist_path.unlink(missing_ok=True)
    return True


def _is_installed_macos() -> bool:
    """Check if the LaunchAgent plist exists."""
    return _launchagent_path().exists()


# ---------------------------------------------------------------------------
# Linux: systemd user service
# ---------------------------------------------------------------------------

def _systemd_dir() -> Path:
    """Return the systemd user service directory."""
    return Path.home() / ".config" / "systemd" / "user"


def _systemd_path() -> Path:
    return _systemd_dir() / "cap-daemon.service"


def _generate_systemd_unit() -> str:
    """Generate a systemd user service unit file."""
    cap_home = _cap_home()

    return dedent(f"""\
        [Unit]
        Description={SERVICE_DESCRIPTION}
        After=default.target

        [Service]
        Type=simple
        ExecStart={_PYTHON} -m cap.harness.daemon
        Restart=on-failure
        RestartSec=10
        Environment="CAP_HOME={cap_home}"
        Environment="PATH={os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"

        [Install]
        WantedBy=default.target
    """)


def _install_linux() -> str:
    """Install systemd user service."""
    service_dir = _systemd_dir()
    service_dir.mkdir(parents=True, exist_ok=True)

    # Ensure log/run directories exist
    (_cap_home() / "logs").mkdir(parents=True, exist_ok=True)
    (_cap_home() / "run").mkdir(parents=True, exist_ok=True)

    service_path = _systemd_path()
    service_path.write_text(_generate_systemd_unit())

    # Reload and enable
    os.system("systemctl --user daemon-reload")
    os.system("systemctl --user enable cap-daemon.service")
    return str(service_path)


def _uninstall_linux() -> bool:
    """Uninstall systemd user service."""
    service_path = _systemd_path()
    if not service_path.exists():
        return False

    os.system("systemctl --user stop cap-daemon.service")
    os.system("systemctl --user disable cap-daemon.service")
    service_path.unlink(missing_ok=True)
    os.system("systemctl --user daemon-reload")
    return True


def _is_installed_linux() -> bool:
    """Check if the systemd service file exists."""
    return _systemd_path().exists()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install_service() -> str:
    """Install the daemon as an OS service.

    Returns the path to the installed service file.
    Raises RuntimeError on unsupported platforms.
    """
    system = platform.system()
    if system == "Darwin":
        return _install_macos()
    elif system == "Linux":
        return _install_linux()
    else:
        raise RuntimeError(f"Unsupported platform for service install: {system}")


def uninstall_service() -> bool:
    """Uninstall the daemon OS service.

    Returns True if service was found and removed, False if not installed.
    """
    system = platform.system()
    if system == "Darwin":
        return _uninstall_macos()
    elif system == "Linux":
        return _uninstall_linux()
    else:
        raise RuntimeError(f"Unsupported platform for service uninstall: {system}")


def is_service_installed() -> bool:
    """Check whether the daemon OS service is installed."""
    system = platform.system()
    if system == "Darwin":
        return _is_installed_macos()
    elif system == "Linux":
        return _is_installed_linux()
    else:
        return False


def service_file_path() -> Path | None:
    """Return the path to the service file, or None if unsupported."""
    system = platform.system()
    if system == "Darwin":
        return _launchagent_path()
    elif system == "Linux":
        return _systemd_path()
    return None
