# Palo Alto Networks Firewall Integration (EDL & NAT)

## 1. Overview

The **Palo Alto Networks External Dynamic List (EDL)** feed dynamically authorizes distributed cloud honeypot sensors to communicate with the T-Pot Hive.

Whenever sensors are deployed, rescheduled, or destroyed across DigitalOcean or GCP, their public IP addresses are automatically published in real time as a standard plaintext feed. The Palo Alto firewall polls this feed periodically and automatically adapts its security policies and Destination NAT rules—requiring **zero manual firewall administration**.

```
  [ Cloud Sensors (DO / GCP) ]
               │
               ▼ Logstash stream (TCP 64294)
  ┌────────────────────────────────────────────────────────┐
  │              Palo Alto Networks Firewall               │
  │                                                        │
  │  1. EDL Object: "TPot-Sensors-EDL"                     │
  │     Source URL: https://<hive>:64297/edl/sensors.txt   │
  │     (Refreshed every 5 minutes / Hourly)               │
  │                                                        │
  │  2. Inbound Security Rule:                             │
  │     Source Zone: Untrust / Internet                    │
  │     Source Address: TPot-Sensors-EDL                   │
  │     Destination Zone: DMZ / Trust                      │
  │     Service: TCP 64294                                 │
  │     Action: Allow                                      │
  │                                                        │
  │  3. Destination NAT Rule:                              │
  │     Source: TPot-Sensors-EDL                           │
  │     Destination: Public Firewall VIP                   │
  │     Translated Address: <Hive-Internal-IP>             │
  │     Translated Port: 64294                             │
  └──────────────────────────┬─────────────────────────────┘
                             │
                             ▼ Ingest traffic (TCP 64294)
               [ T-Pot Hive Collector ]
```

---

## 2. Feed Endpoints

| Endpoint | Protocol | Port | Authentication | Notes |
| :--- | :--- | :--- | :--- | :--- |
| `https://<HIVE_IP>:64297/edl/sensors.txt` | HTTPS | `64297` | **None** (`auth_basic off`) | Primary production feed (TLS encrypted) |

### Which sensors are listed, and when

A sensor is listed **as soon as it has an IP**, which is right after its droplet is created and while it is still being configured (status `provisioning`), and stays listed while `active`. It is removed when it is destroyed or if its setup fails (`provision_failed`), and static entries are always listed.

Publishing early matters. The firewall can only admit a sensor to the Hive (TCP 64294) after it has fetched a feed that contains the sensor's IP, and setting a sensor up takes about 5 minutes. With the recommended 5-minute refresh, the sensor is usually admitted before its setup finishes.

The deployer's health check verifies the sensor can reach the Hive. If it cannot yet, the deploy waits (up to 8 minutes, `HIVE_ADMIT_WAIT_SECONDS`) for the firewall's next refresh, and if the firewall never admits the sensor it reports a **warning** instead of failing: the sensor runs and records attacks but cannot ship them.

### Feed Format
The feed conforms strictly to PAN-OS IP Address List requirements:
- One IPv4 address or CIDR per line (e.g. `165.22.45.101`).
- Comments preceded by `#`.
- Standard UNIX newlines (`\n`).
- Cache-Control headers set to `no-cache, no-store, must-revalidate` with `expires: 0`.

---

## 3. PAN-OS Configuration Guide

### Step 1: Create the External Dynamic List (EDL) Object
1. Log in to the PAN-OS Web Interface.
2. Navigate to **Objects > External Dynamic Lists** and click **Add**.
3. Configure the object:
   - **Name**: `TPot-Sensors-EDL`
   - **Type**: `IP List`
   - **Source**: `https://<HIVE_IP>:64297/edl/sensors.txt`
   - **Repeat**: `Every 5 Mins` (recommended for responsive campaign tracking) or `Hourly`
   - **Certificate Profile**: If using HTTPS with T-Pot's self-signed certificate, select a Certificate Profile that trusts Hive's CA or allows untrusted server certificates during initial deployment.
4. Click **OK**.

### Step 2: Configure Destination NAT (DNAT)
To translate inbound traffic from the sensor fleet to your internal Hive collector:
1. Navigate to **Policies > NAT** and click **Add**.
2. Configure **General**:
   - **Name**: `TPot-Sensor-Logstash-DNAT`
3. Configure **Original Packet**:
   - **Source Zone**: `Untrust` (or external zone facing the internet)
   - **Destination Zone**: `Untrust`
   - **Destination Interface**: Your external WAN interface
   - **Service**: `service-tcp-64294` (create service object for TCP 64294 if not already present)
   - **Source Address**: Select `TPot-Sensors-EDL`
   - **Destination Address**: Your external public firewall IP / VIP
4. Configure **Translated Packet**:
   - **Destination Translation**:
     - **Translation Type**: `Dynamic IP and Port` or `Static IP`
     - **Translated Address**: Internal LAN/DMZ IP of your T-Pot Hive (e.g. `172.31.30.66`)
     - **Translated Port**: `64294`
5. Click **OK**.

### Step 3: Configure Inbound Security Policy
1. Navigate to **Policies > Security** and click **Add**.
2. Configure **General**:
   - **Name**: `Allow-TPot-Sensors-Ingest`
   - **Rule Type**: `universal`
3. Configure **Source**:
   - **Source Zone**: `Untrust`
   - **Source Address**: Select `TPot-Sensors-EDL`
4. Configure **Destination**:
   - **Destination Zone**: Zone where the Hive resides (e.g. `DMZ` or `Trust`)
   - **Destination Address**: Hive Internal IP (post-NAT) or Public VIP (pre-NAT, depending on PAN-OS version policy matching)
5. Configure **Service / URL Category**:
   - **Service**: `service-tcp-64294`
6. Configure **Actions**:
   - **Action**: `Allow`
   - **Log Setting**: Enable `Log at Session End`
7. Click **OK**, then click **Commit**.

---

## 4. Operational Verification via PAN-OS CLI

To verify that PAN-OS is successfully fetching and parsing the EDL:

```text
# View the status and fetch timestamp of the list:
request system external-list show type ip name TPot-Sensors-EDL

# Manually force an immediate refresh:
request system external-list refresh type ip name TPot-Sensors-EDL

# View the active parsed entries stored in firewall memory:
show external-list name TPot-Sensors-EDL
```

---

### Checking that the firewall is polling

T-Pot's nginx logs each fetch. The firewall's requests are for `/edl/sensors.txt` with an empty user agent (browsers and `curl` show a user agent), and the log records the firewall's address in `src_ip`:

```bash
grep '"request_uri": "/edl/sensors.txt"' "$TPOT_HOST_DIR/data/nginx/log/access.log" | tail -5
```

You should see one fetch about every 5 to 7 minutes (the refresh interval plus fetch time). If a new sensor cannot reach the Hive, compare its deploy time with the last fetch that came after the sensor was created.

---

## 5. Adding Static Sensors to the EDL

If you have on-premise honeypots, hardware appliances, or fixed cloud instances that should also be included in the firewall EDL:
1. Open the Web UI at `https://<hive-ip>:64297/sensors/`.
2. Scroll to the **Palo Alto Networks EDL** panel.
3. Click **Add Static IP**.
4. Enter the public IPv4 address and description (e.g. `Branch Office Hardware Honeypot`).
5. Click **Add to Whitelist**. The IP will immediately be included in the live feed.
