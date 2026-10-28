# snapgent-bridge-linux

**Bridge Python untuk [Snapgent](https://github.com/herdodp/snapgent-connector) di Linux** — menghubungkan chat AI web gratis (DeepSeek, Claude, Gemini, ChatGPT, dll.) ke VS Code lewat MCP, tanpa API key.

> English summary: The upstream Snapgent repo ships only a Windows `bridge.exe`; the `bridge.py` its macOS launcher references was never published. This repo re-implements that bridge in Python from the extension's open protocol, so Snapgent works on Linux today. Since v0.2 the bridge serves **every** MCP server in `config.json` at once — stdio servers and HTTP servers (legacy SSE / Streamable HTTP) — which includes the MCP Server built into JetBrains IDEs (Android Studio, IntelliJ, ...) since 2025.2. Tested end-to-end.

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

Bridge membaca `config.json` (format sama seperti yang ditulis `setup-config.ps1` versi Windows) dan melayani **semua** server di dalamnya sekaligus:

```json
{"mcpServers": {
  "vscode": {"command": "python3", "args": ["/path/to/vscode_mcp.py"], "env": {"APPDATA": "/home/user/.config"}},
  "android-studio": {"url": "http://127.0.0.1:64342/sse"}
}}
```

Entri berkunci `command` dijalankan sebagai proses MCP stdio; entri berkunci `url` diajak bicara lewat HTTP — transport SSE lama bila URL berakhiran `/sse`, Streamable HTTP selain itu. Katalog tool digabung dan setiap tool ditandai server asalnya; bila dua server punya tool bernama sama, salinan server berikutnya diiklankan dengan awalan `nama-server/` (konvensi proyek hulu), dan pemanggilan boleh memakai nama polos (diarahkan ke pemilik pertama) atau nama berawalan itu.

**Trik Linux yang penting:** `vscode_mcp.py` mencari workspace aktif VS Code dari `%APPDATA%\Code\User\globalStorage\storage.json` — jalur khas Windows. VS Code di Linux menyimpan berkas yang sama persis di `~/.config/Code/User/globalStorage/storage.json`, jadi cukup arahkan env `APPDATA` ke `$HOME/.config` dan deteksi workspace berjalan tanpa mengubah satu baris pun dari kode resmi.

## Isi repo

| Berkas | Keterangan |
| --- | --- |
| `bridge.py` | Bridge Linux v0.2 (baru, ditulis untuk repo ini): multi-server stdio + SSE + Streamable HTTP. Satu-satunya dependensi: `websockets`. |
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

Hasil uji 28 Okt 2026 (Linux, Python 3.12): **27/27 pemeriksaan lulus**. Skenario 1 (regresi satu server): sapaan bridge memuat 24 tool, dan perintah `create_file` / `read_file` / `edit_file` / `run_command` yang dikirim lewat WebSocket (sebagaimana ekstensi mengirimnya) benar-benar terjadi di berkas workspace. Skenario 2 (tiga server sekaligus): server VS Code (stdio) + dua server tiruan JetBrains (Streamable HTTP dan SSE) — katalog gabungan 30 tool, tabrakan nama teratasi dengan awalan server, pemanggilan tool run configuration IDE sampai ke server yang benar, tambah/hapus server saat bridge berjalan tersimpan ke `config.json`. Unit test bawaan `vscode_mcp.py` dari proyek resmi juga lulus 4/4.

## Android Studio & IDE JetBrains lain

Android Studio (dan IntelliJ IDEA, PyCharm, WebStorm, ...) sejak versi 2025.2 membawa MCP Server bawaan — artinya AI bisa bekerja lewat IDE-nya langsung: inspeksi kode, refactor ganti nama semantik, menjalankan run configuration, dan perintah terminal di dalam IDE.

1. Di Android Studio: **Settings → Tools → MCP Server** → centang **Enable MCP Server**. (Opsional: aktifkan juga *brave mode* di bagian Command execution bila AI boleh menjalankan perintah/run configuration tanpa konfirmasi setiap kali.)
2. Masih di halaman yang sama, pada **Manual Client Configuration** klik **Copy SSE Config** atau **Copy HTTP Stream Config** — isinya URL server IDE kamu, mis. `http://127.0.0.1:64342/sse`.
3. Tambahkan sebagai entri `url` di `config.json` bridge (lihat contoh di atas; nama entri bebas, mis. `android-studio`), lalu restart bridge — atau tambahkan langsung dari UI ekstensi; bridge menyimpan perubahan ke `config.json`.
4. Di bar Snapgent pada tab chat, pilih target `android-studio`. Tool-tool IDE akan tampil di katalog perintah.

Catatan: jalur ini teruji terhadap server MCP tiruan yang setia pada protokol SSE & Streamable HTTP (lihat berkas test) — bukan terhadap Android Studio asli. Laporkan isu bila perilaku IDE aslimu berbeda.

Jalur yang selalu tersedia tanpa syarat versi IDE: buka folder proyek Android yang sama sebagai workspace (lewat server `vscode`), dan AI mengedit berkas di disk serta menjalankan build `./gradlew` lewat tool perintah — Android Studio memuat ulang perubahan berkas dari luar secara otomatis.

## Batasan

- Target Roblox Studio tidak didukung (khusus Windows).
- URL MCP hanya `http://` (server IDE lokal memang HTTP); `https://` belum didukung.
- Bridge hanya mendengarkan di `127.0.0.1`.

## Keamanan

Ini perangkat agentik: AI dapat mengubah berkas dan menjalankan perintah di dalam folder proyek. Pakai hanya di proyek yang ter-backup / ter-git. Mengotomatiskan chat web gratis berada di wilayah abu-abu syarat layanan masing-masing provider — risiko akun ditandai ditanggung pengguna.

## Lisensi & atribusi

GPL-3.0 (lihat `LICENSE`). `vscode_mcp.py` adalah karya proyek [Snapgent](https://github.com/herdodp/snapgent-connector) oleh herdodp, disertakan tanpa perubahan di bawah lisensi yang sama. `bridge.py`, `start-linux.sh`, dan `test_linux_bridge.py` adalah karya repo ini. Snapgent dan nama-nama produk lain adalah milik pemiliknya masing-masing; repo ini adalah proyek komunitas yang tidak berafiliasi.
