# Runbook harian paper trading LQ45

Dijalankan Claude setiap hari bursa setelah penutupan. Pemilik akun membaca hasilnya di chat dan di dashboard
https://claude.ai/artifact/NSiEW7U2ivbXtpCfpcHnyn. Semua pesan ke pemilik dalam Bahasa Indonesia.

## Aturan yang disetujui pemilik

- Paper trading saja, modal virtual Rp 10 juta, LQ45. Tidak ada order sungguhan.
- Elder: risiko maks. 2% per trade, rem 6% per bulan (rugi bulan ini + risiko terbuka).
- Risiko besar atau kesalahan: hentikan langkah berikutnya dan beri tahu pemilik dulu.

## Langkah

1. `git pull origin ccr-1c2d0097-crqzw5`, lalu `pip install -q -e . pytest` bila paket belum terpasang.
2. `python -m pytest tests/test_idx_bot.py -q`. Kalau gagal, berhenti dan laporkan.
3. `python -m idx_bot.daily run`. Kalau `date` sama dengan laporan sebelumnya, bursa libur: kirim satu baris saja.
4. Untuk setiap kandidat (maks. 5), jalankan tim analis secara singkat:
   - Teknikal: baca baris kandidat di `runs/screener-<date>.json`.
   - Fundamental: angka di baris yang sama.
   - Berita: WebSearch berita emiten dan pasar hari itu (aksi korporasi, suspensi, UMA, isu MSCI).
   - Bull vs bear: satu alasan terkuat masing-masing.
   - Risiko: rencana stop/target/lot di laporan; tolak bila ada kabar buruk yang belum tercermin di harga.
   - Keputusan BELI atau LEWATI. BELI: `python -m idx_bot.daily buy KODE --reason "<alasan singkat>"`.
5. Unggah `idx_bot/paper/dashboard/{screener,posisi,ekuitas}.csv` ke dashboard (Artifact `asset: true`),
   lalu dengan ArtifactData `update` dataset `screener`, `posisi`, `ekuitas`: `source.url`, `source.name`
   dan `updated` (baca versi dokumen dulu).
6. Commit `idx_bot/paper` dan `idx_bot/runs`, push ke `ccr-1c2d0097-crqzw5`.
7. Laporan di chat: kondisi IHSG, kejadian (beli/jual/stop), keputusan tiap kandidat dan alasannya,
   ekuitas dan status rem 6%, link dashboard. Singkat.
8. Kirim notifikasi HP (PushNotification, satu baris di bawah 200 karakter) di akhir setiap run, karena pemilik
   tidak membuka aplikasi. Mulai dengan yang paling penting, misalnya "LQ45 19 Okt: BELI ICBP 4 lot, ekuitas Rp 10,0 jt"
   atau "LQ45 19 Okt: tidak ada sinyal, IHSG -1,2%". Bila harus berhenti (risiko besar, data aneh, tes gagal),
   kirim notifikasinya segera dengan kata "PERLU KEPUTUSAN".

## Catatan

- Susunan LQ45 berganti awal November 2026: cek pengumuman BEI dan perbarui `config.LQ45`,
  dengan memberi tahu pemilik apa yang berubah.
- Masa uji 2–4 minggu sejak 19 Oktober 2026. Setelah 2 minggu, kirim evaluasi kinerja dan tanyakan apakah lanjut.
