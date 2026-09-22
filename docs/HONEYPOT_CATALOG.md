# Honeypot Sensor Catalog: Supported Sensor Types

The Deployer supports 12 honeypot profiles ranging from single-service deception emulators to industrial control systems and multi-sensor combos.

---

> **Playbook status:** a sensor type can be deployed only when it has both a deploy and a health playbook (see [PLAYBOOKS.md](PLAYBOOKS.md)). All 12 types below have them, each deployed and health-checked on a real DigitalOcean droplet; GCP has been verified the same way for Cowrie, with the rest expected to work (same shared code path) but not yet each confirmed on a real GCP instance. The deployer refuses to create a machine, or a campaign, for any type missing its playbooks; adding a new type is two small files.

> **Sizing:** every sensor also runs T-Pot's Logstash shipper, the largest process on the machine. The deployer gives it a trimmed per-sensor pipeline (see [PLAYBOOKS.md](PLAYBOOKS.md#the-trimmed-logstash-pipeline)) with a 512 MB heap and a 1 GB limit. A Cowrie sensor was measured on a 2 GB droplet using about 1.1 GB in total (Logstash about 660 MB, the honeypot under 60 MB), so **2 GB is enough for a single-honeypot sensor**; the multi-sensor runs several honeypots and needs 4 GB. The stock T-Pot sensor pipeline does not start on machines this small.

## 1. Quick Reference Matrix

| Sensor Type | Icon | Category | Open Ports | Droplet RAM | Playbooks | Target Threats |
| :--- | :---: | :--- | :--- | :--- | :---: | :--- |
| **Cowrie** | 🐚 | Shell / Deception | `22/tcp`, `23/tcp` | 2 GB | ✅ ready | SSH & Telnet brute-force, shell execution, botnet droppers |
| **Dionaea** | 🪤 | Malware / Exploits | `21, 42, 69u, 80, 135, 443, 445, 1433, 1723, 1883, 3306, 5060, 5061, 8081` | 2 GB | ✅ ready | WannaCry, EternalBlue, Conficker, SQL injection, worm payloads |
| **Conpot** | 🏭 | ICS / SCADA | `80, 102, 161u, 502, 1911, 2404, 44818, 47808u` | 2 GB | ✅ ready | Industrial reconnaissance, Modbus, S7, BACnet, power grid scans |
| **Elasticpot** | 🔍 | Cloud / DB | `9200/tcp` | 2 GB | ✅ ready | Elasticsearch CVE RCE exploits, unauthenticated cluster dumps |
| **Mailoney** | ✉️ | Mail / Spam | `25/tcp`, `587/tcp` | 2 GB | ✅ ready | Open-relay spam testing, credential brute force, phishing |
| **Heralding** | 🔑 | Credential Trap | `21, 22, 25, 110, 143, 993, 995, 1080, 3306, 5432, 5900` | 2 GB | ✅ ready | Multi-protocol credential harvesting (POP3, IMAP, VNC, SOCKS) |
| **Cisco ASA** | 🛡️ | Perimeter VPN | `5000/udp`, `8443/tcp` | 2 GB | ✅ ready | VPN zero-day reconnaissance, CVE-2020-3452, SSL portal probes |
| **Citrix Honeypot** | 🌐 | Enterprise Edge | `443/tcp` | 2 GB | ✅ ready | Citrix ADC / Gateway RCE exploits (e.g. CVE-2019-19781) |
| **Redis Honeypot** | ⚡ | Cache / NoSQL | `6379/tcp` | 2 GB | ✅ ready | Unprotected database probing, Redis RCE, crypto-miner injectors |
| **Sentrypeer** | 📞 | VoIP / SIP | `5060/tcp`, `5060/udp` | 2 GB | ✅ ready | SIP trunk toll fraud, brute-force PBX scanning |
| **Adbhoney** | 🤖 | IoT / Android | `5555/tcp` | 2 GB | ✅ ready | Android Debug Bridge worm infections, ADB.Miner botnet |
| **Multi-Sensor** | ⚡ | All-in-One | Ports for Cowrie, Dionaea, Elasticpot, Mailoney | 4 GB | ✅ ready | High-density threat collection across 18+ services |

---

## 2. Sensor Type Profiles

### 1. Cowrie (SSH / Telnet Deception)
- **Description**: Medium-interaction honeypot simulating an authentic Debian/Ubuntu Linux shell. Logs attacker usernames, passwords, typed terminal commands, and captures downloaded binary artifacts (trojans, worms, rootkits).
- **Default Exposed Ports**:
  - `22/tcp`: SSH Honeypot
  - `23/tcp`: Telnet Honeypot
- **Host Administration**: Relocated to Port `64295`.

### 2. Dionaea (Malware & Network Worm Harvester)
- **Description**: Designed to trap network worms that exploit SMB, RPC, SIP, and database flaws. Dionaea emulates vulnerable services, captures shellcode, and automatically downloads and quarantines executable binaries for malware analysis.
- **Default Exposed Ports**:
  - `445/tcp` (SMB), `135/tcp` (MS-RPC), `21/tcp` (FTP), `1433/tcp` (MSSQL), `3306/tcp` (MySQL), `1883/tcp` (MQTT), `5060/udp` (SIP).

### 3. Conpot (Industrial Control System / SCADA)
- **Description**: Low-to-medium interaction ICS honeypot designed to emulate industrial controllers. Presents realistic Siemens S7, Modbus, and BACnet PLC responses to assess targeted infrastructure reconnaissance.
- **Default Exposed Ports**:
  - `502/tcp` (Modbus), `102/tcp` (Siemens S7), `161/udp` (SNMP), `47808/udp` (BACnet), `1911/tcp` (Fox), `44818/tcp` (EtherNet/IP).

### 4. Elasticpot (Elasticsearch Database Trap)
- **Description**: Simulates vulnerable Elasticsearch clusters. Attracts threat actors seeking unprotected database nodes or attempting remote code execution (RCE) via CVE vulnerabilities.
- **Default Exposed Ports**:
  - `9200/tcp` (Elasticsearch HTTP REST API).

### 5. Mailoney (SMTP Open Relay & Spambot Trap)
- **Description**: Emulates an open SMTP relay server. Records incoming spam payloads, source IPs, target recipients, and authentication cracking attempts.
- **Default Exposed Ports**:
  - `25/tcp` (SMTP), `587/tcp` (Submission).

### 6. Heralding (Multi-Protocol Credential Harvester)
- **Description**: Fast, credential-focused honeypot that implements simple protocol handshakes to log submitted authentication credentials without providing interactive access.
- **Default Exposed Ports**:
  - `21` (FTP), `22` (SSH), `25` (SMTP), `110` (POP3), `143` (IMAP), `993` (IMAPS), `995` (POP3S), `1080` (SOCKS), `3306` (MySQL), `5432` (PostgreSQL), `5900` (VNC).

### 7. Cisco ASA (Perimeter Firewall / SSL VPN Trap)
- **Description**: Low-interaction honeypot emulating Cisco Adaptive Security Appliance (ASA) SSL VPN interfaces and web portals.
- **Default Exposed Ports**:
  - `5000/udp` (IKEv2 / IPsec), `8443/tcp` (Cisco AnyConnect Web Portal).

### 8. Citrix Honeypot (Enterprise Edge Gateway)
- **Description**: Emulates Citrix Application Delivery Controller (ADC) and Gateway login interfaces to detect scans and exploitation attempts.
- **Default Exposed Ports**:
  - `443/tcp` (HTTPS Citrix Gateway).

### 9. Redis Honeypot (NoSQL Cache Trap)
- **Description**: Emulates an open Redis in-memory key-value database. Catches unauthorized commands (`INFO`, `CONFIG SET`, `SLAVEOF`) and rogue SSH key injection attempts.
- **Default Exposed Ports**:
  - `6379/tcp` (Redis).

### 10. Sentrypeer (VoIP & Telecom Fraud Trap)
- **Description**: Distributed peer-to-peer VoIP honeypot that detects rogue SIP scanners attempting toll fraud and unauthorized calling loops.
- **Default Exposed Ports**:
  - `5060/tcp`, `5060/udp` (SIP).

### 11. Adbhoney (Android IoT Debug Trap)
- **Description**: Low-interaction honeypot simulating the Android Debug Bridge (ADB) protocol on smart TVs, Android devices, and embedded hardware.
- **Default Exposed Ports**:
  - `5555/tcp` (Android Debug Bridge).

### 12. Multi-Sensor Combo
- **Description**: High-density honeypot deploying Cowrie, Dionaea, Elasticpot, and Mailoney together in a unified Docker Compose stack on a single host. Recommended for 4GB RAM droplets.
- **Default Exposed Ports**:
  - `22, 23, 25, 80, 135, 445, 587, 1433, 3306, 9200`.
