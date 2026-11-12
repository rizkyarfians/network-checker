# Orange Pi Network Checker

Standalone **LAN-primary → modem-backup Internet failover** untuk SPFF (Python 3, NetworkManager, systemd). Tidak membutuhkan Firebase, database, broker, atau service SPFF. **Tidak perlu tahu tipe modem sebelum menjalankan scan.**

> **Status:** kode dan unit test sudah dicek di lingkungan pengembangan, **belum diuji di Orange Pi, modem, maupun NetworkManager asli**. Jangan aktifkan perubahan routing via SSH-only. Siapkan akses console lokal atau out-of-band sebelum `--apply`.

## Perilaku

- Men-scan interface **read-only** dari `nmcli`, IPv4 default routes dari `ip`, plus `mmcli` dan `lsusb` jika tersedia.
- Otomatis mengenali LAN Ethernet onboard sebagai primary dan Wi-Fi / WWAN / GSM / USB-Ethernet sebagai calon backup. Jika hasilnya ambigu, berhenti dan minta nama interface di config, **tidak asal memilih**.
- Menguji koneksi HTTPS per interface menggunakan `curl --interface`. Setelah 3× LAN gagal dan backup sehat, mengutamakan backup; kembali ke LAN setelah 6× LAN sehat.
- Hanya mengubah **active IPv4 route metric** lewat NetworkManager; tidak down/up interface, tidak menyentuh profil tersimpan, tidak mengubah route subnet lokal secara langsung.
- Jika keduanya gagal: tidak mengarang koneksi sehat, mempertahankan pilihan terakhir. MQTT, PostgreSQL dan dashboard lokal tetap berjalan terpisah.
- Default **dry-run**; mode `--scan` tidak mengubah routing. Service hanya dipasang/di-enable saat sudah lulus tes di perangkat nyata.

## Prasyarat

Linux Debian/Ubuntu dengan NetworkManager mengelola interface, `iproute2`, `curl`, dan Python 3.10+. Untuk diagnostik USB modem, opsional `usbutils` / `modemmanager`. Kedua internet uplink perlu sudah tersambung otomatis dan mendapat **gateway IPv4**, idealnya subnet berbeda. Jika modem butuh APN/dial-up, atur dahulu lewat NetworkManager/ModemManager; scanner **tidak** mengisi APN/password, membuat profil, atau mengaktifkan SIM.

## Scan saja (AMAN)

```bash
sudo apt-get install -y python3 curl iproute2 network-manager
# Opsional: sudo apt-get install -y usbutils modemmanager
python3 network_checker.py --scan --config config.example.json
```

Output JSON memuat device, jenis koneksi, USB-backed, gateway, route metric, dan hasil pemilihan. `selection_warning` berarti perlu bantuan konfigurasi, **bukan kegagalan scanner**. Jangan kirim informasi SIM/credential dari `mmcli`; output scan ini hanya menjalankan `mmcli -L`.

## Instalasi di Orange Pi (setelah onsite)

```bash
sudo install -D -m 0644 network_checker.py /opt/network-checker/network_checker.py
sudo install -D -m 0644 config.example.json /etc/orangepi-network-checker.json
sudo nano /etc/orangepi-network-checker.json
```

`primary_interface` dan `backup_interface` **`null`** berarti auto-detect. Jika hasil scan tidak pasti, isi contoh `"primary_interface": "end0"` dan `"backup_interface": "wwan0"` mengikuti nama riil dari scan.

Uji tanpa perubahan routing:

```bash
python3 /opt/network-checker/network_checker.py --scan
python3 /opt/network-checker/network_checker.py --once
```

Setelah terverifikasi di konsol lokal:

```bash
sudo python3 /opt/network-checker/network_checker.py --apply --once
ip -4 route show default
# Hanya aktifkan service setelah dipastikan routing, SSH LAN dan dashboard SPFF tetap aman.
sudo install -D -m 0644 systemd/orangepi-network-checker.service /etc/systemd/system/orangepi-network-checker.service
sudo systemctl daemon-reload
sudo systemctl enable --now orangepi-network-checker
journalctl -u orangepi-network-checker -f
```

**Rollback** jika terjadi masalah di konsol lokal:

```bash
sudo systemctl disable --now orangepi-network-checker
# Reaktivasi profil koneksi sesuai kondisi jaringan setempat / reboot untuk mengembalikan active route metric.
nmcli connection show --active
ip -4 route show default
```

## Pengujian setelah pemasangan

1. Saat LAN + modem sehat, pastikan internet keluar dari LAN (cek `ip route get 1.1.1.1`).
2. Putuskan **akses internet WAN router LAN**, jangan cabut semua koneksi LAN. Setelah 3 kegagalan, periksa route mengarah ke modem.
3. Cabut kabel LAN; pastikan backup tetap aktif, termasuk setelah device tidak punya default route.
4. Pulihkan WAN LAN dan pastikan setelah 6 probe sukses pemilihan kembali ke LAN.
5. Matikan kedua internet, pastikan dashboard lokal, database PostgreSQL dan MQTT masih bisa diakses lewat LAN/Wi-Fi lokal sesuai topologi.
6. Restart modem / Orange Pi, pastikan service recovery bekerja. Amati `journalctl` untuk error.

## Batasan dan keamanan

- Ini **route failover, bukan bonding**. Koneksi TCP aktif (termasuk Firebase sync) bisa terputus sementara. Worker SPFF harus tetap retry/idempotent.
- **Tidak membuat hotspot, NAT, forwarding, atau Wi-Fi AP**. Klien LAN tidak otomatis ikut memakai internet modem.
- DNS sistem mungkin memakai resolver dari interface yang mati; verifikasi DNS/resolved dan target HTTPS di perangkat aktual.
- Beberapa perangkat modem USB tidak muncul sebagai `wwan0` sampai ModemManager/APN aktif. USB tethering bisa muncul sebagai Ethernet. `--scan` membantu, tetapi bukan jaminan mendeteksi model modem secara lengkap.
- NetworkManager bisa mengubah metric ketika koneksi reconnect. Service mencoba menegakkan kembali preferensi di setiap siklus saat link tersedia. Perlu uji pada distro Orange Pi.
- Jika service lain (netplan/systemd-networkd/ifupdown) yang mengelola network, jangan gunakan mode `--apply` ini tanpa adaptasi.
- Jangan menyimpan `.env`, SIM PIN, APN credential, atau Wi-Fi password ke repo.

## Test lokal

```bash
python3 -m compileall -q network_checker.py tests/
python3 -m unittest discover -s tests -v
```

Tidak ada `npm run build` / `npm run lint` karena ini repo Python standalone, bukan workspace React/Node SPFF.
