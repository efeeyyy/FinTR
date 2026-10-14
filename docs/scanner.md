# Gözlem tarayıcısı (`fintr.scanner`)

Aşama 2'nin ilk adımı: kaydedilmiş dönem/fiyat verisini çevrimdışı tarayan bir
gözlem tarayıcısı. Seçilen bir gün (`as_of`) için geliştirme hisselerinde,
ön kayıtlı H1–H4 koşullarından hangisinin sağlandığını kaynaklarıyla raporlar.

## Ne yapmaz

- **Getiri, hedef, puan, sıralama veya "al/sat" üretmez.** Çıktı şemasında
  `score`/`rank`/`return`/`target` alanı yoktur (`tests/test_scanner.py`
  `ForbiddenFieldsTests` bunu doğrular). Tarayıcı çıktısı ve şeması 2026-10-02
  kararından sonra da aynıdır; yalnız Telegram bildirimi (`fintr.notify`) Efe'nin
  kişisel kullanımı için, spec'teki `predicted_direction`'ı doğrulanmamış hipotez
  uyarısıyla "beklenen yön: yukarı (al yönlü)" / "aşağı (sat yönlü)" diye gösterebilir
  (bkz. `docs/notify.md`, `docs/prereg/DECISIONS.md`).
- **Ağ, Telegram ya da model çağrısı yok.** Yalnız `data/interim/period_analysis/`
  ve `data/interim/price_history/` altındaki kaydedilmiş dosyaları okur.
  `data/interim/studies/` klasörünü hiç okumaz.
- Yeni eşik veya parametre eklemez: her kuralın koşulu, dondurulmuş spec
  dosyasının (`configs/studies/prereg/h1..h4_*.json`) `condition` alanından
  gelir, aynen.
- Formül kopyalamaz: `fintr.research.conditions.evaluate_condition`,
  `fintr.research.features.truncate_period_capture`,
  `fintr.research.engine.load_symbol_data`/`detect_custody_scale_jumps`/
  `drop_ratio_regime_signal_dates` ve
  `fintr.evaluation.symbol_guard.load_active_guards` doğrudan kullanılır.

Her gözlemde sabit Türkçe uyarı ve durum bulunur:

> "Gözlem bildirimidir; yatırım tavsiyesi, al/sat sinyali, niyet veya
> manipülasyon iddiası değildir. Kurallar ön kayıtlı hipotezdir, holdout
> testi bekleniyor."

`validation_status: "preregistered_hypothesis_holdout_test_pending"`.

## Kural dosyası (`fintr_scanner_rules_v1`, `configs/scanner/rules_v1.json`)

Her kural: `rule_id`, dondurulmuş spec dosyasının yolu (`spec_path`),
`docs/prereg/FREEZE.md`'deki ham dosya SHA-256'sı (`raw_sha256`) ve kanonik
spec SHA-256'sı (`canonical_sha256`, `load_study_spec` tarafından hesaplanan).
`fintr.scanner.rules.load_scanner_rules` şunlardan biri sağlanmazsa açık
hata verip çalışmaz:

- spec dosyası diskte `raw_sha256` ile eşleşmeli (dosya FREEZE.md'den beri
  değişmemiş olmalı),
- `load_study_spec` ile yüklenen spec'in `frozen` alanı `true` olmalı,
- spec'in kanonik `spec_sha256`'sı `canonical_sha256` ile eşleşmeli,
- spec bir `preregistration` bloğu (dolayısıyla `family_role`) taşımalı.

Bugün dört kural yüklenir: H1, H2 (`family_role: primary`) ve H3, H4
(`family_role: secondary_descriptive`).

## Lookahead koruması

`as_of` günündeki değerlendirme yalnız `truncate_period_capture(capture,
as_of)` sonucunu ve `<= as_of` kapanışlarını görür (placeholder/tatil bar
hiçbir zaman kapanış sağlamaz -- `fintr.research.engine.tradeable_price_bars`
zaten hariç tutar). `tests/test_scanner.py::LookaheadTests` `as_of` sonrası
verinin değişmesinin `as_of` sonucunu etkilemediğini doğrudan test eder.

## Veri kalitesi bastırmaları

`run_study` ile aynı kurallar, gözlem seviyesinde:

- **`suppressed: "ratio_regime"`**: `as_of`, sembolün `join_price_history`
  ile tespit edilen bir `ratio_regime`'i içindeyse (`drop_ratio_regime_signal_dates`
  ile aynı mantıkla tespit edilir), o sembol/gün için hiçbir kuralda gözlem
  üretilmez.
- **`suppressed: "custody_scale_jump"`**: koşulun okuduğu takas verisi
  penceresi (`min_date`..`max_date`) bir `detect_custody_scale_jumps` bulgusu
  içeriyorsa, o kural için gözlem üretilmez.

Her iki durumda da `condition_met: null`, `features: []`.

## Tazelik raporu

Her sembol × `as_of` için `freshness`: son AKD sunucu tarihi, son takas
tarihi, son fiyat tarihi. Eksik veri `null` olarak raporlanır, asla sıfır
sayılmaz. Takas AKD'nin, `as_of` tarihinde geçerli takas gecikmesi kadar (şu an
2, T+2 yerleşimi) sunucu günü veya daha fazla gerisindeyse
`custody_flagged_stale_t_plus_2: true` (anahtar adı sabit kalır). Gecikme koddan
değil, tarih-etkin `configs/market/settlement_cycle.json` dosyasından gelir
(`fintr.data.settlement_cycle`; hatalı/sıralı olmayan dosya ya da ilk dönemden
önceki `as_of` çalışmayı durdurur). Borsa İstanbul T+1 değerlendiriyor (duyuru
2025-05-05); 2026-10-14 itibarıyla yürürlük tarihi bilinmiyor. Şema değişmedi.

## Hisse kapsamı

Varsayılan: etkin ayrımın (`configs/splits/current.json`) geliştirme
hisseleri, dışlananlar (`configs/exclusions/symbols.json`) hariç. Holdout
hissesi istenirse ret (holdout testine kadar analiz yasak); dışlanan hisse
(DSTKF) her zaman ret. **`--no-split-guard` seçeneği yoktur.** Çıktıda
`split_guard` bloğu bulunur (`run_study`'deki gibi split/dışlama dosyası ve
SHA-256'ları).

## Çıktı şeması (`fintr_scanner_observations_v1`/`_v2`)

```json
{
  "schema": "fintr_scanner_observations_v1",
  "generated_at_utc": "...",
  "code_commit": "...",
  "python_version": "...",
  "rules_file": "configs/scanner/rules_v1.json",
  "rules_file_sha256": "...",
  "split_guard": { "...": "run_study'deki gibi" },
  "warning": "Gözlem bildirimidir; ...",
  "symbols": {
    "AKFIS": {
      "as_of_source": "default_common_last_day" | "explicit",
      "input_files": {
        "period_capture": {"path": "...", "sha256": "..."},
        "price_history": {"path": "...", "sha256": "..."}
      },
      "scans": [
        {
          "as_of": "2026-09-25",
          "freshness": {
            "last_akd_server_date": "2026-09-25",
            "last_custody_date": "2026-09-23",
            "last_price_date": "2026-09-25",
            "custody_behind_akd_server_dates": 2,
            "custody_flagged_stale_t_plus_2": true
          },
          "observations": [
            {
              "rule_id": "H1",
              "family_role": "primary",
              "as_of": "2026-09-25",
              "suppressed": null,
              "condition_met": true,
              "not_evaluable_reason": null,
              "features": [
                {
                  "name": "vwap_proxy_vs_close",
                  "params": {},
                  "formula": "(daily_market_proxy.vwap_proxy[t] - price_bar.close[t]) / price_bar.close[t]; ...",
                  "value": "-0.0123",
                  "data_families": ["akd", "price"],
                  "min_date": "2026-09-25",
                  "max_date": "2026-09-25",
                  "institution_labels": {}
                }
              ],
              "validation_status": "preregistered_hypothesis_holdout_test_pending"
            }
          ]
        }
      ]
    }
  }
}
```

`condition_met` üç değerli: `true`/`false`/`null` (veri eksikse `null`,
`not_evaluable_reason: "missing_data"`). `features[].institution_labels`
yalnız feature'ın parametresinde açık kurum kodu (`codes`, örn.
`flow_run_length`/`custody_change`) varsa doldurulur; `resolve_brokerage_label`
ile yalnız kesin kod eşleşmesi kullanılır (eşleşmezse `unresolved`,
çakışırsa `ambiguous`, v1 capture'da dizin yoksa `directory_not_in_capture`).
Bugünkü dondurulmuş H1–H4 hiçbiri `codes` parametresi kullanmaz (H3/H4'ün
`top1_akd_net_share_of_volume`'u hangi kurumun öne çıktığını da açıklamaz --
bu kasıtlıdır, bkz. H3 spec açıklaması).

## Günlük birleştirme (`--daily-root`)

Varsayılan kapalı (ADR "Uygulama sırası" madde 4,
`docs/research/daily_miniapp_collection_adr.md` §4). Verilmezse çıktı
mevcut davranışla bayt düzeyinde aynıdır (üretim zamanı gibi değişken
alanlar hariç); bu, `tests/test_scanner.py::ScriptEndToEndTests` ve
`DailyRootEndToEndTests::test_default_run_without_daily_root_unaffected_by_daily_root_data`
ile doğrulanır.

`--daily-root <dizin>` verilirse, her sembolün `--period-dir` altındaki
temel (en uzun/en son biten) kaydı, `<dizin>/<SYM>/` altındaki **tüm**
`<S>_<E>.json` günlük (kayan pencere) kayıtlarıyla
`fintr.research.compose_period_capture.compose_period_capture` ile
birleştirilir (`data/interim/daily_capture/<SYM>/` şeması, ADR §2). Hiçbir
formül burada yeniden hesaplanmaz; sadece hangi kaydın hangi tarih×aile
için kazandığı (en yeni `captured_at_utc`), boşluk, kesinleşme ve
revizyon `compose_period_capture`'ın kendi kurallarına (ADR §3) göre
belirlenir. Fiyat dosyası, tüm girdi kayıtlarının kapsadığı
`min(start)..max(end)` aralığını örten dosyadır (`discover_price_history`,
değişmedi); birleştirmenin `gaps` hesaplaması için gereken takvim bu fiyat
dosyasının işlem günlerinden gelir. `<dizin>/<SYM>/` boşsa ya da yoksa,
sembol yalnız temel kayıttan (tek girdi) birleştirilir -- birleştirici tek
girdiyle de çalışır, ama bu durumda `_akd_settled`/`_custody_settled`'ın
"en az iki kayıt" koşulu hiçbir tarih için sağlanamayacağından AKD ailesi
için tüm günler `unsettled` görünür (custody ailesi, temel kaydın kendi
penceresi yeterince genişse yine kesinleşebilir; bkz.
`compose_period_capture` modül docstring'i kural 4).

Şema adı yalnız bu durumda `fintr_scanner_observations_v2`'ye değişir
(`--daily-root` verilmediğinde hep `_v1`). Sebep: eklenen alanlar (aşağıda)
yalnızca opsiyonel değil, birleştirilmiş veri kullanılmadığı sürece hiç
üretilmez; şema adını sabit tutmak yerine ayırmak, eski bir tüketicinin
(örn. arşivlenmiş bir `observations.json` okuyan bir betik) `composition`/
`data_status` alanlarının varlığını şemadan anlayabilmesini sağlar. Eski
`_v1` dosyaları (bu değişiklikten önce üretilmiş ya da `--daily-root`
verilmeden üretilmiş her dosya) aynen okunmaya devam eder; `fintr.notify`
her iki şemayı da kabul eder (bkz. `docs/notify.md`).

Sembol başına eklenen `composition` bloğu:

```json
"composition": {
  "composition_sha256": "...",
  "composer_version": "compose_period_capture_v1",
  "inputs": [
    {"kind": "base", "path": "data/interim/period_analysis/AKFIS/....json", "sha256": "..."},
    {"kind": "rolling", "path": "data/interim/daily_capture/AKFIS/....json", "sha256": "..."}
  ],
  "window": {"start": "2026-09-08", "end": "2026-09-25"},
  "gaps": {"count": 1, "dates": ["2026-09-24"], "in_window_count": 1},
  "unsettled": {"day_count": 2, "entries": [{"date": "2026-09-25", "family": "custody"}], "in_window_day_count": 1},
  "revisions": {"day_count": 1, "entries": [{"date": "2026-09-23", "family": "custody"}], "in_window_day_count": 1}
}
```

**Pencere-sınırlı sayımlar.** `gaps.count`/`unsettled.day_count`/
`revisions.day_count` ve `entries` listeleri birleştiricinin tüm dönem
toplamıdır (denetim için tam liste; birleştirici kuralı değişmedi,
`composition_sha256` aynı). Kural 4 gereği yalnız tek girdide görülen AKD
günü `unsettled` sayıldığından, yalnız temel yıllık kayıtta bulunan yüzlerce
eski gün de toplama girer; bu yüzden bildirim toplamı kullanmaz. `window` =
`[günlük (rolling) girdilerin kapsadığı en erken sunucu tarihi, as_of]`, iki
uç dahil (`as_of` = o sembolün tarama referans tarihi, yani son `as_of`).
`in_window_*` alanları yalnız bu aralıktaki tekil günleri sayar; `as_of`
sonrası tarihler hiçbir pencere sayımına girmez. Rolling girdi yoksa (ya da
en erken rolling tarihi `as_of`'tan sonraysa) `window` ve üç `in_window_*`
alanı `null`'dır. Şema adı `fintr_scanner_observations_v2` **kalır**: alanlar
yalnız ekleniyor, mevcut alanların anlamı değişmiyor; eski v2 okuyucuları
etkilenmez ve `fintr.notify` alanların yokluğunu (eski v2 dosyası) toplama
düşerek işler (bkz. `docs/notify.md`).

`inputs` her girdi dosyasının kendi SHA-256'sını taşır (temel + her günlük
kayıt, `load_period_capture`'ın zaten doğruladığı `packet_sha256` değil,
diskteki dosyanın ham baytlarının hash'i -- `input_files.period_capture`
alanıyla aynı yöntem). `gaps`/`unsettled`/`revisions` yalnız sayı ve tarih
(+ aile) taşır, hiçbir değer yok (AGENTS.md: hesaplanan değerleri kaynaktan
ayır kuralı burada "hiç değer gösterme" ile karşılanır). `day_count`,
birden çok aile aynı günü etkileyebileceği için (ör. hem `akd` hem
`custody`) tekil tarih sayısıdır; `entries` tarih×aile ayrıntısını taşır.

Her `scans[]` girdisine, yalnız birleştirme kullanıldığında, o `as_of`
gününe özel bir `data_status` eklenir:

```json
"data_status": {
  "unsettled_families": ["custody"],
  "revised_families": []
}
```

ADR §4 varsayılanı: as_of günü kesinleşmemiş ya da revize edilmişse
gözlem yalnız işaretlenir, **hiçbir gözlem bastırılmaz veya `condition_met`
değiştirilmez** -- `data_status` mevcut `observations` listesinin yanına
eklenir, onun yerine geçmez (`tests/test_scanner.py::DailyRootEndToEndTests
::test_data_status_marks_revised_and_unsettled_as_of_days_not_suppressed`).

### Lookahead koruması, birleştirilmiş veride de geçerli

`evaluate_rule_for_symbol`, birleştirilmiş `PeriodCapture` için de yalnız
`truncate_period_capture(composed, as_of)` sonucunu görür -- birleştirme
mantığı değişmez, tek fark girdinin `compose_period_capture`'dan gelmesidir.
`composition` alanı (gaps/unsettled/revisions), tanımı gereği as_of'tan
**sonraki** kayıtları da kullanabilir (ör. bir günün kesinleşmesi, o günden
sonraki bir kaydın varlığına bakar); bu kasıtlıdır ve yalnız
`data_status`/`composition` raporlamasını etkiler, `condition_met`'i asla
etkilemez -- `tests/test_scanner.py::LookaheadTests
::test_composed_capture_future_data_change_never_affects_as_of_result`
bunu doğrudan test eder.

## Komut

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\scan_observations.py --as-of 2026-09-25 --format json --save
.\.venv\Scripts\python.exe scripts\scan_observations.py --symbols AKFIS,GARAN --lookback-days 3
.\.venv\Scripts\python.exe scripts\scan_observations.py --as-of 2026-09-25 `
  --daily-root data\interim\daily_capture --format json --save
```

- `--as-of YYYY-MM-DD`: varsayılan, her hissenin kendi AKD ve fiyat verisinin
  ortak son günü (`as_of_source: "default_common_last_day"`); hisseye göre
  farklıysa çıktıda hisse başına raporlanır.
- `--lookback-days N` (varsayılan 1): son N sunucu gününü ayrı ayrı `as_of`
  olarak tarar.
- `--symbols SYM1,SYM2`: varsayılan, etkin ayrımın geliştirme hisseleri.
- `--period-dir` / `--price-dir`: varsayılanlar `data/interim/period_analysis`,
  `data/interim/price_history`. Dosya eşleştirme `analyze_period.py --all`
  mantığıyla aynıdır (`<SYM>/<start>_<end>.json`; fiyat için en dar kapsayan
  dosya seçilir).
- `--daily-root DİZİN` (varsayılan kapalı): verilirse her sembol
  temel kayıt + `DİZİN/<SYM>/` altındaki günlük kayıtlarla birleştirilir;
  bkz. "Günlük birleştirme" bölümü yukarıda.
- `--format text|json`.
- `--save`: `data/interim/scanner/<as_of>/observations.json` dosyasına yazar;
  var olan dosyanın üzerine asla yazmaz.

## Haftalık replay (`scripts/replay_scans.py`, E3)

Dondurulmuş tarayıcıyı saklı veriyle son N `as_of` için yeniden çalıştırır ve
`data/interim/scanner_daily/<as_of>/observations.json` ile alan alan karşılaştırır
(`docs/research/signal_bot_best_practices_2026-10-13.md` E3). Fark = kod kayması,
kural değişikliği ya da veri revizyonu; **tespit edilir ve sınıflanır, sessizce
geçilmez**. Ağ, Telegram, model çağrısı yok; `data/interim/studies/` okunmaz;
günlük boru hattına eklenmez (haftalık, ör. Cumartesi; bkz. runbook 6k).

```powershell
.\.venv\Scripts\python.exe scripts\replay_scans.py                    # son 5, iki mod
.\.venv\Scripts\python.exe scripts\replay_scans.py --as-of 2026-10-09,2026-10-13 --mode point-in-time
```

- `--last N` (varsayılan 5) ya da `--as-of D1,D2`; `--mode point-in-time|current|both`
  (varsayılan `both`); `--scanner-dir/--period-dir/--daily-root/--price-dir`
  (varsayılanlar günlük boru hattıyla aynı); `--out` (yoksa
  `data/interim/scanner_replay/<UTC>/replay_report.json`; var olan dosyaya asla yazmaz,
  işe başlamadan reddeder).
- Çıkış: 0 hepsi aynı, 2 fark, 1 hata (hata önceliklidir: eksik replay "aynı" sayılmaz).
  Son satır `REPLAY: as_of=K identical=I diff=D errors=E`; I/D/E (as_of, mod)
  çiftlerini sayar (`both` ile 2K'ya kadar).
- Her as_of için kaydedilen belgenin `symbols` anahtarlarına, kendi son `as_of` ve tarama
  sayısına göre gruplanıp tarayıcı yeniden çalıştırılır (`--as-of` her zaman açık verilir;
  `as_of_source` karşılaştırılmaz). Şema v2 ise `--daily-root` verilir.

### Neden iki mod: günlük kayıtlar sonradan revize edilir

T+2 takas revizyonu ve gecikmeli metadata, günlük kayıtlara yeni dosya (yeni pencere)
olarak eklenir; eski dosya değişmez. Bugünkü tüm veriyle yapılan saf yeniden çalıştırma
bu yüzden meşru olarak farklı çıkabilir. İki mod ayrımı sağlar:

1. **`point-in-time`**: `scan_observations.py --data-cutoff-utc <özgün generated_at_utc>`
   (yeni, varsayılan kapalı seçenek; verilmezse davranış ve çıktı değişmez). Yalnız
   `capture_metadata.captured_at_utc` (temel/günlük dönem kayıtları) ya da
   `capture_metadata.accessed_at_utc` (fiyat geçmişi) bu andan **önce veya ona eşit**
   olan dosyalar kullanılır; sonradan gelen revizyon dosyaları dışarıda kalır. Çıktıya
   üst düzey `data_cutoff_utc` yazılır (yalnız bu seçenekle). Değişmemiş kod+kural için
   fark 0 olmalıdır; fark = kod/kural kayması **veya** özgün girdi dosyasının sonradan
   değiştirilmesi/silinmesi (`input_set_mismatch`). Geç taranan günler doğru işlenir: 10-12
   taraması 10-13 verisi oluştuktan sonra yapıldıysa, kesme anı o taramanın
   `generated_at_utc`'sidir ve 10-13 dosyaları dahildir (özgün tarama da kullandı).
2. **`current`**: diskteki tüm veri. Fark = veri revizyonu + kod kayması.

### Sınıflama (sembol başına, yalnız kayıtlı provenance ile)

`input_files` ve `composition.inputs` (yol kuyruğu + sha256) aynıysa ama çıktı farklıysa:
`code_drift` (`rules_file_sha256` de farklıysa `rule_change`). Girdi kümesi farklıysa:
`current` modunda `data_revision`, `point-in-time` modunda `input_set_mismatch`; bu
durumda o sembolde kod kayması veri değişiminden ayrılamaz (raporda belirtilir).
`rules_file_sha256` eşitliği ve özgün/yeni `code_commit` her birimde raporlanır.

Karşılaştırma tam eşitliktir (Decimal metinleri dahil); yalnız `generated_at_utc`,
`python_version`, `code_commit` (raporlanır, sayılmaz), `data_cutoff_utc` ve `as_of_source`
dışarıda tutulur. Dosya yolları kuyruğa indirgenir (dizin yazımı farkı fark sayılmaz).
`composition_sha256`, birleştiricide girdi **yol yazımlarını** da hash'lediği için yalnız
yazımlar birebir aynıysa (aynı göreli dizinlerle çalıştırma) karşılaştırılır; aksi halde
raporda `composition_sha256_not_comparable` altında listelenir (girdi sha256'ları ve diğer
tüm alanlar yine karşılaştırılır). Bu yüzden betik repo kökünden, varsayılan göreli
dizinlerle çalıştırılmalıdır.

Rapor: birim başına durum, `diff_count`, `(symbol, rule, field)` sayıları
(`summary.by_field/by_rule/by_symbol/by_class`), ilk `--max-diffs` (500) alan farkı
(özgün/yeniden değer), kesilen sayı. Esik/kural/spec eklemez, getiri veya karar üretmez.

Sınırlar: eşik parametreleri (`--adjustment-ratio-epsilon` vb.) belgede kayıtlı değildir;
replay varsayılanlarla çalışır, onları değiştiren bir üretim koşusu fark olarak görünür.
`price_revisions/` ve `repair_incomplete_prices.py` ile değiştirilen fiyat dosyaları, aynı
dosya adı korunuyorsa yalnız `sha256` farkıyla (veri revizyonu) görünür.

## Sınırlar

- Bu bir gözlem bildirimidir, tahmin değildir; H1/H2 birincil, H3/H4
  ikincil/tanımlayıcıdır ve hepsi holdout testi (2026-10-01 sonrası)
  bekliyor.
- `custody_change`/`flow_run_length`'in okuduğu kodlar bir kurum
  sınıflandırmasını (örn. "yabancı takas") doğrulamaz; yalnız spec'in verdiği
  kod kümesini toplar.
- `top1_akd_net_share_of_volume`'un hangi kurumu öne çıkardığı raporlanmaz;
  bu bilinçli bir sınırdır (kurum kimliği/niyeti hakkında çıkarım yapmama).
