#!/usr/bin/env bash
# Import the boot-partition Wi-Fi credentials once, before network-online.
# WIFI_FILE and WIFI_BOOT_ROOT are for isolated tests; the unit sets neither.
set -euo pipefail
export LC_ALL=C
umask 077

pi_file="${WIFI_BOOT_ROOT:-}/boot/firmware/thermoctl/wifi.env"
efi_file="${WIFI_BOOT_ROOT:-}/efi/thermoctl/wifi.env"
if [ -n "${WIFI_FILE:-}" ]; then
  wifi_file=$WIFI_FILE
elif { [ -e "$pi_file" ] || [ -L "$pi_file" ]; } \
  && { [ -e "$efi_file" ] || [ -L "$efi_file" ]; }; then
  echo 'firstboot-wifi: credentials exist at both boot paths' >&2
  exit 1
elif [ -e "$pi_file" ] || [ -L "$pi_file" ]; then
  wifi_file=$pi_file
else
  wifi_file=$efi_file
fi
profile=thermoctl-firstboot-wifi

erase_file() {
  # FAT has no useful file permissions. Overwrite the live file before unlinking;
  # flash wear leveling may still retain older physical copies.
  local size
  size=$(wc -c < "$wifi_file")
  if [ "$size" -gt 0 ]; then
    if ! head -c "$size" /dev/zero | dd of="$wifi_file" bs=4096 conv=notrunc 2>/dev/null; then
      echo 'firstboot-wifi: could not overwrite credential file' >&2
      return 1
    fi
  fi
  rm -- "$wifi_file"
}

if [ ! -e "$wifi_file" ] && [ ! -L "$wifi_file" ]; then
  exit 0
fi
if [ ! -f "$wifi_file" ] || [ -L "$wifi_file" ]; then
  echo 'firstboot-wifi: credential path is not a regular file' >&2
  exit 1
fi

size=$(wc -c < "$wifi_file")
if [ "$size" -gt 112 ]; then
  echo 'firstboot-wifi: invalid credential file; removing it' >&2
  erase_file
  exit 1
fi

lines=()
while IFS= read -r line || [ -n "$line" ]; do
  lines+=("$line")
done < "$wifi_file"
if [ "${#lines[@]}" -ne 2 ] || [[ "${lines[0]}" != SSID=* ]] || [[ "${lines[1]}" != PASSWORD=* ]]; then
  echo 'firstboot-wifi: invalid credential file; removing it' >&2
  erase_file
  exit 1
fi
ssid=${lines[0]#SSID=}
password=${lines[1]#PASSWORD=}

# WPA2 passphrases are 8-63 printable ASCII bytes, or a 64-digit hex PSK.
valid_password=0
if [ "${#password}" -ge 8 ] && [ "${#password}" -le 63 ] \
  && [[ "$password" != *[![:print:]]* ]]; then
  valid_password=1
elif [ "${#password}" -eq 64 ] && [[ "$password" != *[!0-9a-fA-F]* ]]; then
  valid_password=1
fi
# cmp also rejects missing final LF, CR, embedded NUL (which bash drops),
# duplicate keys, and any other byte that the line reader did not preserve.
if ! cmp -s "$wifi_file" <(printf 'SSID=%s\nPASSWORD=%s\n' "$ssid" "$password") \
  || [ "${#ssid}" -lt 1 ] || [ "${#ssid}" -gt 32 ] \
  || [[ "$ssid" == *[[:cntrl:]]* ]] || [ "$valid_password" -ne 1 ]; then
  echo 'firstboot-wifi: invalid credential file; removing it' >&2
  erase_file
  exit 1
fi

# A fixed profile name makes retries safe if unlinking failed on a prior boot.
# nmcli's default keyfile backend writes root-owned, mode-0600 profiles.
if nmcli connection show "$profile" >/dev/null 2>&1; then
  if ! nmcli connection modify "$profile" \
    802-11-wireless.ssid "$ssid" wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$password" connection.autoconnect yes >/dev/null 2>&1; then
    echo 'firstboot-wifi: NetworkManager could not update the Wi-Fi profile' >&2
    exit 1
  fi
elif ! nmcli connection add type wifi ifname '*' con-name "$profile" \
  ssid "$ssid" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$password" \
  connection.autoconnect yes >/dev/null 2>&1; then
  echo 'firstboot-wifi: NetworkManager could not save the Wi-Fi profile' >&2
  exit 1
fi

erase_file
printf 'firstboot-wifi: imported Wi-Fi profile for SSID %s\n' "$ssid" >&2
