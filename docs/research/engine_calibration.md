# Motor kalibrasyonu (sentetik veri, A4)

Tarih: 2026-10-13. Kaynak madde: `docs/research/signal_bot_best_practices_2026-10-13.md` A4.
Bu belge **yalnız sentetik veriyle** dondurulmuş çalışma motorunun (E1 hareketli blok bootstrap,
Holm m=2, 5 gün ufku, taraf başına 10 bps) yanlış-pozitif oranını ve gücünü ölçer. Hiçbir gerçek
veri, getiri, p-değeri, holdout verisi veya `data/interim/studies/*` okunmaz; sonuçlar piyasalar
hakkında değildir ve yatırım tavsiyesi değildir. Motor ve dondurulmuş spec'ler değişmez.

**Durum: sonuç bölümü henüz yok.** Tam koşu yarıda kesildi (CPU ısınması); `--resume` ile
devam edecek. Sonuçlar tamamlanınca bu belgenin "Sonuçlar" bölümüne eklenir.

## Yöntem

- **Dikiş (seam).** `fintr.research.report.run_study` içine, bellekte kurulmuş
  `PeriodCapture`/`PriceHistoryCapture` nesnelerinden `load_symbol_data` ile üretilen `SymbolData`
  verilir (yükleyiciler atlanır; mevcut `tests/test_study_engine.py` de aynı yolu kullanır).
  Spec dosyaları `load_study_spec` ile **olduğu gibi** yüklenir (H1, H2: koşul eşikleri,
  `block_bootstrap` L=5, B=9999, `run_parameters`: 10 bps/taraf vb.); Holm kararı gerçek
  `fintr.research.family.evaluate_primary_family` ile verilir.
- **Üreteç** `tests/synthetic_market.py`: 20 sembol (`SYN01`..) x 250 işlem günü, tohumlu rastgele
  yürüyüş; AKD (`daily_akd_net`, `daily_akd_market`), takas ve metadata sentetik. `vwap_proxy_vs_close`
  girdisi (broker `total_cost` ile) getirilerden bağımsız çekilir; H1 ve H2 koşulları günlerin ~%10'unda,
  H3/H4 ~%1-3'ünde tetiklenir. Üreteç bayrakları motorun kendi olay kümeleriyle birebir eşleşir
  (testle doğrulanır). İleri 5 günlük getirinin sd'si ~%5 (README güç varsayımı `σ_5`).
- **Varyantlar.** `iid`: bağımsız normal getiri, piyasa faktörü payı %40. `dependent`: GARCH(1,1)
  oynaklık kümelenmesi (piyasa ve hisse bazlı), Student-t(6), zayıf AR(1), koşul olasılığının piyasa
  oynaklığıyla ve kalıcı bir piyasa-geneli gizli değişkenle birlikte hareket etmesi (koşullu günler
  zamanda kümelenir). Boş hipotezde hiçbir getiri koşul bayrağına bağlı değildir.
- **Boş hipotez (NULL).** Etki yok; FWER = en az bir Holm reddi oranı (nominal 0,05), hipotez bazında
  ham `p <= 0,025` / `<= 0,05` ve Holm red oranları, p-değeri dağılımı, `bootstrap SE / gözlenen fark SD`.
- **Yerleştirilmiş etki (ALT).** H1 için −d, H2 için +d (tahmin edilen yönde); olay başına log-getiri
  kaymaları, gürültüsüz koşullu−baseline fark **tam d** olacak şekilde panel başına çözülür (örtüşen 5
  günlük pencereler ve baseline'ın koşullu olayları içermesi nedeniyle olay başı kayma d'ye eşit
  değildir). d = 0,01, 0,02, 0,03. NULL ve ALT aynı gürültüyü kullanır (ortak rastgele sayılar).
- **README güç modeli** (`docs/prereg/README.md` "Güç / fizibilite"): `MDE ≈ 2,80 σ_5/√n_eff`
  (tek yönlü 0,025). Betik `power = Φ(d√n_eff/σ_5 − 1,96)`'yı n_eff = 50 ve 170 (README'nin 250 gün x 20
  hisse aralığı) ve bootstrap SE'sinden çıkan n_eff için hesaplayıp deneysel güçle yan yana yazar.
- **Güven aralıkları:** tam (Clopper-Pearson) iki yönlü %95 binom aralığı.
- **Sapmalar** (JSON `deviations` alanında da): yalnız ufuk 5 kurulur (diğer ufuklar Holm girdisine
  girmez); ikincil i.i.d. bootstrap tanılaması 20 çekimle (dondurulmuş 2000; hiçbir karara girmez); E1
  bootstrap tohumu replikat başına türetilir (dondurulmuş tek sabit tohumun yerine; replikatlar
  bağımsız olsun diye); semboller `SYN01`.. ve `symbol_holdout` boş; E1 B=9999 ve L=5 dondurulmuş.

## Nasıl çalıştırılır

```powershell
$env:PYTHONPATH="src"
.\.venv\Scripts\python.exe scripts\calibrate_engine.py --out data\interim\research\engine_calibration\2026-10-13 `
  --replicates-null 700 --replicates-alt 150 --workers 6
```

- Çıktı: `calibration.json` (şema `fintr_engine_calibration_v1`, commit, tohumlar, süre, replikat
  ayrıntıları) ve `calibration.md`. `--out` boş değilse çalışmaz (üzerine yazmaz).
- **Süre ve `--workers`.** Motor koşul değerlendirmesinde sembol/gün başına `daily_market_proxy`'yi tüm
  geçmiş için yeniden hesaplar (O(T²)); bir replikat (H1+H2) tek çekirdekte ~25-40 sn sürer. 14 işçiyle
  ölçüm: ~3,5 sn/replikat (toplam duvar süresi). Varsayılan `--workers` en çok 12'dir ve 2 çekirdek
  boş bırakır; **dizüstünde uzun sürüp ısınır, örn. `--workers 6` kullanın** (süre orantılı uzar).
- **Yarıda kesilirse devam.** Her biten replikat `replicates.partial.jsonl`'e eklenir. Aynı
  seçeneklere `--resume` eklenir; betik dosyayı doğrular (tohumlar, hipotez kümesi, varsa başlık
  satırı; yarım son satır atılır), biten (senaryo, r) çiftlerini atlar, kalanları ekler ve nihai
  çıktıyı kesintisiz bir koşuyla aynı sonuçlarla yazar (tohumlar replikat başınadır; testle
  doğrulanır). İlk betik sürümü başlık satırı yazmadığından o dosyada yalnız tohumlar doğrulanır;
  `--n-symbols/--n-days/--block-resamples` gibi seçenekleri ilk komutla aynı vermek kullanıcının
  sorumluluğudur.

```powershell
.\.venv\Scripts\python.exe scripts\calibrate_engine.py --out data\interim\research\engine_calibration\2026-10-13 `
  --replicates-null 700 --replicates-alt 150 --workers 6 --resume
```

## Testler

- Hızlı (varsayılan paket): `tests/test_engine_calibration.py` — üreteç belirlenimi, koşul sıklıkları,
  üreteç bayraklarının motor olay kümeleriyle eşleşmesi, boş hipotezde bağımsızlık, ALT farkının tam
  çözümü, istatistik yardımcıları, küçük uçtan uca koşu, `--resume` = kesintisiz koşu.
- Yavaş: `FINTR_SLOW_TESTS=1` ile, 12 sembol x 150 gün, R=300/varyant, FWER'in tam %95 aralığının
  alt sınırının ≤ 0,05 ve nokta tahmininin ≤ 0,10 olduğunu doğrular (birkaç dakika, çok çekirdek).

## Sonuçlar (2026-10-14, R_null=700, R_alt=150, commit `3af447c`)

Çıktı: `data/interim/research/engine_calibration/2026-10-13/calibration.{json,md}` (Git dışı).
Yalnız sentetik veri; piyasalar hakkında bir şey söylemez.

| Senaryo | H1 Holm red | H2 Holm red | Aile (herhangi bir red) | SE_boot / SD_gözlem |
|---|---|---|---|---|
| iid / null | 2,7% [1,6; 4,2] | 2,9% [1,8; 4,4] | 5,3% [3,7; 7,2] | 0,98 / 0,96 |
| dependent / null | 6,6% [4,9; 8,7] | 5,0% [3,5; 6,9] | 10,3% [8,1; 12,8] | 0,75 / 0,76 |

Güç (Holm red): iid'de 0,01 etkide ≥99%; dependent'ta 0,01: 67% / 70%, 0,02: 98% / 96%,
0,03: ≥99%; README güç modeliyle (n_eff ≈ 130–140) uyumlu.

Yorum: Bağımsız günlerde motor nominal düzeyde (aile ≈ %5). Kalıcı, piyasa genelinde
kümelenen koşul + GARCH + AR(1) içeren `dependent` senaryoda E1 blok bootstrap (L=5) standart
hatayı ~%25 düşük tahmin ediyor ve aile yanlış-pozitif oranı nominalin yaklaşık iki katı
(%10,3). Bu senaryonun bağımlılık düzeyi bir modelleme varsayımıdır (`latent_phi=0.9`,
`latent_loading=0.35`); gerçek koşul kümelenmesi aşağıdaki T-29 bölümünde ölçüldü.
Ortak piyasa payı (0,4) gerçek train verisiyle uyumlu (TRIAL_LOG T-28: ort. ikili korelasyon
0,41). Dondurulmuş spec ve train sonuçları değişmedi; olası değişiklik yalnız Efe kararıyla,
holdout'tan önce `DECISIONS.md`'ye yazılarak yapılır.

## Gerçek koşul bayrağı kümelenmesi (T-29, 2026-10-14)

`scripts/measure_condition_clustering.py`: 20 geliştirme hissesi, train 2025-09-26..2026-09-25;
yalnız dondurulmuş H1-H4 koşullarının (sembol, tarih) bayrağı, motorun kendi koşul kodu ve
veri-kalitesi düşüşleriyle (ratio-regime, değerlendirilemeyen, takas ölçek sıçraması).
Getiri, ortalama veya çalışma çıktısı hesaplanmadı/okunmadı. Aynı istatistikler sentetik `iid` ve
`dependent` dünyalarda (varsayılan `MarketConfig`, null, 5 tohum) hesaplandı. Çıktı:
`data/interim/research/condition_clustering_2026-10-14.{json,md}` (Git dışı).

| H1 / H2 | gerçek | iid | dependent |
|---|---|---|---|
| bayrak sıklığı | 0,135 / 0,168 | 0,10 | 0,07 |
| dağılım indeksi (aynı tarihte semboller arası; 1 = bağımsız) | 5,17 / 5,05 | 1,00 / 0,98 | 2,35 / 1,81 |
| sembol başına bayrak otokorelasyonu lag 1 | 0,00 / 0,03 | 0,00 / 0,00 | 0,16 / 0,16 |
| günlük bayrak sayısı otokorelasyonu lag 1 / lag 5 | -0,02, -0,10 / 0,08, -0,09 | ~0 | 0,56, 0,33 / 0,43, 0,26 |
| tasarım etkisi, zaman kısmı (K=10) | 0,94 / 1,57 | 1,0 / 0,92 | 5,05 / 3,78 |
| tasarım etkisi, toplam (K=10) | 4,8 / 7,9 | 1,0 / 0,9 | 12,6 / 7,4 |

Bulgu: gerçek bayraklar **aynı gün semboller arasında** `dependent` dünyadan da güçlü kümelenir
(bayrakların %80-85'i ≥3 bayraklı tarihlerde), ancak **zaman içinde** kalıcılık yoktur
(otokorelasyonlar, sembol başına ve günlük sayı için, bağımsızlık referansının aralığında; yalnız
H2 sayı lag 10 = 0,18 aralık dışına yakın). `dependent` dünyası ise her iki boyutta kalıcıdır
(`latent_phi=0,9`). E1 yeniden örnekleme birimi tarihtir, bu yüzden aynı-gün bağımlılığı
yapısal olarak korunur; L=5'in yakalayamadığı kalıcı (L'den uzun) bağımlılık gerçek bayraklarda
görünmüyor. Bu, `dependent` sonucunun (FWER %10,3) gerçek veriye doğrudan aktarılamayacağını
gösterir ama bayrak-getiri bağımlılığını (üreteçteki volatilite bağımlılığı) ölçmez; ayrıştırma için
"aynı-gün kümelenmiş, zamanda bağımsız" bir sentetik senaryo sonraki adımdır. H3/H4 bayrakları
çok seyrek (10 ve 8 bayrak) olduğu için bilgi vermez.

## Sınırlar

- Sentetik üreteç gerçek BIST dinamiklerini temsil etmez (fiyat limiti, tatiller, açılış/kapanış
  seansları, gerçek AKD yapısı, gerçek kümelenme yoktur); sonuç motorun **istatistik düzeneğinin**
  kalibrasyonudur, stratejinin veya piyasanın değil.
- Yalnız tek bir bağımlılık biçimi ailesi denenir; gerçek veride farklı bir bağımlılık yapısı
  farklı sonuç verebilir.
- Replikat sayısı sonlu: FWER için aralık genişliği yaklaşık ±%1,5-2 puan; küçük sapmalar ayırt edilemez.
- Bulunan her motor sorunu **düzeltilmez**; asgari yeniden üretimle raporlanır, düzeltme `DECISIONS.md`
  kaydı ve Efe kararı gerektirir.
