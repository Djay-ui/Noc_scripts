#!/usr/bin/env python3
"""
backup_mikrotik.py

Automates backups of a list of MikroTik RouterOS devices over SSH/SFTP.

For each router this script will:
  1. Connect over SSH (paramiko)
  2. Run `/system backup save` to create a binary .backup file
  3. Run `/export` to create a human-readable .rsc config export
  4. Download both files locally via SFTP (RouterOS supports SFTP natively)
  5. Optionally remove the backup files from the router afterwards
  6. Log a summary of successes/failures

Usage:
    python3 backup_mikrotik.py --devices devices.csv --outdir ./backups

Device CSV format (header required):
    name,host,port,username,password

    name      - a friendly identifier used in local filenames/folders
    host      - IP address or hostname of the router
    port      - SSH port (usually 22)
    username  - RouterOS username (needs ssh + read/write ftp policy)
    password  - RouterOS password

SECURITY NOTE:
    Storing plaintext passwords in a CSV is convenient but risky.
    At minimum:
      - chmod 600 devices.csv
      - keep it out of version control (.gitignore it)
      - consider SSH key auth instead (see --use-keys) or a secrets
        manager / vault for production use.
"""

import argparse
import csv
import datetime
import logging
import os
import sys
import time
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("Missing dependency. Install with: pip install paramiko --break-system-packages")
    sys.exit(1)


# --------------------------------------------------------------------------
# Logging setup
# --------------------------------------------------------------------------
def setup_logging(outdir: Path) -> logging.Logger:
    outdir.mkdir(parents=True, exist_ok=True)
    log_file = outdir / f"backup_run_{datetime.datetime.now():%Y%m%d_%H%M%S}.log"

    logger = logging.getLogger("mikrotik_backup")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_file)
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# --------------------------------------------------------------------------
# Device list loading
# --------------------------------------------------------------------------
def load_devices(csv_path: Path):
    devices = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        required = {"name", "host", "username", "password"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(
                f"CSV must contain columns: {required}. Found: {reader.fieldnames}"
            )
        for row in reader:
            row["port"] = int(row.get("port") or 22)
            devices.append(row)
    return devices


# --------------------------------------------------------------------------
# Core backup logic for a single router
# --------------------------------------------------------------------------
def backup_device(device: dict, outdir: Path, logger: logging.Logger,
                   timeout: int, keep_remote: bool, key_path: str = None) -> bool:
    name = device["name"]
    host = device["host"]
    port = device["port"]
    username = device["username"]
    password = device.get("password") or None

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"{name}_{stamp}.backup"
    export_filename = f"{name}_{stamp}.rsc"

    device_outdir = outdir / name
    device_outdir.mkdir(parents=True, exist_ok=True)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    logger.info(f"[{name}] Connecting to {host}:{port} ...")
    try:
        connect_kwargs = dict(
            hostname=host,
            port=port,
            username=username,
            timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        if key_path:
            connect_kwargs["key_filename"] = key_path
        else:
            connect_kwargs["password"] = password

        client.connect(**connect_kwargs)
    except Exception as e:
        logger.error(f"[{name}] SSH connection failed: {e}")
        return False

    try:
        # 1. Create binary backup on the router
        cmd = f"/system backup save name={backup_filename.replace('.backup', '')}"
        logger.info(f"[{name}] Running: {cmd}")
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        stdout.channel.recv_exit_status()  # wait for completion
        err = stderr.read().decode(errors="ignore").strip()
        if err:
            logger.warning(f"[{name}] backup save stderr: {err}")

        # 2. Create readable config export
        cmd = f"/export file={export_filename.replace('.rsc', '')}"
        logger.info(f"[{name}] Running: {cmd}")
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        stdout.channel.recv_exit_status()
        err = stderr.read().decode(errors="ignore").strip()
        if err:
            logger.warning(f"[{name}] export stderr: {err}")

        # Give RouterOS a moment to flush files to disk
        time.sleep(2)

        # 3. Download both files via SFTP
        sftp = client.open_sftp()
        remote_files = sftp.listdir(".")

        downloaded = []
        for remote_name in (backup_filename, export_filename):
            if remote_name in remote_files:
                local_path = device_outdir / remote_name
                sftp.get(remote_name, str(local_path))
                logger.info(f"[{name}] Downloaded {remote_name} -> {local_path}")
                downloaded.append(remote_name)
            else:
                logger.warning(f"[{name}] Expected file not found on router: {remote_name}")

        # 4. Optionally clean up remote copies to avoid filling router storage
        if not keep_remote:
            for remote_name in downloaded:
                try:
                    sftp.remove(remote_name)
                    logger.info(f"[{name}] Removed remote copy: {remote_name}")
                except Exception as e:
                    logger.warning(f"[{name}] Could not remove remote {remote_name}: {e}")

        sftp.close()

        if len(downloaded) == 0:
            logger.error(f"[{name}] No files were downloaded — treating as failure.")
            return False

        logger.info(f"[{name}] Backup completed successfully.")
        return True

    except Exception as e:
        logger.error(f"[{name}] Backup failed: {e}")
        return False
    finally:
        client.close()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Automate MikroTik RouterOS backups over SSH/SFTP.")
    parser.add_argument("--devices", required=True, help="Path to devices CSV file")
    parser.add_argument("--outdir", default="./backups", help="Local directory to store backups")
    parser.add_argument("--timeout", type=int, default=30, help="SSH command timeout in seconds")
    parser.add_argument("--keep-remote", action="store_true",
                         help="Keep backup/export files on the router after download (default: delete them)")
    parser.add_argument("--use-keys", metavar="KEY_PATH",
                         help="Use SSH key auth instead of the password column (path to private key)")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    logger = setup_logging(outdir)

    csv_path = Path(args.devices)
    if not csv_path.exists():
        logger.error(f"Devices file not found: {csv_path}")
        sys.exit(1)

    devices = load_devices(csv_path)
    logger.info(f"Loaded {len(devices)} device(s) from {csv_path}")

    results = {}
    for device in devices:
        success = backup_device(
            device, outdir, logger,
            timeout=args.timeout,
            keep_remote=args.keep_remote,
            key_path=args.use_keys,
        )
        results[device["name"]] = success

    # Summary
    logger.info("=" * 50)
    logger.info("BACKUP SUMMARY")
    logger.info("=" * 50)
    ok = [n for n, s in results.items() if s]
    failed = [n for n, s in results.items() if not s]
    logger.info(f"Succeeded ({len(ok)}): {', '.join(ok) if ok else '-'}")
    logger.info(f"Failed    ({len(failed)}): {', '.join(failed) if failed else '-'}")

    if failed:
        sys.exit(2)


if __name__ == "__main__":
    main()
