#!/bin/bash

mkdir -p /opt/bw_manager

while true; do
    echo "=============================="
    echo "Enter connection information"
    echo "=============================="

    read -rp "Enter MikroTik IP Address (RAS): " Miktotik_IP
    read -rp "MikroTik Username: " Mikrotik_Username
    #printf "MikroTik Password: "
    read -rsp "MikroTik Password: " Mikrotik_Password
    printf "\n"
    read -rsp "RADIUS Secret: " Radius_Secret
    printf "\n"
    read -rp "Maximum BandWidth (mbps): " Max_BW

    echo

    # Create password mask
    Password_Mask=$(printf '%*s' "${#Mikrotik_Password}" '' | tr ' ' '*')
    Secret_Mask=$(printf '%*s' "${#Radius_Secret}" '' | tr ' ' '*')



    echo
    echo "=============================="
    echo "Please verify your information"
    echo "=============================="

    printf "MikroTik IP Address: %s\n" "$Miktotik_IP"
    printf "MikriTik Username: %s\n" "$Mikrotik_Username"
    printf "MikroTik Password: %s\n" "$Password_Mask"
    printf "RADIUS Secret: %s\n" "$Secret_Mask"
    printf "Maximum BandWidth: %s\n" "$Max_BW"

    echo

    read -rp "Is this information correct? [y/N/q]: " confirm

    if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
        break
    elif [[ "$confirm" == "q" || "$confirm" == "Q" ]]; then
        exit 0
    fi


    echo
    echo "Information was not confirmed."
    echo "Please enter the values again."
    echo
done

cat > /opt/bw_manager/config.json << EOF
{
    "mikrotik": {
        "host": "$Miktotik_IP",
        "api_port": 8728,
        "username": "$Mikrotik_Username",
        "password": "$Mikrotik_Password"
    },
    "radius": {
        "secret": "$Radius_Secret",
        "coa_port": 1700
    },
    "total_bandwidth_mbps": $Max_BW,
    "burst_time_seconds": 10,
    "log_file": "/var/log/bw_manager/bw_manager.log"
}
EOF

cp ./bw_manager.py /opt/bw_manager/

cat > /etc/systemd/system/bw_manager.service << EOF2
[Unit]
Description=Bandwidth Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/bw_manager
ExecStart=/usr/bin/python3 /opt/bw_manager/bw_manager.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF2

cat > /etc/systemd/system/bw_manager.timer << EOF3
[Unit]
Description=Bandwidth Manager periodically

[Timer]
OnBootSec=30s
OnUnitActiveSec=30s
Unit=bw_manager.service

[Install]
WantedBy=timers.target
EOF3

sudo systemctl daemon_reload
sudo systemctl enable bw_manager.timer
sudo systemctl enable bw_manager.service
sudo systemctl start bw_manager.timer
sudo systemctl start bw_manager.service
sudo systemctl status bw_manager.timer
sudo systemctl status bw_manager.service
