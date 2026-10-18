# Runbook harian paper trading (saham IDX dan kripto)

Dijalankan Claude oleh dua jadwal: **saham** setiap hari bursa 16:48 WIB (`--market idx`) dan **kripto** setiap hari
07:15 WIB (`--market crypto`). Pemilik akun membaca hasilnya di chat, notifikasi HP, dan dashboard
https://claude.ai/artifact/NSiEW7U2ivbXtpCfpcHnyn (tab "Saham IDX" dan "Kripto"). Semua pesan dalam Bahasa Indonesia.

## Aturan yang disetujui pemilik

- Paper trading saja. Tidak ada order sungguhan, di Mirae maupun Binance.
- Saham: LQ45, modal virtual Rp 10 juta. Kripto: 15 koin terbesar Binance spot tanpa stablecoin/meme coin,
  modal virtual 555,56 USDT (Rp 10 juta), spot saja tanpa leverage. Dua akun terpisah.
- Elder: risiko maks. 2% per trade, rem 6% per bulan (rugi bulan ini + risiko terbuka), dihitung per akun.
- Risiko besar atau kesalahan: hentikan langkah berikutnya dan beri tahu pemilik dulu.
- Jangan pernah meminta seed phrase Trust Wallet atau kunci API apa pun di chat.

## Langkah (ganti `<m>` dengan `idx` atau `crypto`)

1. `git pull origin ccr-1c2d0097-crqzw5`, lalu `pip install -q -e . pytest` bila paket belum terpasang.
2. `python -m pytest tests/test_idx_bot.py -q`. Kalau gagal, berhenti dan laporkan.
3. `python -m idx_bot.daily run --market <m>`. Saham: kalau `date` sama dengan laporan sebelumnya, bursa libur;
   kirim satu baris saja.
4. Untuk setiap kandidat (maks. 5), jalankan tim analis secara singkat:
   - Teknikal: baris kandidat di `runs/screener-<date>.json` (kripto: `crypto_paper/runs/`).
   - Saham, fundamental: angka di baris yang sama. Kripto, skor pasar: peringkat market cap, kekuatan vs BTC, volume.
   - Berita: WebSearch hari itu. Saham: aksi korporasi, suspensi, UMA, isu MSCI. Kripto: peretasan, delisting,
     regulasi, ETF, jadwal unlock token, kondisi BTC.
   - Bull vs bear: satu alasan terkuat masing-masing.
   - Risiko: rencana stop/target/jumlah di laporan; tolak bila ada kabar buruk yang belum tercermin di harga.
   - Keputusan BELI atau LEWATI. BELI: `python -m idx_bot.daily buy KODE --market <m> --reason "<alasan singkat>"`.
5. Unggah CSV dashboard (Artifact `asset: true`), lalu dengan ArtifactData `update` dataset-nya: `source.url`,
   `source.name` dan `updated` (baca versi dokumen dulu).
   - Saham, dari `idx_bot/paper/dashboard/`: `screener.csv` → `screener`, `posisi.csv` → `posisi`,
     `ekuitas.csv` → `ekuitas`, `ihsg-6bulan.csv` → `ihsg`.
   - Kripto, dari `idx_bot/crypto_paper/dashboard/`: `screener.csv` → `kripto_screener`, `posisi.csv` → `kripto_posisi`,
     `ekuitas.csv` → `kripto_ekuitas`, `btc-6bulan.csv` → `btc`.
6. Commit `idx_bot/paper`, `idx_bot/runs` dan `idx_bot/crypto_paper`, push ke `ccr-1c2d0097-crqzw5`.
7. Laporan di chat: kondisi IHSG atau BTC, kejadian (beli/jual/stop), keputusan tiap kandidat dan alasannya,
   ekuitas dan status rem 6%, link dashboard. Singkat.
8. Kirim notifikasi HP (PushNotification, satu baris di bawah 200 karakter) di akhir setiap run, karena pemilik
   tidak membuka aplikasi. Mulai dengan yang paling penting, misalnya "LQ45 19 Okt: BELI ICBP 4 lot, ekuitas Rp 10,0 jt"
   atau "Kripto 19 Okt: BELI BTC 0,0026, LEWATI NEAR; ekuitas 555,6 USDT". Bila harus berhenti (risiko besar,
   data aneh, tes gagal), kirim notifikasinya segera dengan kata "PERLU KEPUTUSAN".

## Catatan

- Susunan LQ45 berganti awal November 2026: cek pengumuman BEI dan perbarui `config.LQ45`,
  dengan memberi tahu pemilik apa yang berubah.
- Masa uji 2–4 minggu sejak 19 Oktober 2026. Setelah 2 minggu, kirim evaluasi kinerja kedua pasar dan tanyakan
  apakah lanjut.
- Data kripto diambil dari Binance lewat library python-binance (fork pemilik, aamutakaliman-gif/python-binance),
  termasuk aturan pasangan koin (kelipatan jumlah, kelipatan harga, minimum order 5 USDT). Bila Binance tidak bisa
  dijangkau, otomatis memakai Yahoo; `price_source` di laporan harian menyebut sumbernya. Sebutkan di laporan bila
  sumbernya Yahoo. Data publik memakai data-api.binance.vision, karena api.binance.com menolak (451) wilayah server ini.
- Binance Spot Testnet (uang mainan): bila BINANCE_TESTNET_API_KEY dan BINANCE_TESTNET_API_SECRET ada di environment,
  run kripto membeli posisi paper yang baru terisi di testnet dan memasang OCO (target dan stop) dengan jarak yang sama
  dari harga isi testnet, lalu menutupnya saat posisi paper ditutup. Field `testnet` di laporan menyebut statusnya;
  laporkan setiap baris "testnet ...", dan perlakukan "GAGAL" sebagai hal yang perlu diketahui pemilik. Kinerja tetap
  dinilai dari buku paper, karena harga testnet tidak sama dengan pasar asli.
- Uang sungguhan di Binance hanya setelah pemilik setuju, memakai API key yang dibuat pemilik dengan izin trading saja,
  tanpa izin penarikan, dan dikunci ke IP server.
