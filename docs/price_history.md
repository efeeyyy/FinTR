# Fiyat/hacim geçmişi (price history)

## Kaynak ve kapsam

`scripts/fetch_price_history.py`, Yahoo Finance'ın herkese açık, resmî
olmayan chart endpoint'inden (`https://query1.finance.yahoo.com/v8/finance/chart/<SEMBOL>.IS`)
bir BIST sembolünün günlük OHLCV (open/high/low/close/volume) geçmişini
çeker. Bu **resmî olmayan ikincil bir kaynaktır**; Borsa İstanbul'un resmî
verisi değildir. İş Yatırım `HisseTekil` endpoint'i denendi ve hata döndü;
bu nedenle kullanılmadı.

İzin durumu her capture'ın `capture_metadata.permission_provenance.status`
alanında tam olarak `user_asserted_2026-09-26_not_independently_verified`
olarak kaydedilir: Efe 2026-09-26 tarihinde tüm veri erişimi için izni
olduğunu belirtmiştir; bu bağımsız olarak doğrulanmamıştır.

## Alanlar

- `open`, `high`, `low`, `close`: ham (unadjusted) günlük fiyatlar.
- `adjusted_close`: kaynağın ayrı döndürdüğü düzeltilmiş kapanış; `close` ile
  asla karıştırılmaz, ayrı alanda tutulur.
- `volume`: tam sayı; kaynak `null` verirse alan `None` kalır, asla `0`'a
  çevrilmez.
- `date`: unix timestamp'in `meta.exchangeTimezoneName` (IANA, örn.
  `Europe/Istanbul`) kullanılarak borsa yerel tarihine çevrilmiş hali.
  Çalışma ortamında IANA `tzdata` paketi kurulu değilse (bu proje bu şekilde
  gözlemlenmiştir) dönüşüm `meta.gmtoffset`e düşer; kullanılan yöntem her
  capture'da `normalized.date_source` alanında (`exchange_timezone_name`
  veya `gmtoffset_fallback`) kaydedilir.

## Provenance

Her capture dosyası `fintr_price_history_capture_v1` şemasıyla üç bölüm
içerir: `capture_metadata` (kaynak, istek URL'si, erişim zamanı, izin
provenance'ı, ham ve normalize edilmiş içeriğin SHA-256 özeti, dönüşüm
açıklaması), `raw_response` (kaynağın döndürdüğü JSON, olduğu gibi) ve
`normalized` (katı tipli, doğrulanmış OHLCV serisi). İstenen
`[start_date, end_date]` aralığının dışında kalan noktalar atılır ve sayıları
`normalized.dropped_before_start_count` / `normalized.dropped_after_end_count`
alanlarında saklanır.

## Çalıştırma

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\fetch_price_history.py `
  --symbol AKFIS --start-date 2026-06-29 --end-date 2026-09-25 --dry-run
```

`--dry-run` yalnızca istek URL'sini ve hedef kayıt yolunu yazdırır; ağ
bağlantısı açmaz. Gerçek bir çekim ve kayıt için `--save` eklenir; hedef
`data/interim/price_history/<SEMBOL>/<BAŞLANGIÇ>_<BİTİŞ>.json` zaten varsa
üzerine yazılmaz.

Aynı sembol/aralık için daha önce kaydedilmiş bir dönem paketiyle
(`data/interim/period_analysis/...`) yalnızca tarih kapsamını karşılaştırmak
için (hiçbir fiyat/hacim değeri yazdırmadan):

```powershell
.\.venv\Scripts\python.exe scripts\fetch_price_history.py `
  --symbol AKFIS --start-date 2026-06-29 --end-date 2026-09-25 `
  --compare-capture data\interim\period_analysis\AKFIS\2026-06-29_2026-09-25.json
```
