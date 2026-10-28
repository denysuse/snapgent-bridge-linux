# snapgent-bridge-linux

**Bridge Python untuk [Snapgent](https://github.com/herdodp/snapgent-connector) di Linux** — menghubungkan chat AI web gratis (DeepSeek, Claude, Gemini, ChatGPT, dll.) ke VS Code lewat MCP, tanpa API key.

> English summary: The upstream Snapgent repo ships only a Windows `bridge.exe`; the `bridge.py` its macOS launcher references was never published. This repo re-implements that bridge in Python from the extension's open protocol, so Snapgent works on Linux today. Tested end-to-end.

## Kenapa repo ini ada

Snapgent mengubah chat AI web biasa menjadi agen yang bisa bekerja di dalam VS Code (membaca/mengubah berkas, menjalankan perintah) lewat tiga komponen:

1. **Ekstensi browser** (resmi, open source) — menempel di halaman chat AI.
2. **Bridge lokal** — jembatan WebSocket `ws://127.0.0.1:17613` antara ekstensi dan server MCP.
3. **`vscode_mcp.py`** (resmi, open source) — server MCP dengan 24 tool workspace.

Masalahnya: komponen nomor 2 hanya dibagikan sebagai `bridge.exe` untuk Windows. Source `bridge.py`-nya dirujuk oleh peluncur macOS dan changelog proyek, tetapi tidak diterbitkan di mana pun — tidak di pohon repo, tidak di tag rilis, tidak di aset rilis, tidak di situs dokumentasinya (semua diverifikasi 28 Okt 2026). Akibatnya pengguna Linux (dan macOS) tidak bisa memakai Snapgent sama sekali.

Repo ini menambal lubang itu: `bridge.py` di sini adalah implementasi ulang yang bersih dari protokol yang sama persis seperti yang diharapkan ekstensi resmi, ditulis dengan membaca source ekstensinya yang terbuka.

## Cara kerja bridge ini

```
Chat AI (browser) ⇄ Ekstensi Snapgent ⇄ WebSocket :17613 ⇄ bridge.py ⇄ stdio JSON-RPC (MCP) ⇄ vscode_mcp.py ⇄ folder proyek VS Code
```

Bridge membaca `config.json` (format sama seperti yang ditulis `setup-config.ps1` versi Windows):

```json
{"mcpServers": {"vscode": {"command": "python3", "args": ["/path/to/vscode_mcp.py"], "env": {"APPDATA": "/home/user/.config"}}}}
```

**Trik Linux yang penting:** `vscode_mcp.py` mencari workspace aktif VS Code dari `%APPDATA%\Code\User\globalStorage\storage.json` — jalur khas Windows. VS Code di Linux menyimpan berkas yang sama persis di `~/.config/Code/User/globalStorage/storage.json`, jadi cukup arahkan env `APPDATA` ke `$HOME/.config` dan deteksi workspace berjalan tanpa mengubah satu baris pun dari kode resmi.

## Isi repo

| Berkas | Keterangan |
| --- | --- |
| `bridge.py` | Bridge Linux (baru, ditulis untuk repo ini). Satu-satunya dependensi: `websockets`. |
| `vscode_mcp.py` | Server MCP VS Code **resmi dari proyek Snapgent**, tanpa diubah. |
| `start-linux.sh` | Peluncur: membuat `.venv`, memasang `websockets`, menulis `config.json`, menjalankan bridge. |
| `test_linux_bridge.py` | Uji asap mandiri: bridge + MCP + operasi berkas nyata di workspace sementara. |
| `config.example.json` | Contoh konfigurasi. |
| `LICENSE` | GPL-3.0. |

Ekstensi browser tidak disertakan — ambil dari repo resmi: folder `snapgent-extension/` di [herdodp/snapgent-connector](https://github.com/herdodp/snapgent-connector).

## Pakai

1. Clone repo ini, buka terminal di foldernya.
2. Jalankan `./start-linux.sh` — biarkan terminal tetap terbuka.
3. Buka **VS Code** dan buka folder proyek yang mau dikerjakan (bridge membaca workspace yang sedang aktif).
4. Di Chrome/Chromium/Edge: `chrome://extensions` → aktifkan **Developer mode** → **Load unpacked** → pilih folder `snapgent-extension` dari repo resmi.
5. Buka situs chat AI (DeepSeek direkomendasikan pembuat Snapgent), mulai chat baru, klik **Start** pada bar Snapgent, lalu tulis tugasmu seperti biasa.

## Uji

```bash
python3 -m venv .venv && .venv/bin/pip install websockets
.venv/bin/python test_linux_bridge.py
```

Hasil uji 28 Okt 2026 (Linux, Python 3.12): **semua pemeriksaan lulus** — sapaan bridge memuat 24 tool, dan perintah `create_file` / `read_file` / `edit_file` / `run_command` yang dikirim lewat WebSocket (sebagaimana ekstensi mengirimnya) benar-benar terjadi di berkas workspace. Unit test bawaan `vscode_mcp.py` dari proyek resmi juga lulus 4/4.

## Batasan

- Bridge ini melayani satu server MCP (`vscode`). Pesan `add_server`/`remove_server` dari ekstensi dijawab "tidak didukung" — ubah `config.json` lalu restart bridge.
- Target Roblox Studio tidak didukung (khusus Windows).
- Bridge hanya mendengarkan di `127.0.0.1`.

## Keamanan

Ini perangkat agentik: AI dapat mengubah berkas dan menjalankan perintah di dalam folder proyek. Pakai hanya di proyek yang ter-backup / ter-git. Mengotomatiskan chat web gratis berada di wilayah abu-abu syarat layanan masing-masing provider — risiko akun ditandai ditanggung pengguna.

## Lisensi & atribusi

GPL-3.0 (lihat `LICENSE`). `vscode_mcp.py` adalah karya proyek [Snapgent](https://github.com/herdodp/snapgent-connector) oleh herdodp, disertakan tanpa perubahan di bawah lisensi yang sama. `bridge.py`, `start-linux.sh`, dan `test_linux_bridge.py` adalah karya repo ini. Snapgent dan nama-nama produk lain adalah milik pemiliknya masing-masing; repo ini adalah proyek komunitas yang tidak berafiliasi.
