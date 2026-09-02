import concurrent.futures
import re
import pandas as pd
from netmiko import ConnectHandler, NetMikoTimeoutException

# Load target switch IPs from CSV
switches_df = pd.read_csv("switches.csv")
DEVICE_LIST = switches_df.to_dict(orient="records")


def parse_huawei_switch(ip, username, password):
    device = {
        "device_type": "huawei_telnet",
        "ip": ip,
        "username": username,
        "password": password,
        "port": 23,
        "timeout": 25,
        "global_delay_factor": 2,
    }

    try:
        print(f"[*] Connecting via Telnet to {ip}:23...")
        net_connect = ConnectHandler(**device)

        # -------------------------------------------------------------
        # 1. Handle Prompt Warnings & Password Renewal (Handle 'N' option)
        # -------------------------------------------------------------
        initial_output = net_connect.send_command("", expect_string=r"[>\]]")
        if any(
            phrase in initial_output
            for phrase in ["Change now", "Please choose", "password"]
        ):
            net_connect.send_command_timing("N")

        # -------------------------------------------------------------
        # 2. Get Switch Serial Number (display esn / display elabel)
        # -------------------------------------------------------------
        switch_sn = "Unknown"
        esn_output = net_connect.send_command_timing("display esn")

        if "[Y/N]" in esn_output or "Y/N" in esn_output:
            esn_output = net_connect.send_command_timing("Y")

        esn_match = re.search(r"ESN of device:\s*(\S+)", esn_output)
        if esn_match:
            switch_sn = esn_match.group(1).strip()
        else:
            elabel_output = net_connect.send_command_timing("display elabel")
            if "[Y/N]" in elabel_output or "Y/N" in elabel_output:
                elabel_output = net_connect.send_command_timing("Y")

            sn_match = re.search(
                r"BarCode\s*=\s*(\S+)|BoardSN\s*=\s*(\S+)", elabel_output
            )
            if sn_match:
                switch_sn = (
                    sn_match.group(1) if sn_match.group(1) else sn_match.group(2)
                )

        # -------------------------------------------------------------
        # 3. Check Interface Types for Switch Type (1G vs 10G / 40G)
        # -------------------------------------------------------------
        display_brief = net_connect.send_command("display interface brief")

        has_xge = bool(re.search(r"XGigabitEthernet\d+/\d+/\d+", display_brief))
        has_40g = bool(re.search(r"40GE\d+/\d+/\d+", display_brief))

        if has_40g:
            switch_type = "40 GE"
        elif has_xge:
            switch_type = "10G"
        else:
            switch_type = "1G"

        # Extract Switch Model
        display_device = net_connect.send_command("display device")
        model_match = re.search(r"S\d{4}[A-Z0-9-]*", display_device)
        switch_model = model_match.group(0) if model_match else "S5700"

        # -------------------------------------------------------------
        # 4. Extract Interfaces with SFP Modules Present
        # -------------------------------------------------------------
        fiber_ports = re.findall(
            r"((?:XGE|GE|XGigabitEthernet|GigabitEthernet)\d+/\d+/\d+)",
            display_brief,
        )
        fiber_ports = list(dict.fromkeys(fiber_ports))  # Deduplicate

        ports_data = []

        for port in fiber_ports:
            trans_output = net_connect.send_command(
                f"display transceiver interface {port}"
            )

            # Skip if output indicates no transceiver or error
            if (
                "Error" in trans_output
                or "not present" in trans_output
                or "offline" in trans_output
                or not trans_output.strip()
            ):
                continue

            # Extract SFP Serial / Vendor details
            sfp_sn = re.search(
                r"Manu\. Serial Number\s*:\s*(\S+)|Serial Number\s*:\s*(\S+)",
                trans_output,
            )
            vendor_name = re.search(r"Vendor Name\s*:\s*(.*)", trans_output)
            vendor_pn = re.search(
                r"Vendor Part Number\s*:\s*(.*)", trans_output
            )

            sfp_sn_val = (
                (sfp_sn.group(1) if sfp_sn.group(1) else sfp_sn.group(2))
                if sfp_sn
                else None
            )
            vendor_name_val = vendor_name.group(1).strip() if vendor_name else None

            # FILTER: ONLY include ports that have an actual SFP inserted
            # (Must have a valid Serial Number or Vendor Name and not be N/A / UNKNOWN)
            if not sfp_sn_val and not vendor_name_val:
                continue
            if sfp_sn_val in ["N/A", "UNKNOWN", ""] and vendor_name_val in [
                "N/A",
                "UNKNOWN",
                "",
            ]:
                continue

            # Fetch optical RX, TX, and Threshold levels
            verbose_output = net_connect.send_command(
                f"display transceiver interface {port} verbose"
            )

            trans_type = re.search(
                r"Transceiver Type\s*:\s*(.*)", trans_output
            )
            wavelength = re.search(
                r"Wavelength\(nm\)\s*:\s*(.*)", trans_output
            )
            distance = re.search(
                r"Transfer Distance\(m\)\s*:\s*(.*)", trans_output
            )

            rx_power = re.search(
                r"RX Power\s*\(dBM\)\s*:\s*([\d\.-]+)", verbose_output
            )
            tx_power = re.search(
                r"TX Power\s*\(dBM\)\s*:\s*([\d\.-]+)", verbose_output
            )
            rx_threshold = re.search(
                r"RX Power Low\s*Threshold\s*\(dBM\)\s*:\s*([\d\.-]+)",
                verbose_output,
            )

            ports_data.append(
                {
                    "Fiber Port": port,
                    "SFP Serial Number": sfp_sn_val if sfp_sn_val else "N/A",
                    "Transceiver Type": (
                        trans_type.group(1).strip()
                        if trans_type
                        else "UNKNOWN_SFP"
                    ),
                    "Wavelength (nm)": (
                        wavelength.group(1).strip() if wavelength else "N/A"
                    ),
                    "Transfer Distance (m)": (
                        distance.group(1).strip() if distance else "N/A"
                    ),
                    "Vendor Part Number": (
                        vendor_pn.group(1).strip() if vendor_pn else "N/A"
                    ),
                    "Vendor Name": (
                        vendor_name_val if vendor_name_val else "N/A"
                    ),
                    "RX Power (dBm)": (
                        rx_power.group(1).strip() if rx_power else "N/A"
                    ),
                    "TX Power (dBm)": (
                        tx_power.group(1).strip() if tx_power else "N/A"
                    ),
                    "RX Threshold (dBm)": (
                        rx_threshold.group(1).strip() if rx_threshold else "N/A"
                    ),
                }
            )

        net_connect.disconnect()

        # Build output rows
        records = []
        if not ports_data:
            # Optional: Log if switch has 0 active SFPs
            print(f"[!] {ip} has NO active SFPs inserted.")
            return []
        else:
            for idx, pdata in enumerate(ports_data):
                record = {
                    "Switch IP": ip if idx == 0 else "",
                    "Switch Serial Number": switch_sn if idx == 0 else "",
                    "Switch Type": switch_type if idx == 0 else "",
                    "Switch Model": switch_model if idx == 0 else "",
                }
                record.update(pdata)
                records.append(record)

        print(
            f"[+] Successfully collected {len(ports_data)} active SFP(s) from {ip}"
        )
        return records

    except NetMikoTimeoutException:
        print(f"[-] {ip} IP not pingable")
        return [
            {
                "Switch IP": ip,
                "Switch Serial Number": "IP not pingable",
                "Switch Type": "N/A",
                "Switch Model": "N/A",
                "Fiber Port": "N/A",
                "SFP Serial Number": "N/A",
                "Transceiver Type": "N/A",
                "Wavelength (nm)": "N/A",
                "Transfer Distance (m)": "N/A",
                "Vendor Part Number": "N/A",
                "Vendor Name": "N/A",
                "RX Power (dBm)": "N/A",
                "TX Power (dBm)": "N/A",
                "RX Threshold (dBm)": "N/A",
            }
        ]
    except Exception as err:
        print(f"[-] Failed to collect data from {ip}: {str(err)}")
        return []


# Process all switches in parallel
all_output_rows = []
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    futures = [
        executor.submit(
            parse_huawei_switch, dev["ip"], dev["username"], dev["password"]
        )
        for dev in DEVICE_LIST
    ]
    for future in concurrent.futures.as_completed(futures):
        all_output_rows.extend(future.result())

# Export to Excel
if all_output_rows:
    output_df = pd.DataFrame(all_output_rows)
    output_df.to_excel("huawei_sfp_inventory_refined.xlsx", index=False)
    print(
        "\n[✔] Refined dataset generated! Saved to 'huawei_sfp_inventory_refined.xlsx'"
    )
else:
    print("\n[-] No SFP module data gathered.")