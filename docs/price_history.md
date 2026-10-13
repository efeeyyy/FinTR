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

## Kaydedilmiş capture'ı yükleme ve dönem paketiyle eşleştirme

`fintr.data.price_history.load_price_history_capture`, kaydedilmiş bir
capture dosyasını okur ve `capture_metadata.normalized_sha256`'yı
`normalized` bölümünden yeniden hesaplayıp doğrular (dönem capture'larının
`packet_sha256_canonical_json`'ı doğrulamasıyla aynı desen); uyuşmazsa veya
şema beklenenden farklıysa `PriceHistoryCaptureError` fırlatır, sessizce
yoksaymaz.

`fintr.features.price_join.join_price_history`, bu şekilde yüklenen barları
bir dönem paketinin sunucu tarihlerine hizalar (placeholder/beklenmeyen işlem
barı sınıflandırması, hacim oranı kontrolü, düzeltme sınırı tarihleri, ve
Yahoo'nun `close`/`adjusted_close`'u aynı katsayıyla geriye dönük düzelttiği
durumlarda düzeltme sınırı tespitinin gözden kaçırdığı bir "sabit 1/k hacim
oranı rejimi" hipotezi); ayrıntı için `docs/features.md`'deki "Fiyat/hacim
eşleştirmesi" bölümüne bakın.
`scripts/analyze_period.py`, tek kayıt modunda `--price-history PATH`, toplu
modda ise sembol ve aralığa göre otomatik keşifle bu eşleştirmeyi raporun
`price_volume` bölümüne ekler.

Aynı sembol/aralık için daha önce kaydedilmiş bir dönem paketiyle
(`data/interim/period_analysis/...`) yalnızca tarih kapsamını karşılaştırmak
için (hiçbir fiyat/hacim değeri yazdırmadan):

```powershell
.\.venv\Scripts\python.exe scripts\fetch_price_history.py `
  --symbol AKFIS --start-date 2026-06-29 --end-date 2026-09-25 `
  --compare-capture data\interim\period_analysis\AKFIS\2026-06-29_2026-09-25.json
```

## Toplu (batch) çekim

`scripts/fetch_price_history_batch.py`, bir evren (universe) dosyasındaki
(`configs/universe/*.json`, `{"symbols": [...]}` alanı zorunlu) her sembol
için yukarıdaki tek-sembol çekim mantığını tekrar kullanır. Ekledikleri:
istekler arası CLI'dan ayarlanabilir, alt sınırı 1,5 saniye olan bekleme
(`--pacing-seconds`); hedef dosyası zaten var olan sembolleri atlama; bir
sembolün başarısız olması durumunda diğerlerine devam etme; ve kaynak hız
sınırlaması (HTTP 429) tespit edilirse kalan sembolleri hemen durdurup
"hammer" etmeme. Çıktı yalnızca sembol, durum (`saved`/`skipped`/`planned`/
`failed`), bar sayısı, `date_source` ve sıfır hacimli düz (placeholder) bar
sayısından oluşan bir tablo yazdırır; hiçbir fiyat veya hacim değeri
basmaz. `--save` verilmezse ağ bağlantısı açılmaz, yalnızca plan
(`planned`/`skipped`) yazdırılır. En az bir sembol başarısız olursa çıkış
kodu 1'dir.

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\fetch_price_history_batch.py `
  --universe configs\universe\bist30.json `
  --start-date 2023-01-02 --end-date 2026-09-25 --save
```

## Geriye dönük revizyon izleme (`diff_price_history.py`)

Yahoo resmî olmayan ikincil kaynaktır ve kayıtlı geçmişi sonradan değiştirebilir
(2026-10-10 elle fark: 2026-10-09'da çekilen dosyalarda 7 işaret satırı yoktu;
TUPRS/TRALT `adjusted_close` 932 tarihte revize edilmişti, ham OHLCV aynıydı).
Capture dosyaları üzerine yazılmadığı için revizyon, aynı sembolün **yeni bir
dosyasının bir önceki dosyayla çakışan tarihlerde uyuşmaması** olarak görünür.
`scripts/diff_price_history.py` (mantık: `fintr.data.price_revisions`) bunu
çevrimdışı tespit eder; ağ yok, fiyat dosyalarına dokunmaz.

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\diff_price_history.py `
  --out data\interim\price_revisions\2026-10-13\manual.json
# tek çift: --old <eski dosya> --new <yeni dosya> (aynı sembol; sıra verildiği gibi)
# --symbols AKBNK,TUPRS  --tolerance 1e-6  --price-dir <kök>
```

- Karşılaştırma: her `price_history/<SEMBOL>/` için (`_incomplete/` hariç) en yeni
  dosya ile bir öncekisi, ikisinin `requested_start..requested_end` kesişiminde.
  **Sıra çekim zamanına** (`capture_metadata.accessed_at_utc`) göredir, dosya adındaki
  bitiş tarihine göre değil (2026-10-09'da bitişi 10-08 olan dosya, 10-09 olandan sonra çekildi).
  Tek dosyalı sembol karşılaştırılmaz (`single_file`); kesişim yoksa `no_overlap`.
- Tarih başına tek satır, ilk eşleşen tür: `removed` / `added` (bar yalnız bir dosyada),
  `marker_status_changed` (`trading`/`placeholder`/`no_data_bar` sınıfı değişti; sınıf
  `fintr.features.price_join.classify_price_bar` kuralıdır, `unexpected_trading_bar`
  burada `trading` yazılır), `raw_ohlcv_changed` (open/high/low/close/volume),
  `adjusted_close_changed` (yalnız `adjusted_close`).
- Bağıl fark = `|a-b| / max(|a|,|b|)` (aynı alan, iki dosya). Tolerans altı
  (varsayılan `1e-6`, `--tolerance`) float gürültüsü sayılır, satır üretmez, yalnız
  `noise_ignored_dates` sayısına girer. Null <-> dolu geçişi her zaman değişikliktir.
- Önem (bir kural, istatistiksel eşik değil): `high` = işlem barı eklendi/silindi/sınıf
  değiştirdi ya da işlem barının ham OHLCV değeri değişti; `medium` = yalnız
  `adjusted_close` değişti, ya da işaret barı başka işaret sınıfına geçti / işaret barının
  ham değeri değişti; `info` = işaret satırı (`no_data_bar`/`placeholder`) eklendi/silindi.
- Rapor (`--out`, JSON, üzerine yazılmaz): `schema_version` (`fintr_price_revisions_report_v1`),
  `parameters`, her sembol için eski/yeni dosyanın yolu + `sha256` + `accessed_at_utc` +
  aralık + bar sayısı, tür/önem sayıları ve tarih satırları (tarih, tür, önem, eski/yeni
  sınıf, değişen alanlar, en büyük bağıl fark). Raporda fiyat/hacim **değeri yoktur**.
- Çıkış kodu: `0` değişiklik yok, `2` değişiklik bulundu (uyarı, hata değil), `1` hata
  (okunamayan/geçersiz dosya, bilinmeyen sembol, `--out` zaten var). Son stdout satırı
  `PRICE_DIFF: changes=N high=H medium=M info=I errors=E report=<yol>`.
- Sınırlar: yalnız ardışık iki dosyayı karşılaştırır; bir değişiklik bir sonraki yeni
  dosyaya kadar her çalıştırmada yeniden raporlanır. Tatil takvimi bu aracın dışındadır
  (`removed`/`marker_status_changed` bir tatil değerlendirmesi yapmaz). Revizyonun nedeni
  (kurumsal işlem, Yahoo kaynak değişikliği) bu araçla doğrulanmaz.

`scripts/run_daily_pipeline.py --price-diff` (varsayılan kapalı) aynı kontrolü başarılı
`collect` sonrasında çalıştırır ve raporu `data/interim/price_revisions/<çalışma günü>/
price_diff_<UTC damga>.json` altına yazar; özet satırı `price_diff: <none|changes:N|failed>`.
Bildirimi engellemez, çıkış kodunu değiştirmez; çalışmalar ve tarayıcı fiyatları eskisi gibi okur.
