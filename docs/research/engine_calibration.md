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
## Aday yöntemler (2026-10-14, TRIAL_LOG T-30)

Amaç: sentetik dünyaların hepsinde nominale yakın, gücü makul bir varyans/çıkarım yöntemi bulmak; Efe
holdout'tan **önce** ön kayıtlı bir değişiklik olarak benimseyebilir. **Hiçbiri benimsenmedi**;
dondurulmuş E1 (L=5, B=9999), spec'ler, hash'ler ve motor varsayılanları değişmedi.

### L=5 neden liberal

Fark `Δ̂ = m_c − m_b`'nin tarih skorları `u_t = (C_t − n_t m_c)/N_c − (B_t − b_t m_b)/N_b`
(t günündeki tüm sembol olayları toplanır) için birinci mertebede `Δ̂ − Δ ≈ Σ_t u_t`,
`Var(Δ̂) ≈ Σ_k γ_u(k)`. Kabaca `u_t ≈ w_t (r̄_t − μ)`, `w_t = n_t/N_c − b_t/N_b`, yani
`γ_u(k) ≈ E[w_t w_{t+k}] · Cov(r_t, r_{t+k})`; 5 günlük ileri pencereler örtüştüğü için
`Cov(r_t, r_{t+k}) > 0` (k<5).

- `iid`/`xsection`: bayraklar zamanda bağımsız → `E[w_t w_{t+k}] ≈ 0` (k≠0); örtüşme kovaryansı
  sönümlenir. Aynı-gün kümelenmesi yalnız `γ_u(0)`'ı büyütür ve tarih yeniden örneklemesi onu
  yapısal olarak taşır. Ölçülen `u_t` otokorelasyonu lag 1-10'da ~0.
- `dependent`: kalıcı bayraklar (gizli φ=0,9, sembol φ=0,3, oynaklık bağı) → `u_t` otokorelasyonu
  lag 1-4'te 0,30 / 0,17 / 0,10 / 0,04, sonra hafif negatif.

Tanı (yalnız üreteç, motor yok; 200 sentetik dünya, H1; `dependent` H2 benzer): gerçek
`SD(Δ̂)`'ye göre ortalama DK varyansının karekökü
`sqrt(mean V_M)/SD(Δ̂)`:

| dünya | M=0 | M=5 | M=10 | M=20 | M=40 | M=60 |
|---|---|---|---|---|---|---|
| `iid` | 0,95 | 0,94 | 0,93 | 0,90 | 0,87 | 0,84 |
| `xsection` | 1,11 | 1,10 | 1,08 | 1,06 | 1,01 | 0,97 |
| `dependent` | 0,59 | 0,83 | 0,85 | 0,83 | 0,78 | 0,75 |
| `dependent`, `latent_phi=0` | 0,76 | 0,93 | 0,94 | 0,92 | — | 0,82 |
| `dependent`, GARCH yok | 0,63 | 0,83 | 0,86 | 0,86 | — | 0,81 |

Sonuç: `dependent`'ta açık **L=5 kesmesi değil**. Pencere büyütülünce oran ~0,85'te tepe yapıp
düşer: 250 tarihlik tek panelde kalıcı (düşük frekanslı) varyans parçası örneklem içi
otokovaryanslardan geri kazanılamaz (skorlar `Σ u_t = 0` ile merkezlendiği için uzun gecikmelerde
örneklem otokorelasyonu negatife çekilir, lag 11-30 toplamı −0,18; bilinen küçük örneklem HAC
yanlılığı). Uzun blok/gecikme bu yanlılığı azaltmaz, tahminin oynaklığını artırır. Buna uygun
olarak pilotta tüm adaylarda `dependent` SE/SD ≈ 0,75-0,81 kaldı (aşağıda). Önceki taslaktaki
"L=10/20 varyansın %84/%92'sini yakalar" yaklaşık hesabı (yalnız örtüşmeyi sayıyordu) **yanlış
çıktı** ve geri çekildi. Tanı betiği oturum scratchpad'indeydi (Git dışı; sonuç yalnız bu tablo).

### Tanımlar (aday kümesi koşudan önce sabitlendi)

Tüm adaylar aynı replikatta, dondurulmuş `run_study`'nin kullandığı **aynı** havuzlanmış 5 günlük
olaylarla (yeni, isteğe bağlı `run_study(event_sink=...)`), aynı min-olay kuralı ve gerçek
`holm_decisions` (m=2, α=0,05) ile değerlendirilir. Bootstrap adayları `frozen` ile aynı tohumu kullanır.

| ad | yöntem |
|---|---|
| `frozen` | E1 hareketli blok bootstrap, sinyal tarihi ekseni, L=5, B=9999 (referans) |
| `mbb10` | aynı motor fonksiyonu (`moving_block_bootstrap`), L=10, B=9999 |
| `mbb20` | aynı, L=20, B=9999 |
| `dk10` | Driscoll-Kraay: `u_t` üzerinde Bartlett HAC, 10 gecikme, `p = Φ(z)`/`1−Φ(z)` (normal referans) |
| `dk20` | aynı, 20 gecikme |

- Görevdeki "(b) tarih kümeli (kesitsel) blok bootstrap" ayrıca koşulmadı: motor zaten çekilen
  tarihin tüm sembol olaylarını (koşullu ve baseline) birlikte taşır, koşullu ve baseline ortalamayı
  aynı çekilmiş tarihler üzerinden alır; sentetik panelde her işlem günü bir sinyal tarihidir. Yani
  (b) L=5 = `frozen`, L=10 = `mbb10`.
- **Yeni dünya `xsection`** (TRIAL_LOG T-30 ek; yalnız T-29'un gerçek bayrak istatistiklerine göre,
  hiçbir yöntem sonucuna bakmadan): getiriler `iid` ile aynı (tek faktör, pay 0,4, normal); koşul
  girdisi günler arasında bağımsız bir piyasa-geneli gizli değişkene 0,65 yüklenir, `vwap` ölçeği
  0,00965. 5 tohumda dağılım indeksi 5,1/5,2, sıklık 0,15, bayrak/sayı lag-1 otokorelasyonu ~0
  (gerçek: 5,17/5,05, 0,135/0,168, ~0). Harness varsayılan varyantları `iid dependent` kaldı.
- Kod: `src/fintr/research/candidate_inference.py` (DK formülleri `FORMULAS`'ta),
  `scripts/calibrate_engine.py --method`, `tests/synthetic_market.py` (`xsection`). Testler:
  `tests/test_candidate_inference.py` (`run_study` ve `iid`/`dependent` dondurulmuş replikat çıktısı
  `44fed3a`'da hesaplanan özetlerle bayt bayt aynı; `event_sink` sonucu değiştirmez; aday yolunda
  L=5 dondurulmuş p/SE/Holm'u birebir verir; DK elle formül; `xsection` kümelenme ve olay eşleşmesi).

### Pilot (yalnız kod kontrolü; null, R=100, `--workers 4`, tohum 20261013)

`iid`/`dependent` commit `5f525c3` (1083 s), `xsection` commit `75c52dc` (529 s). Holm red oranı,
tam %95 aralık; SE/SD = ortalama SE / gözlenen fark SD'si. **PİLOT — karar için değil**: R=100'de
aile oranının aralığı ±~5 puan.

| varyant | yöntem | H1 Holm red | H2 Holm red | aile (herhangi red) | SE/SD H1 / H2 |
|---|---|---|---|---|---|
| iid | `frozen` | 5% [2; 11] | 4% [1; 10] | 7% [3; 14] | 0,88 / 0,98 |
| iid | `mbb10` | 6% [2; 13] | 5% [2; 11] | 9% [4; 16] | 0,86 / 0,96 |
| iid | `mbb20` | 8% [4; 15] | 5% [2; 11] | 11% [6; 19] | 0,82 / 0,92 |
| iid | `dk10` | 7% [3; 14] | 5% [2; 11] | 9% [4; 16] | 0,86 / 0,96 |
| iid | `dk20` | 8% [4; 15] | 6% [2; 13] | 11% [6; 19] | 0,82 / 0,92 |
| dependent | `frozen` | 8% [4; 15] | 1% [0; 5] | 9% [4; 16] | 0,79 / 0,75 |
| dependent | `mbb10` | 6% [2; 13] | 2% [0; 7] | 7% [3; 14] | 0,81 / 0,78 |
| dependent | `mbb20` | 6% [2; 13] | 4% [1; 10] | 9% [4; 16] | 0,80 / 0,75 |
| dependent | `dk10` | 8% [4; 15] | 2% [0; 7] | 10% [5; 18] | 0,81 / 0,78 |
| dependent | `dk20` | 7% [3; 14] | 2% [0; 7] | 9% [4; 16] | 0,80 / 0,76 |
| xsection | `frozen` | 3% [1; 9] | 2% [0; 7] | 5% [2; 11] | 1,00 / 0,98 |
| xsection | `mbb10` | 3% [1; 9] | 3% [1; 9] | 6% [2; 13] | 0,99 / 0,96 |
| xsection | `mbb20` | 3% [1; 9] | 4% [1; 10] | 7% [3; 14] | 0,97 / 0,93 |
| xsection | `dk10` | 3% [1; 9] | 3% [1; 9] | 6% [2; 13] | 0,99 / 0,96 |
| xsection | `dk20` | 3% [1; 9] | 4% [1; 10] | 7% [3; 14] | 0,97 / 0,93 |

Pilot okuması (kaba): hiçbir aday `dependent`'taki SE açığını kapatmıyor (yukarıdaki tanıyla
tutarlı); `iid`/`xsection`'da uzun blok/gecikme SE'yi biraz düşürüp aile oranını hafif artırıyor.
`xsection`'da `frozen` nominale yakın görünüyor. `dependent` için anlamlı bir aday ancak sabit-b
(Kiefer-Vogelsang) kritik değerli DK gibi örneklem içi yanlılığı hesaba katan bir referans dağılımı
olabilir; bu **koşulmadı**, ayrı bir TRIAL_LOG satırıyla önceden tanımlanması gerekir.

### Tam koşu (Efe başlatır; sıra önemli)

Kesilen koşu aynı komuta `--resume` eklenerek sürer. Her iki komut 50 sn başlatılıp öldürüldü ve
`--resume` ile temiz devam ettiği doğrulandı (geçici klasörde; başlık aynı, tekrar kayıt yok).
Süre tahmini: pilotta replikat başına ~20-21 sn CPU (adaylarla; yalnız `frozen` ~17 sn); 12
işçide termal/paylaşım kaybıyla ~3-4 sn/replikat duvar süresi varsayıldı (2026-10-13'te 14 işçi:
~3,5 sn).

1. **Belirleyici koşu — `frozen`, `xsection`, null R=700** (~700 replikat, ~35-50 dk):

```powershell
$env:PYTHONPATH="src"; $env:PYTHONIOENCODING="utf-8"
.\.venv\Scripts\python.exe scripts\calibrate_engine.py `
  --out data\interim\research\engine_calibration\2026-10-14_frozen_xsection `
  --variants xsection --replicates-null 700 --effect-sizes --workers 12
```

2. **Adaylar — `iid` + `dependent`, null R=700 + alt 0,01/0,02 R=150, tüm adaylar tek koşuda**
   (2000 replikat, ~1,7-2,5 sa). Olay kurulumu replikat başına bir kez yapıldığından alt
   senaryolarını yalnız en iyi 1-2 aday yerine tüm adaylar için koşmak ek maliyet getirmez
   (aday başına ~1,3 sn/replikat):

```powershell
.\.venv\Scripts\python.exe scripts\calibrate_engine.py `
  --out data\interim\research\engine_calibration\2026-10-14_candidates `
  --variants iid dependent --replicates-null 700 --replicates-alt 150 --effect-sizes 0.01 0.02 `
  --method frozen mbb10 mbb20 dk10 dk20 --workers 12
```

#### Belirleyici koşu: dondurulmuş yöntem, `xsection` dünyası (2026-10-14, R=700, commit `d1a277a`)

Çıktı: `data/interim/research/engine_calibration/2026-10-14_frozen_xsection/` (Git dışı).
`xsection`: gerçek train bayraklarına benzer (aynı günde güçlü kümelenme, zamanda süreklilik yok;
TRIAL_LOG T-29/T-30). Boş hipotez altında Holm red: H1 3,3% [2,1; 4,9], H2 2,9% [1,8; 4,4];
aile 6,0% [4,4; 8,0]; SE_boot/SD_gözlem 0,97 / 0,97. Güven aralığı nominal %5'i içeriyor:
gerçeğe benzeyen bağımlılık yapısında dondurulmuş E1 (L=5) yaklaşık nominal. `dependent`
dünyasındaki ~2x şişme zamanda kalıcı kümelenmeden geliyor; gerçek train bayraklarında bu
görülmedi. Karar: dondurulmuş yöntem değişmez, DECISIONS kaydı gerekmez. Sınır: yalnız bayrak
istatistiklerine dayalı benzerlik; bayrak–getiri/oynaklık bağı ölçülmedi; tek panel.

## Sınırlar (adaylar)

- L=20'de ~245 tarihte ~12 blok kalır; bootstrap varyans tahmini daha oynak olur (hem boyut hem
  güç etkilenebilir). Gerçek bayraklarda zaman kalıcılığı görülmedi (T-29), ama bayrak-getiri/
  oynaklık bağı ve getirilerin kendi bağımlılığı gerçek veride ölçülmedi.
- DK normal referansla, b = M/T ≈ 0,04–0,08'de sabit-b kuramına göre hafif liberal kalabilir;
  doğrusallaştırmaya dayanır (oran tipi istatistik).
- Adaylar aynı sentetik tohumlarda karşılaştırılır; "en iyi" adayı seçmek bu sentetik dünyaya hafif
  uyum (seçim iyimserliği) taşır. Bağımlılık düzeyi (`latent_phi=0.9`) bir modelleme varsayımıdır.
- Benimsenecek yöntem holdout'tan önce `DECISIONS.md`'ye yazılmalı ve dondurulmuş spec'lerin
  `block_bootstrap` değişikliği yeni spec hash'i gerektirir (Efe kararı).

## Sınırlar

- Sentetik üreteç gerçek BIST dinamiklerini temsil etmez (fiyat limiti, tatiller, açılış/kapanış
  seansları, gerçek AKD yapısı, gerçek kümelenme yoktur); sonuç motorun **istatistik düzeneğinin**
  kalibrasyonudur, stratejinin veya piyasanın değil.
- Yalnız tek bir bağımlılık biçimi ailesi denenir; gerçek veride farklı bir bağımlılık yapısı
  farklı sonuç verebilir.
- Replikat sayısı sonlu: FWER için aralık genişliği yaklaşık ±%1,5-2 puan; küçük sapmalar ayırt edilemez.
- Bulunan her motor sorunu **düzeltilmez**; asgari yeniden üretimle raporlanır, düzeltme `DECISIONS.md`
  kaydı ve Efe kararı gerektirir.
