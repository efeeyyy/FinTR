# İleri günlük (forward log): "sonuçtan önce yazıldı" kanıtı

Kaynak: `docs/research/signal_bot_best_practices_2026-10-13.md` D3. Önceki durum:
günlük gözlem/makbuz dosyalarının "sonuçtan önce yazıldığı" yalnızca yerel dosya
zamanına dayanıyordu (kolayca değiştirilebilir). Bu araç günlük dosyaların SHA-256
özetlerini hash zincirli, yalnız-ekleme bir manifestoya yazar; manifesto git'te
izlenir (commit/etiket dış çapa olur).

Kod: `src/fintr/forward_log/core.py`, CLI: `scripts/forward_log.py`, testler:
`tests/test_forward_log.py`. Manifesto: `forward_log/manifest.jsonl` (yalnız özet,
yol ve boyut içerir; veri içeriği yoktur).

## Kayıt biçimi (`fintr_forward_log_v1`, satır başına bir JSON)

`seq` (1'den), `as_of`, `appended_at_utc` (yazıldığı andaki yerel saat, UTC),
`files` (`path` veri köküne göreli POSIX yol, `sha256`, `size`), `code_commit`
(yoksa `null`), `revision`, `backfilled`, `supersedes_seq` (yalnız revizyonda),
`prev_hash` (ilk kayıtta 64 sıfır), `entry_hash`. `entry_hash` = `entry_hash`
alanı çıkarılmış kaydın kanonik JSON'unun (anahtarlar sıralı, boşluksuz, UTF-8)
SHA-256'sıdır.

Günün dosyaları: `scanner_daily/<as_of>/`, `notify/<as_of>/` (makbuzlar dahil,
`note_receipt*.json` de) ve `llm_notes/<as_of>/` altındaki **tüm** dosyalar
(özyinelemeli; paket, ham yanıt, kayıt dahil), her biri dosya baytlarının SHA-256'sı.
Girdi olan `daily_capture` ve fiyat dosyaları kapsam dışıdır.

## Komutlar

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\forward_log.py append --as-of 2026-10-09
.\.venv\Scripts\python.exe scripts\forward_log.py verify --check-files
.\.venv\Scripts\python.exe scripts\forward_log.py backfill --from 2026-09-30 --to 2026-10-09
```

- `append`: aynı as_of, aynı özetler -> işlem yok (`noop`). Aynı as_of, farklı özetler ->
  reddedilir (çıkış 1); gerçekten değiştiyse `--revision` ile yeni bir revizyon kaydı
  eklenir (eski satırlara dokunulmaz). Bozuk manifestoya ekleme yapılmaz.
- `verify`: `seq` sürekliliği, `prev_hash` bağları, `entry_hash`, geçerli şema, zaman
  damgasının geri gitmemesi, aynı as_of'un ikinci kaydının revizyon olması. `--check-files`
  her as_of'un **son** kaydını diskteki dosyalarla karşılaştırır (eksik, değişmiş, kayıtta
  olmayan fazladan dosya). Herhangi bir kopmada çıkış 1. Çıktıdaki `head=` değerini
  etikete/commit mesajına yazmak istenirse not edilebilir.
- `backfill`: `scanner_daily` altında gözlem dosyası olan geçmiş tarihleri sırayla
  `backfilled: true` ile ekler. **Dürüst sınır:** bu kayıtlar yalnız "bu dosyalar şu anda
  bu içerikteydi"yi gösterir; eklendikleri andan önceki zaman hakkında hiçbir şey kanıtlamaz.

## Boru hattı (isteğe bağlı)

`run_daily_pipeline.py --send --forward-log`: bildirim gittikten (veya makbuz zaten
varsa) sonra son adım olarak `append` çalışır; özet satırı `forward_log: appended | noop |
skipped_dry_run | failed:...`. Hata bildirimi ve çıkış kodunu değiştirmez. Dry-run
bildirimde makbuz oluşmadığından kayıt yazılmaz (sonradan revizyon gerektirirdi).

## Runbook (günlük)

1. Günlük çalıştırmadan sonra (`--forward-log` verilmediyse elle) `append --as-of <as_of>`.
2. `forward_log/manifest.jsonl` dosyasını commit et. Düzenli aralıkla (ör. haftalık)
   `verify --check-files` çalıştır ve `git tag forward-log-YYYY-MM-DD` ile etiketle;
   mümkünse commit'i/etiketi uzak depoya (origin) it. Uzak depodaki commit zamanı yerel
   dosya zamanından çok daha zor değiştirilir.

## Sınırlar (dürüstçe)

- `appended_at_utc` yerel saattir; dış bir zaman damgası değildir. Kanıt gücü, manifestonun
  commit'inin uzak depoda (veya bağımsız bir kopyada) ne zaman göründüğünden gelir.
- Zincir, **dosyanın sonundan satır kesmeyi** tek başına göstermez; bunu önceki commit/etiket
  ve not edilen `head=` yakalar.
- Dosyaları ve manifestoyu birlikte yeniden yazmak mümkündür; dış çapa (git push, isteğe
  bağlı OpenTimestamps benzeri harici damga, [belirsiz]) olmadan yerel kanıt zayıftır.
- Makbuzu sonradan değişen gün (ör. v2'de kısmi gönderimin tamamlanması) için `--revision`
  gerekir; revizyonlar zincirde görünür kalır.
- Bu araç bir performans değerlendirmesi değildir; holdout kuralları aynen geçerlidir. K4, Efe'nin 2026-10-16 kararıyla (`docs/prereg/DECISIONS.md`) yalnız 20 geliştirme hissesi için kaldırıldı; holdout sembolleri hâlâ izlenmez (isabet izleme: `accuracy.md`).

## Karar ve alternatifler

- Seçilen: tek dosyalı JSONL hash zinciri + git. Küçük, bağımlılıksız, `git diff` ile okunur.
- Reddedilen: her dosya için ayrı imza dosyası (sıra ve silme kanıtı yok); ağ gerektiren
  harici zaman damgası servisi (bağımlılık/[belirsiz]; sonradan eklenebilir, `head=` damgalanır).
- Reddedilen: tek `as_of` için çoğa-tek özet (Merkle); dosya sayısı küçük, düz liste yeterli.
