# İsabet izleme (`track_accuracy.py`, `fintr.accuracy`)

Karar: Efe, 2026-10-16 (`docs/prereg/DECISIONS.md`, deneme kaydı `docs/prereg/TRIAL_LOG.md` T-31).
K4 yalnız **20 geliştirme hissesi** için kaldırıldı; 10 holdout hissesi **izlenmez**
(kod koruması reddeder). Kurallar, eşikler ve spec'ler dondurulmuştur. Çıktı yalnız yerel
dosyadır; **Telegram'a, bildirime ve analiz notuna girmez.** Bu bir doğrulama değil,
tanımlayıcı izlemedir; yatırım tavsiyesi veya al/sat sinyali değildir.

## Ne ölçülür

Her sinyal günü `t` (varsayılan: veri olan son 42 işlem günü) ve her dondurulmuş kural
(H1-H4, `configs/scanner/rules_v1.json`, hash doğrulamalı) için kuralın koşulu `t`'de
tutuyor mu (`condition_met`). Tutuyorsa, kuralın dondurulmuş `predicted_direction` değeriyle
gerçekleşen yön karşılaştırılır:

- **Getiri tanımı motorunkiyle aynıdır** (`fintr.research.engine._price_return`, yeniden
  yazılmadı): giriş `t`'den sonraki ilk işlem gününün **açılışı**, çıkış girişten `vade` işlem
  günü sonraki **kapanış**; tatil/yer tutucu barlar işlem günü sayılmaz
  (`tradeable_price_bars`); vade penceresine düzeltme sınırı (corporate action) girerse
  olay **düşer** (`dropped`, spec politikası `drop`).
- **Vadeler:** birincil 5 işlem günü; ikincil 1 işlem günü (spec'lerin `horizons` listesinde).
- **İsabet:** brüt getirinin işareti tahmin yönüyle aynı. **Sıfır getiri isabet değildir**,
  ayrı sayılır (`zero_return`). **Maliyetli isabet:** tahmin yönünde `|getiri| > 2x10 bp`
  (gidiş-dönüş maliyeti, specin `cost_bps_per_side` = 10).
- **Bekleyen (`pending`):** vadesi kayıtlı fiyat geçmişinde henüz dolmamış olay; tahmin
  edilmez, sonuçlanana kadar ayrı sayılır. Fiyat dosyası eskiyse bekleyen sayısı büyür.
- **Tahmin kaynağı (`prediction_source`):** `forward_log` (kayıtlı tarama, dosya özeti
  `forward_log/manifest.jsonl`'de **backfill olmayan** bir kayıtla eşleşiyor),
  `saved_scan` (kayıtlı `scanner_daily/<gün>/observations.json` var; ileri günlükte
  doğrulanmış, backfill olmayan kaydı yok -- backfill kaydı yalnız "dosya o anda bu içerikteydi"
  der), `reconstructed` (yeniden kurulan: aşağı bakın). Kayıtlı tahmin varsa o tercih edilir ve
  yeniden kurulumla karşılaştırılır (`reconstruction_check`: karşılaştırılan/uyuşan/uyuşmayan).

## Neden taban oranı

Bir yönün isabet oranı tek başına bir şey söylemez: fiyatlar zaten çoğu dönemde bir yöne
meyilli olabilir ve bu pencerede sıfır getirili günler de çoktur. Bu yüzden her grup için
**aynı hisseler, günler, dönem ve vade** üzerinde koşulsuz yön oranı hesaplanır (koşulsuz tüm
hisse-günlerin getirisinin tahmin yönünde olma oranı; motorun baseline'ı gibi ratio-regime
günleri düşülür) ve `fark = isabet oranı - taban oranı` raporlanır. Yorum farka bakılarak
yapılmalıdır, isabet oranına değil. Wilson %95 aralığı da verilir.

## Dönemler: asla tek sayıda toplanmaz

- `train_window`: sinyal günü <= 2026-09-25. Kurallar bu veriyle kuruldu (örneklem içi);
  burada iyi görünmek kanıt değildir.
- `post_train`: sinyal günü >= 2026-09-26. Satır alanı `in_test_window` sinyal günü
  >= 2026-10-01 olanı işaretler (birincil holdout penceresi). **Bu günler geliştirme
  sembolleri için artık kör değildir**; 30 sembollü birincil test (Holm H1+H2) planlandığı gibi
  kalır ama geliştirme sembolleri bakımından kör sayılmaz; E5 (yalnız holdout) kör kalır.
  Bir kural bu sayılar görüldükten sonra değişirse, o kuralın holdout penceresi **değişiklik
  tarihinde yeniden başlar** ve `DECISIONS.md` ile `TRIAL_LOG.md`'ye yazılır.

## Örneklem büyüklüğü

42 işlem günü, 20 hisse ve 4 kuralla sinyal sayıları küçüktür ve **bağımlıdır**: vadeler
birbirine biner (5 günlük pencere), aynı gün birçok hisse birlikte hareket eder, aynı
hisse-gün birden çok kuralda görünebilir (H1/H3, H2/H4 koşulları örtüşür; `toplam` satırı
bağımsız gözlem sayısı değildir). Wilson aralığı olayları bağımsız varsayar, yani gerçek
belirsizlik raporlanandan geniştir; `distinct_signal_dates` etkin bağımsız sayıya kaba
bir üst sınırdır. Hiçbir p-değeri veya anlamlılık iddiası üretilmez.

## İleriye bakış sınırları (dürüstçe)

- **Yeniden kurulan (`reconstructed`) tahmin** yalnız `t` ve öncesi tarihli satırları görür;
  takasta yalnız `t`'ye kadar kesinleşmiş (T+2, `configs/market/settlement_cycle.json`)
  günleri; fiyatta yalnız `t`'ye kadarki barları. `t` sonrası verinin değişmesi tahmini
  değiştiremez (`tests/test_accuracy.py`). H1-H4 takas kullanmaz; maske yine de uygulanır.
- **Artık ileriye bakış:** `t` veya öncesi tarihli AKD/metadata satırları, `t`'den sonra
  alınmış yakalamalardan gelen nihai/revize değerler olabilir (taban yakalama 2026-09-26'da
  alındı: `train_window` günlerinin tamamı böyledir; günlük yakalamalar da önceki günleri yeniden
  okuyup revize edebilir). Çıktıdaki `look_ahead.exposure` bunu sayar (taban yakalamaya düşen
  gün, sonradan revize edilen gün, kesinleşmemiş AKD günü). Yanlılığın yönü bilinmiyor.
  Kayıtlı tahminin yeniden kurulumla uyuşması bu riski sınırlı bir örnekte ölçer, ortadan kaldırmaz.
- **Sonuç bilerek ileriye bakar** (giriş `t+1` açılışı, çıkış vade sonu); sorun değil, tanımdır.
- **Bildirim zamanı:** günlük toplama penceresi `R-1`'de biter; bir günün sinyali çoğu zaman
  `t+1` akşamı üretilir. Getiri tanımındaki `t+1` açılış girişi, bildirimi alan biri için
  gerçekleşebilir olmayabilir. Bu izleme dondurulmuş çalışma tanımını ölçer, işlem sonucunu değil.
- Fiyatlar resmî olmayan Yahoo verisidir; `vwap_proxy` doğrulanmamış AKD türevidir (spec
  açıklamaları). Kapanış fiyatı kaydında eksik/yarım son bar varsa gün `pending` kalır.

## Kullanım

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\track_accuracy.py
```

Varsayılan: 20 geliştirme hissesi, son 42 işlem günü, `data\interim\accuracy\<bugün>\`
altına `accuracy.json` (+ kısa Türkçe `accuracy.md`); **üzerine yazmaz** (aynı gün ikinci
çalıştırma çıkış kodu 3). Seçenekler: `--start/--end` veya `--last-n-days`, `--symbols`
(holdout/dışlanan reddedilir, çıkış 1), `--scanner-dir`, `--forward-log`, `--run-date`.
stdout yalnız çıktı yolunu ve **sayıları** (sinyal/sonuçlanan/bekleyen/düşen) basar, isabet
oranı veya getiri basmaz.

Günlük boru hattında opt-in: `run_daily_pipeline.py --accuracy` (varsayılan kapalı) taramadan
sonra çalışır, özet satırı `accuracy: saved|exists|failed:...`; hata/zaman aşımı (900 sn)
bildirimi ve çıkış kodunu değiştirmez; Telegram'a hiçbir şey gitmez.

## Çıktı (`fintr_accuracy_v1`)

Kod commit'i, kural dosyası ve spec hash'leri, tüm girdi dosyalarının SHA-256'sı
(dönem/günlük yakalama, fiyat, kayıtlı tarama), parametreler, split koruma bloğu, tahmin
kaynağı kayıtları (`prediction_provenance`), `look_ahead`, `reconstruction_check`,
`evaluation_counts` (koşul sağlandı/sağlanmadı/bastırıldı sayıları), formüller, dönem x vade x
grup özetleri (`summary`) ve her satır: `symbol, rule_id, t, predicted_direction,
prediction_source, horizon, outcome (hit|miss|zero_return|pending|dropped), cost_aware_hit,
gross_return, net_return, entry_date, exit_date, period, in_test_window`.

## Karar ve alternatifler

- Seçilen: kayıtlı tahmini tercih eden, yoksa `t` ve öncesi veriyle (kesin kapsamla) yeniden
  kuran yürüyen izleme; sonuç motorun kendi fonksiyonlarıyla; taban oranı ve Wilson aralığı.
- Reddedilen: `scan_observations.py --data-cutoff-utc` ile her gün için ayrı tarama
  (yalnız 2026-09-26'dan sonrası için anlamlı; taban yakalama tek dosya; ağır); bunun
  yerine tek bileşik yakalama ve gün başına kesme + T+2 takas maskesi.
- Reddedilen: sonuçları bildirime/analiz notuna sokmak (Efe: Telegram çıktısı yok).
- Reddedilen: p-değeri / anlamlılık testi (bağımlı ve küçük örneklem; planlı test holdout'ta).
