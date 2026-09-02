import concurrent.futures
import os
import re
import pandas as pd
from netmiko import ConnectHandler, NetMikoTimeoutException, NetMikoAuthenticationException

# Directory where backup txt files will be stored
BACKUP_DIR = "switch_backups"
os.makedirs(BACKUP_DIR, exist_ok=True)

# Load target switch IPs from djayback.csv
INPUT_CSV = "djayback.csv"
switches_df = pd.read_csv(INPUT_CSV)
DEVICE_LIST = switches_df.to_dict(orient="records")

# List of credential pairs to try automatically for each switch
CREDENTIAL_FALLBACKS = [
    {"username": "noc", "password": "Global@noc"},
    {"username": "noc", "password": "Global@n0c"},
    {"username": "global", "password": "Global@noc"},
    {"username": "Global", "password": "Global@noc"},
    {"username": "global", "password": "Global@n0c"},
    {"username": "Global", "password": "Global@n0c"},
]


def backup_single_switch(ip, initial_user, initial_pass):
    # Prepare credentials list: start with CSV credentials, then fallbacks
    creds_to_try = [{"username": initial_user, "password": initial_pass}]
    for cred in CREDENTIAL_FALLBACKS:
        if cred not in creds_to_try:
            creds_to_try.append(cred)

    net_connect = None
    connected_user = None

    # Try credentials until successful login
    for cred in creds_to_try:
        device = {
            "device_type": "huawei_telnet",
            "ip": ip,
            "username": cred["username"],
            "password": cred["password"],
            "port": 23,
            "timeout": 20,
            "global_delay_factor": 2,
        }
        try:
            net_connect = ConnectHandler(**device)
            connected_user = cred["username"]
            break  # Connection successful!
        except (NetMikoAuthenticationException, NetMikoTimeoutException):
            continue  # Try next credential set if auth fails
        except Exception:
            continue

    if not net_connect:
        print(f"[-] {ip} - Connection/Authentication Failed (IP unreachable or wrong password)")
        return False

    try:
        # 1. Handle Prompt Warnings & Password Renewal ('N' option)
        initial_output = net_connect.send_command("", expect_string=r"[>\]]")
        if any(
            phrase in initial_output
            for phrase in ["Change now", "Please choose", "password"]
        ):
            net_connect.send_command_timing("N")

        # 2. Extract Switch Name for File Naming
        prompt = net_connect.find_prompt()
        switch_name = re.sub(r"[<>]|\[|\]", "", prompt).strip()

        # Fallback to sysname if prompt is default "Huawei" or "Switch"
        if switch_name.lower() in ["huawei", "switch", ""]:
            sysname_output = net_connect.send_command(
                "display current-configuration | include sysname"
            )
            sys_match = re.search(r"sysname\s+(\S+)", sysname_output)
            if sys_match:
                switch_name = sys_match.group(1).strip()
            else:
                switch_name = f"Switch_{ip}"

        # Sanitize filename characters
        safe_switch_name = re.sub(r'[\\/*?:"<>|]', "_", switch_name)

        # 3. Disable CLI output pagination (continuous text stream)
        net_connect.send_command("screen-length 0 temporary")

        # 4. Fetch Descriptions & Configuration
        print(f"[*] Backing up {switch_name} ({ip}) using user '{connected_user}'...")
        desc_output = net_connect.send_command("display interface description")
        config_output = net_connect.send_command("display current-configuration")

        net_connect.disconnect()

        # 5. Format Document Content
        backup_content = f"""====================================================================
SWITCH NAME: {switch_name}
SWITCH IP  : {ip}
AUTHENTICATED AS: {connected_user}
====================================================================

--------------------------------------------------------------------
1. INTERFACE DESCRIPTIONS (display interface description)
--------------------------------------------------------------------
{desc_output}

--------------------------------------------------------------------
2. CURRENT CONFIGURATION (display current-configuration)
--------------------------------------------------------------------
{config_output}
"""

        # 6. Save directly named after Switch Name (.txt)
        file_path = os.path.join(BACKUP_DIR, f"{safe_switch_name}.txt")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(backup_content)

        print(f"[✔] Successfully saved: {file_path}")
        return True

    except Exception as err:
        print(f"[-] Error parsing data from {ip}: {str(err)}")
        if net_connect:
            net_connect.disconnect()
        return False


# -------------------------------------------------------------
# Execute Parallel Backup Tasks (15 Threads concurrently)
# -------------------------------------------------------------
print(f"[*] Starting backup job for {len(DEVICE_LIST)} switches from '{INPUT_CSV}'...\n")

with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
    futures = [
        executor.submit(
            backup_single_switch, dev["ip"], dev["username"], dev["password"]
        )
        for dev in DEVICE_LIST
    ]
    results = [future.result() for future in concurrent.futures.as_completed(futures)]

success_count = sum(1 for r in results if r)
print(f"\n[✔] Backup Complete!")
print(f"[+] Successfully backed up: {success_count}/{len(DEVICE_LIST)} switches.")
print(f"[+] All txt files saved in: '{os.path.abspath(BACKUP_DIR)}/'")