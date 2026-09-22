import copy
from typing import Dict, List, Any, Optional

"""
T-Pot Sensor Registry & Metadata.
Defines supported honeypot types, network port mappings, container topologies,
and platform-agnostic provisioning instructions for DigitalOcean and GCP.
"""

SENSOR_TYPES: Dict[str, Dict[str, Any]] = {
    "cowrie": {
        "id": "cowrie",
        "name": "Cowrie (SSH / Telnet Deception)",
        "short_name": "Cowrie",
        "category": "Shell & Brute Force",
        "icon": "🐚",
        "tagline": "Medium to high interaction SSH & Telnet honeypot designed to log brute force attacks and shell interaction.",
        "description": "Emulates an authentic Linux shell environment, records adversary keystrokes, tracks downloaded malware binaries via wget/curl, and logs credential brute-force attacks.",
        "admin_ssh_relocate": True,
        "recommended_size_do": "s-1vcpu-2gb",
        "recommended_size_gcp": "e2-small",
        "swap_size_gb": 2,
        "ports": [
            {"port": 22, "proto": "tcp", "service": "SSH", "description": "Cowrie SSH Honeypot"},
            {"port": 23, "proto": "tcp", "service": "Telnet", "description": "Cowrie Telnet Honeypot"}
        ],
        "test_commands": [
            {"label": "Test Cowrie SSH", "cmd": "ssh root@{ip} -p 22"},
            {"label": "Test Cowrie Telnet", "cmd": "telnet {ip} 23"}
        ],
        "required_dirs": [
            "/data/cowrie/downloads",
            "/data/cowrie/keys",
            "/data/cowrie/log",
            "/data/cowrie/log/tty"
        ],
        "networks": ["cowrie_local"],
        "services": {
            "cowrie": {
                "container_name": "cowrie",
                "restart": "always",
                "depends_on": {"tpotinit": {"condition": "service_healthy"}},
                "tmpfs": [
                    "/tmp/cowrie:uid=2000,gid=2000",
                    "/tmp/cowrie/data:uid=2000,gid=2000"
                ],
                "networks": ["cowrie_local"],
                "ports": ["22:22", "23:23"],
                "image": "{tpot_repo}/cowrie:{tpot_version}",
                "pull_policy": "always",
                "read_only": True,
                "volumes": [
                    "/data/cowrie/downloads:/home/cowrie/cowrie/dl",
                    "/data/cowrie/keys:/home/cowrie/cowrie/etc",
                    "/data/cowrie/log:/home/cowrie/cowrie/log",
                    "/data/cowrie/log/tty:/home/cowrie/cowrie/log/tty"
                ]
            }
        }
    },

    "dionaea": {
        "id": "dionaea",
        "name": "Dionaea (Malware Collector & Network Services)",
        "short_name": "Dionaea",
        "category": "Malware & Network Services",
        "icon": "🪤",
        "tagline": "Captures malware payloads exploiting SMB (WannaCry/EternalBlue), RPC, FTP, MSSQL, and MySQL.",
        "description": "Emulates vulnerable enterprise services to intercept automated worm replication, network scanners, and exploit payloads. Automatically saves intercepted malware binaries.",
        "admin_ssh_relocate": True,
        "recommended_size_do": "s-1vcpu-2gb",
        "recommended_size_gcp": "e2-small",
        "swap_size_gb": 2,
        "ports": [
            {"port": 20, "proto": "tcp", "service": "FTP-Data", "description": "FTP Data transfer"},
            {"port": 21, "proto": "tcp", "service": "FTP", "description": "FTP Server emulation"},
            {"port": 42, "proto": "tcp", "service": "WINS", "description": "WINS Name Server"},
            {"port": 69, "proto": "udp", "service": "TFTP", "description": "Trivial FTP emulation"},
            {"port": 81, "proto": "tcp", "service": "HTTP", "description": "Dionaea Web Service"},
            {"port": 135, "proto": "tcp", "service": "MS-RPC", "description": "Microsoft RPC Endpoint Mapper"},
            {"port": 445, "proto": "tcp", "service": "SMB", "description": "SMB / CIFS (EternalBlue target)"},
            {"port": 1433, "proto": "tcp", "service": "MSSQL", "description": "Microsoft SQL Server"},
            {"port": 1723, "proto": "tcp", "service": "PPTP", "description": "Point-to-Point Tunneling Protocol"},
            {"port": 1883, "proto": "tcp", "service": "MQTT", "description": "MQTT IoT Broker"},
            {"port": 3306, "proto": "tcp", "service": "MySQL", "description": "MySQL Database"},
            {"port": 27017, "proto": "tcp", "service": "MongoDB", "description": "MongoDB NoSQL Database"}
        ],
        "test_commands": [
            {"label": "Test SMB", "cmd": "smbclient -L //{ip} -N"},
            {"label": "Test Port Scan", "cmd": "nmap -sS -p 21,135,445,1433,3306 {ip}"}
        ],
        "required_dirs": [
            "/data/dionaea/roots/ftp",
            "/data/dionaea/roots/tftp",
            "/data/dionaea/roots/www",
            "/data/dionaea/roots/upnp",
            "/data/dionaea",
            "/data/dionaea/binaries",
            "/data/dionaea/log",
            "/data/dionaea/rtp"
        ],
        "networks": ["dionaea_local"],
        "services": {
            "dionaea": {
                "container_name": "dionaea",
                "restart": "always",
                "depends_on": {"tpotinit": {"condition": "service_healthy"}},
                "image": "{tpot_repo}/dionaea:{tpot_version}",
                "networks": ["dionaea_local"],
                "ports": [
                    "20:20", "21:21", "42:42", "69:69/udp", "81:81",
                    "135:135", "445:445", "1433:1433", "1723:1723",
                    "1883:1883", "3306:3306", "27017:27017"
                ],
                "pull_policy": "always",
                "read_only": True,
                "stdin_open": True,
                "tty": True,
                "volumes": [
                    "/data/dionaea/roots/ftp:/opt/dionaea/var/dionaea/roots/ftp",
                    "/data/dionaea/roots/tftp:/opt/dionaea/var/dionaea/roots/tftp",
                    "/data/dionaea/roots/www:/opt/dionaea/var/dionaea/roots/www",
                    "/data/dionaea/roots/upnp:/opt/dionaea/var/dionaea/roots/upnp",
                    "/data/dionaea:/opt/dionaea/var/dionaea",
                    "/data/dionaea/binaries:/opt/dionaea/var/dionaea/binaries",
                    "/data/dionaea/log:/opt/dionaea/var/log",
                    "/data/dionaea/rtp:/opt/dionaea/var/dionaea/rtp"
                ]
            }
        }
    },

    "conpot": {
        "id": "conpot",
        "name": "Conpot (ICS / SCADA Critical Infrastructure)",
        "short_name": "Conpot",
        "category": "Industrial & SCADA",
        "icon": "🏭",
        "tagline": "Industrial control system deception for Modbus, Siemens S7, SNMP, EtherNet/IP, and BACnet.",
        "description": "Simulates realistic operational technology (OT) protocols and PLC behavior to attract state-sponsored and scanner reconnaissance against utility and industrial targets.",
        "admin_ssh_relocate": True,
        "recommended_size_do": "s-1vcpu-2gb",
        "recommended_size_gcp": "e2-small",
        "swap_size_gb": 2,
        "ports": [
            {"port": 80, "proto": "tcp", "service": "HTTP-HMI", "description": "PLC Web HMI Interface"},
            {"port": 102, "proto": "tcp", "service": "S7COMM", "description": "Siemens S7 Industrial Protocol"},
            {"port": 161, "proto": "udp", "service": "SNMP", "description": "Industrial SNMP Monitoring"},
            {"port": 502, "proto": "tcp", "service": "Modbus", "description": "Modbus PLC Control"},
            {"port": 44818, "proto": "tcp", "service": "EtherNet/IP", "description": "Rockwell / Allen-Bradley Protocol"},
            {"port": 47808, "proto": "udp", "service": "BACnet", "description": "Building Automation & Control"}
        ],
        "test_commands": [
            {"label": "Test Modbus PLC", "cmd": "nmap -sV -p 502 --script modbus-discover {ip}"},
            {"label": "Test S7 Protocol", "cmd": "nmap -sV -p 102 --script s7-info {ip}"}
        ],
        "required_dirs": [
            "/data/conpot/log"
        ],
        "networks": ["conpot_local_default"],
        "services": {
            "conpot_default": {
                "container_name": "conpot_default",
                "restart": "always",
                "depends_on": {"tpotinit": {"condition": "service_healthy"}},
                "environment": [
                    "CONPOT_CONFIG=/etc/conpot/conpot.cfg",
                    "CONPOT_JSON_LOG=/var/log/conpot/conpot_default.json",
                    "CONPOT_LOG=/var/log/conpot/conpot_default.log",
                    "CONPOT_TEMPLATE=default",
                    "CONPOT_TMP=/tmp/conpot"
                ],
                "tmpfs": ["/tmp/conpot:uid=2000,gid=2000"],
                "networks": ["conpot_local_default"],
                "ports": [
                    "80:80",
                    "102:102",
                    "161:161/udp",
                    "502:502",
                    "44818:44818",
                    "47808:47808/udp"
                ],
                "image": "{tpot_repo}/conpot:{tpot_version}",
                "pull_policy": "always",
                "read_only": True,
                "volumes": [
                    "/data/conpot/log:/var/log/conpot"
                ]
            }
        }
    },

    "elasticpot": {
        "id": "elasticpot",
        "name": "Elasticpot (Elasticsearch RCE & Cloud Trap)",
        "short_name": "Elasticpot",
        "category": "Cloud & Databases",
        "icon": "🔍",
        "tagline": "Captures Elasticsearch exploits, CVE scanners, and remote code execution payload deliveries.",
        "description": "Simulates open, unauthenticated Elasticsearch clusters (port 9200) vulnerable to RCE vulnerabilities like CVE-2014-3120 and CVE-2015-1427.",
        "admin_ssh_relocate": True,
        "recommended_size_do": "s-1vcpu-2gb",
        "recommended_size_gcp": "e2-small",
        "swap_size_gb": 2,
        "ports": [
            {"port": 9200, "proto": "tcp", "service": "Elasticsearch", "description": "Elasticsearch REST API"}
        ],
        "test_commands": [
            {"label": "Test Cluster Info", "cmd": "curl -s http://{ip}:9200/"},
            {"label": "Test Search Query", "cmd": "curl -s http://{ip}:9200/_search"}
        ],
        "required_dirs": [
            "/data/elasticpot/log"
        ],
        "networks": ["elasticpot_local"],
        "services": {
            "elasticpot": {
                "container_name": "elasticpot",
                "restart": "always",
                "depends_on": {"tpotinit": {"condition": "service_healthy"}},
                "image": "{tpot_repo}/elasticpot:{tpot_version}",
                "networks": ["elasticpot_local"],
                "ports": ["9200:9200"],
                "pull_policy": "always",
                "read_only": True,
                "volumes": [
                    "/data/elasticpot/log:/opt/elasticpot/log"
                ]
            }
        }
    },

    "mailoney": {
        "id": "mailoney",
        "name": "Mailoney (SMTP Open Relay & Spambot Trap)",
        "short_name": "Mailoney",
        "category": "Email & Spam",
        "icon": "✉️",
        "tagline": "Lures spambots and phishing distributors attempting open-relay email propagation.",
        "description": "Emulates an SMTP server accepting email delivery attempts, records incoming spam headers, relay commands, message bodies, and tracking URLs.",
        "admin_ssh_relocate": True,
        "recommended_size_do": "s-1vcpu-2gb",
        "recommended_size_gcp": "e2-small",
        "swap_size_gb": 2,
        "ports": [
            {"port": 25, "proto": "tcp", "service": "SMTP", "description": "Standard SMTP Mail Port"},
            {"port": 587, "proto": "tcp", "service": "Submission", "description": "SMTP Message Submission"}
        ],
        "test_commands": [
            {"label": "Test SMTP Banner", "cmd": "nc -vn {ip} 25"},
            {"label": "Test VRFY Probe", "cmd": "echo 'VRFY root' | nc -vn {ip} 25"}
        ],
        "required_dirs": [
            "/data/mailoney/log"
        ],
        "networks": ["mailoney_local"],
        "services": {
            "mailoney": {
                "container_name": "mailoney",
                "restart": "always",
                "depends_on": {"tpotinit": {"condition": "service_healthy"}},
                "environment": [
                    "HPFEEDS_SERVER=",
                    "HPFEEDS_IDENT=user",
                    "HPFEEDS_SECRET=pass",
                    "HPFEEDS_PORT=20000",
                    "HPFEEDS_CHANNELPREFIX=prefix"
                ],
                "image": "{tpot_repo}/mailoney:{tpot_version}",
                "networks": ["mailoney_local"],
                "ports": ["25:25", "587:25"],
                "pull_policy": "always",
                "read_only": True,
                "volumes": [
                    "/data/mailoney/log:/opt/mailoney/logs"
                ]
            }
        }
    },

    "heralding": {
        "id": "heralding",
        "name": "Heralding (Credential Harvester)",
        "short_name": "Heralding",
        "category": "Credential Harvesting",
        "icon": "🔑",
        "tagline": "Captures authentication credentials across POP3, IMAP, VNC, PostgreSQL, and SOCKS proxies.",
        "description": "A multi-protocol credentials catcher that accepts login attempts across mail and remote administration protocols, logging plaintext usernames and passwords without granting access.",
        "admin_ssh_relocate": True,
        "recommended_size_do": "s-1vcpu-2gb",
        "recommended_size_gcp": "e2-small",
        "swap_size_gb": 2,
        "ports": [
            {"port": 110, "proto": "tcp", "service": "POP3", "description": "POP3 Mail Protocol"},
            {"port": 143, "proto": "tcp", "service": "IMAP", "description": "IMAP Mail Protocol"},
            {"port": 465, "proto": "tcp", "service": "SMTPS", "description": "Secure SMTP"},
            {"port": 993, "proto": "tcp", "service": "IMAPS", "description": "Secure IMAP"},
            {"port": 995, "proto": "tcp", "service": "POP3S", "description": "Secure POP3"},
            {"port": 1080, "proto": "tcp", "service": "SOCKS", "description": "SOCKS Proxy"},
            {"port": 5432, "proto": "tcp", "service": "PostgreSQL", "description": "PostgreSQL Database"},
            {"port": 5900, "proto": "tcp", "service": "VNC", "description": "VNC Remote Desktop"}
        ],
        "test_commands": [
            {"label": "Test POP3 Auth", "cmd": "nc -vn {ip} 110"},
            {"label": "Test VNC Banner", "cmd": "nc -vn {ip} 5900"}
        ],
        "required_dirs": [
            "/data/heralding/log"
        ],
        "networks": ["heralding_local"],
        "services": {
            "heralding": {
                "container_name": "heralding",
                "restart": "always",
                "depends_on": {"tpotinit": {"condition": "service_healthy"}},
                "image": "{tpot_repo}/heralding:{tpot_version}",
                "networks": ["heralding_local"],
                "ports": [
                    "110:110", "143:143", "465:465", "993:993",
                    "995:995", "1080:1080", "5432:5432", "5900:5900"
                ],
                "pull_policy": "always",
                "read_only": True,
                "tmpfs": ["/tmp/heralding:uid=2000,gid=2000"],
                "volumes": [
                    "/data/heralding/log:/var/log/heralding"
                ]
            }
        }
    },

    "ciscoasa": {
        "id": "ciscoasa",
        "name": "Cisco ASA (Perimeter & SSL VPN Honeypot)",
        "short_name": "Cisco ASA",
        "category": "Perimeter & VPN",
        "icon": "🛡️",
        "tagline": "Emulates Cisco ASA SSL VPN gateway portals and perimeter reconnaissance probes.",
        "description": "Simulates enterprise edge VPN endpoints on ports 5000/udp and 8443, capturing credential stuffing and Cisco ASA exploit attempts.",
        "admin_ssh_relocate": True,
        "recommended_size_do": "s-1vcpu-2gb",
        "recommended_size_gcp": "e2-small",
        "swap_size_gb": 2,
        "ports": [
            {"port": 5000, "proto": "udp", "service": "IKE/ISAKMP", "description": "Cisco VPN UDP service"},
            {"port": 8443, "proto": "tcp", "service": "HTTPS-VPN", "description": "Cisco AnyConnect Web VPN Portal"}
        ],
        "test_commands": [
            {"label": "Test WebVPN Portal", "cmd": "curl -k https://{ip}:8443/+CSCOE+/logon.html"}
        ],
        "required_dirs": [
            "/data/ciscoasa/log"
        ],
        "networks": ["ciscoasa_local"],
        "services": {
            "ciscoasa": {
                "container_name": "ciscoasa",
                "restart": "always",
                "depends_on": {"tpotinit": {"condition": "service_healthy"}},
                "image": "{tpot_repo}/ciscoasa:{tpot_version}",
                "networks": ["ciscoasa_local"],
                "ports": ["5000:5000/udp", "8443:8443"],
                "pull_policy": "always",
                "read_only": True,
                "tmpfs": ["/tmp/ciscoasa:uid=2000,gid=2000"],
                "volumes": [
                    "/data/ciscoasa/log:/var/log/ciscoasa"
                ]
            }
        }
    },

    "citrixhoneypot": {
        "id": "citrixhoneypot",
        "name": "Citrix ADC / Gateway (CVE Exploitation Trap)",
        "short_name": "Citrix",
        "category": "Perimeter & VPN",
        "icon": "🌐",
        "tagline": "Detects CVE-2019-19781 and Citrix ADC / NetScaler remote code execution attacks.",
        "description": "Emulates vulnerable Citrix NetScaler and Application Delivery Controller (ADC) portals on port 443, recording adversary traversal and exploit payloads.",
        "admin_ssh_relocate": True,
        "recommended_size_do": "s-1vcpu-2gb",
        "recommended_size_gcp": "e2-small",
        "swap_size_gb": 2,
        "ports": [
            {"port": 443, "proto": "tcp", "service": "HTTPS-Citrix", "description": "Citrix Gateway HTTPS"}
        ],
        "test_commands": [
            {"label": "Test Citrix Portal", "cmd": "curl -k https://{ip}/vpn/index.html"}
        ],
        "required_dirs": [
            "/data/citrixhoneypot/log"
        ],
        "networks": ["citrixhoneypot_local"],
        "services": {
            "citrixhoneypot": {
                "container_name": "citrixhoneypot",
                "restart": "always",
                "depends_on": {"tpotinit": {"condition": "service_healthy"}},
                "image": "{tpot_repo}/citrixhoneypot:{tpot_version}",
                "networks": ["citrixhoneypot_local"],
                "ports": ["443:443"],
                "pull_policy": "always",
                "read_only": True,
                "volumes": [
                    "/data/citrixhoneypot/log:/opt/citrixhoneypot/logs"
                ]
            }
        }
    },

    "redishoneypot": {
        "id": "redishoneypot",
        "name": "RedisHoneypot (NoSQL & Cloud Key-Value)",
        "short_name": "Redis",
        "category": "Cloud & Databases",
        "icon": "⚡",
        "tagline": "Traps threat actors scanning for unsecured Redis instances and SSH key injectors.",
        "description": "Simulates an open Redis server on port 6379, capturing attacker attempts to dump memory keys, inject SSH authorized_keys via CONFIG SET, or run rogue cronjobs.",
        "admin_ssh_relocate": True,
        "recommended_size_do": "s-1vcpu-2gb",
        "recommended_size_gcp": "e2-small",
        "swap_size_gb": 2,
        "ports": [
            {"port": 6379, "proto": "tcp", "service": "Redis", "description": "Redis Key-Value Database"}
        ],
        "test_commands": [
            {"label": "Test Redis PING", "cmd": "redis-cli -h {ip} ping || echo PING | nc -vn {ip} 6379"}
        ],
        "required_dirs": [
            "/data/redishoneypot/log"
        ],
        "networks": ["redishoneypot_local"],
        "services": {
            "redishoneypot": {
                "container_name": "redishoneypot",
                "restart": "always",
                "depends_on": {"tpotinit": {"condition": "service_healthy"}},
                "image": "{tpot_repo}/redishoneypot:{tpot_version}",
                "networks": ["redishoneypot_local"],
                "ports": ["6379:6379"],
                "pull_policy": "always",
                "read_only": True,
                "volumes": [
                    "/data/redishoneypot/log:/var/log/redishoneypot"
                ]
            }
        }
    },

    "sentrypeer": {
        "id": "sentrypeer",
        "name": "Sentrypeer (VoIP & SIP Telephony)",
        "short_name": "Sentrypeer",
        "category": "Email & Communications",
        "icon": "📞",
        "tagline": "Monitors fraudulent VoIP toll fraud, SIP brute force, and telecom probing.",
        "description": "Distributed SIP honeypot that logs bad actors attempting phone system abuse, unauthenticated SIP dialing, and VoIP network reconnaissance on port 5060.",
        "admin_ssh_relocate": True,
        "recommended_size_do": "s-1vcpu-2gb",
        "recommended_size_gcp": "e2-small",
        "swap_size_gb": 2,
        "ports": [
            {"port": 5060, "proto": "tcp", "service": "SIP-TCP", "description": "SIP Telephony Signaling"},
            {"port": 5060, "proto": "udp", "service": "SIP-UDP", "description": "SIP UDP Signaling"}
        ],
        "test_commands": [
            {"label": "Test SIP OPTIONS", "cmd": "sipsak -s sip:test@{ip} || nc -vn -u {ip} 5060"}
        ],
        "required_dirs": [
            "/data/sentrypeer/log"
        ],
        "networks": ["sentrypeer_local"],
        "services": {
            "sentrypeer": {
                "container_name": "sentrypeer",
                "restart": "always",
                "depends_on": {"tpotinit": {"condition": "service_healthy"}},
                "image": "{tpot_repo}/sentrypeer:{tpot_version}",
                "networks": ["sentrypeer_local"],
                "ports": ["5060:5060/tcp", "5060:5060/udp"],
                "pull_policy": "always",
                "read_only": True,
                "volumes": [
                    "/data/sentrypeer/log:/var/log/sentrypeer"
                ]
            }
        }
    },

    "adbhoney": {
        "id": "adbhoney",
        "name": "Adbhoney (Android Debug Bridge & IoT)",
        "short_name": "Adbhoney",
        "category": "Credential Harvesting",
        "icon": "🤖",
        "tagline": "Traps IoT botnets and miners targeting exposed Android TV and ADB devices.",
        "description": "Emulates an Android device with ADB enabled on port 5555. Records shell commands, APK installs, and cryptocurrency mining payloads.",
        "admin_ssh_relocate": True,
        "recommended_size_do": "s-1vcpu-2gb",
        "recommended_size_gcp": "e2-small",
        "swap_size_gb": 2,
        "ports": [
            {"port": 5555, "proto": "tcp", "service": "ADB", "description": "Android Debug Bridge"}
        ],
        "test_commands": [
            {"label": "Test ADB Connect", "cmd": "adb connect {ip}:5555"}
        ],
        "required_dirs": [
            "/data/adbhoney/log",
            "/data/adbhoney/downloads"
        ],
        "networks": ["adbhoney_local"],
        "services": {
            "adbhoney": {
                "container_name": "adbhoney",
                "restart": "always",
                "depends_on": {"tpotinit": {"condition": "service_healthy"}},
                "image": "{tpot_repo}/adbhoney:{tpot_version}",
                "networks": ["adbhoney_local"],
                "ports": ["5555:5555"],
                "pull_policy": "always",
                "read_only": True,
                "volumes": [
                    "/data/adbhoney/log:/opt/adbhoney/log",
                    "/data/adbhoney/downloads:/opt/adbhoney/dl"
                ]
            }
        }
    },

    "multi_sensor": {
        "id": "multi_sensor",
        "name": "Multi-Sensor Combo (Cowrie + Dionaea + Elasticpot + Mailoney)",
        "short_name": "Multi-Sensor",
        "category": "All-in-One Multi-Sensor",
        "icon": "⚡",
        "tagline": "High-density multi-vector deception combining SSH/Telnet, SMB malware, Elasticsearch, and SMTP on a single host.",
        "description": "Provisions a combined deception node running Cowrie, Dionaea, Elasticpot, and Mailoney with zero port conflicts. Recommended 4GB RAM.",
        "admin_ssh_relocate": True,
        "recommended_size_do": "s-2vcpu-4gb",
        "recommended_size_gcp": "e2-medium",
        "swap_size_gb": 4,
        "ports": [
            {"port": 20, "proto": "tcp", "service": "FTP-Data", "description": "Dionaea FTP Data"},
            {"port": 21, "proto": "tcp", "service": "FTP", "description": "Dionaea FTP"},
            {"port": 22, "proto": "tcp", "service": "SSH", "description": "Cowrie SSH Honeypot"},
            {"port": 23, "proto": "tcp", "service": "Telnet", "description": "Cowrie Telnet Honeypot"},
            {"port": 25, "proto": "tcp", "service": "SMTP", "description": "Mailoney SMTP Spambot Trap"},
            {"port": 42, "proto": "tcp", "service": "WINS", "description": "Dionaea WINS"},
            {"port": 69, "proto": "udp", "service": "TFTP", "description": "Dionaea TFTP"},
            {"port": 81, "proto": "tcp", "service": "HTTP", "description": "Dionaea HTTP"},
            {"port": 135, "proto": "tcp", "service": "MS-RPC", "description": "Dionaea RPC"},
            {"port": 445, "proto": "tcp", "service": "SMB", "description": "Dionaea SMB (EternalBlue)"},
            {"port": 587, "proto": "tcp", "service": "Submission", "description": "Mailoney Submission"},
            {"port": 1433, "proto": "tcp", "service": "MSSQL", "description": "Dionaea MSSQL"},
            {"port": 1723, "proto": "tcp", "service": "PPTP", "description": "Dionaea PPTP"},
            {"port": 1883, "proto": "tcp", "service": "MQTT", "description": "Dionaea MQTT"},
            {"port": 3306, "proto": "tcp", "service": "MySQL", "description": "Dionaea MySQL"},
            {"port": 9200, "proto": "tcp", "service": "Elasticsearch", "description": "Elasticpot API"},
            {"port": 27017, "proto": "tcp", "service": "MongoDB", "description": "Dionaea MongoDB"}
        ],
        "test_commands": [
            {"label": "Test Cowrie SSH", "cmd": "ssh root@{ip} -p 22"},
            {"label": "Test SMB", "cmd": "smbclient -L //{ip} -N"},
            {"label": "Test Elasticsearch", "cmd": "curl -s http://{ip}:9200/"},
            {"label": "Test SMTP", "cmd": "nc -vn {ip} 25"}
        ],
        "required_dirs": [
            "/data/cowrie/downloads",
            "/data/cowrie/keys",
            "/data/cowrie/log",
            "/data/cowrie/log/tty",
            "/data/dionaea/roots/ftp",
            "/data/dionaea/roots/tftp",
            "/data/dionaea/roots/www",
            "/data/dionaea/roots/upnp",
            "/data/dionaea",
            "/data/dionaea/binaries",
            "/data/dionaea/log",
            "/data/dionaea/rtp",
            "/data/elasticpot/log",
            "/data/mailoney/log"
        ],
        "networks": ["cowrie_local", "dionaea_local", "elasticpot_local", "mailoney_local"],
        "services": {}  # Populated dynamically below
    }
}

# Assemble multi_sensor services from component definitions
for comp in ["cowrie", "dionaea", "elasticpot", "mailoney"]:
    SENSOR_TYPES["multi_sensor"]["services"].update(SENSOR_TYPES[comp]["services"])


def get_sensor_type(sensor_type: Optional[str]) -> Dict[str, Any]:
    """Retrieve sensor definition or default to cowrie."""
    stype = (sensor_type or "cowrie").lower().strip()
    return SENSOR_TYPES.get(stype, SENSOR_TYPES["cowrie"])


def list_sensor_types() -> List[Dict[str, Any]]:
    """Return all sensor definitions sanitized for public API consumption."""
    result = []
    for k, v in SENSOR_TYPES.items():
        summary = {
            "id": v["id"],
            "name": v["name"],
            "short_name": v["short_name"],
            "category": v["category"],
            "icon": v["icon"],
            "tagline": v["tagline"],
            "description": v["description"],
            "recommended_size_do": v["recommended_size_do"],
            "recommended_size_gcp": v["recommended_size_gcp"],
            "swap_size_gb": v["swap_size_gb"],
            "admin_ssh_relocate": v["admin_ssh_relocate"],
            "ports": v["ports"],
            "test_commands": v["test_commands"]
        }
        result.append(summary)
    return result


def get_sensor_ports(sensor_type: str) -> List[Dict[str, Any]]:
    """Return ports list for a sensor type."""
    return get_sensor_type(sensor_type).get("ports", [])


def get_gcp_firewall_ports(sensor_type: str) -> Dict[str, List[str]]:
    """
    Returns separate TCP and UDP port strings for GCP firewall rules.
    Example: {"tcp": ["22", "23"], "udp": []}
    """
    ports = get_sensor_ports(sensor_type)
    tcp_ports = []
    udp_ports = []
    for p in ports:
        port_num = str(p["port"])
        if p["proto"].lower() == "udp":
            if port_num not in udp_ports:
                udp_ports.append(port_num)
        else:
            if port_num not in tcp_ports:
                tcp_ports.append(port_num)
    return {"tcp": sorted(tcp_ports, key=int), "udp": sorted(udp_ports, key=int)}


def build_sensor_compose_yaml(
    sensor_type: str,
    hive_ip: str,
    tpot_hive_user: str,
    sensor_name: str,
    ssl_verification: str = "full",
    tpot_version: str = "24.04",
    tpot_repo: str = "dtagdevsec"
) -> str:
    """
    Generates a valid docker-compose.yml content for the selected sensor type,
    wiring tpotinit, logstash (configured to ship to Hive), and the selected honeypot service(s).
    """
    import yaml

    st = get_sensor_type(sensor_type)
    networks = {net: None for net in st.get("networks", [])}

    # Base services: tpotinit and logstash
    services = {
        "tpotinit": {
            "container_name": "tpotinit",
            "env_file": [".env"],
            "restart": "always",
            "stop_grace_period": "60s",
            "tmpfs": [
                "/tmp/etc:uid=2000,gid=2000",
                "/tmp/:uid=2000,gid=2000"
            ],
            "network_mode": "host",
            "cap_add": ["NET_ADMIN"],
            "image": f"{tpot_repo}/tpotinit:{tpot_version}",
            "pull_policy": "always",
            "volumes": [
                "/opt/tpotce/docker-compose.yml:/tmp/tpot/docker-compose.yml:ro",
                "/data/blackhole:/etc/blackhole",
                "/data:/data",
                "/var/run/docker.sock:/var/run/docker.sock:ro"
            ]
        },
        "logstash": {
            "container_name": "logstash",
            "restart": "always",
            "depends_on": {
                "tpotinit": {"condition": "service_healthy"}
            },
            "environment": [
                f"LS_JAVA_OPTS={'-Xms512m -Xmx768m' if sensor_type != 'multi_sensor' else '-Xms1024m -Xmx1536m'}",
                "TPOT_TYPE=SENSOR",
                f"TPOT_HIVE_USER={tpot_hive_user}",
                f"TPOT_HIVE_IP={hive_ip}",
                f"LS_SSL_VERIFICATION={ssl_verification}",
                f"MY_HOSTNAME={sensor_name}"
            ],
            "ports": ["127.0.0.1:64305:64305"],
            "mem_limit": "1536m" if sensor_type != "multi_sensor" else "2560m",
            "image": f"{tpot_repo}/logstash:{tpot_version}",
            "pull_policy": "always",
            "volumes": ["/data:/data"]
        }
    }

    # Add honeypot service(s)
    raw_services = st.get("services", {})
    for svc_name, raw_cfg in raw_services.items():
        svc_copy = copy.deepcopy(raw_cfg)
        # Format string placeholders
        if "image" in svc_copy:
            svc_copy["image"] = svc_copy["image"].format(tpot_repo=tpot_repo, tpot_version=tpot_version)
        services[svc_name] = svc_copy

    compose_dict = {
        "networks": networks,
        "services": services
    }

    return yaml.dump(compose_dict, sort_keys=False)
