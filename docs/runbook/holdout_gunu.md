# Nisan 2027 holdout günü kontrol listesi

Bu sayfa, tek seferlik ön kayıtlı holdout testinin (`scripts/run_holdout_test.py`)
**gün-of çalıştırma sırasını** verir. Test kuralları, hipotezler ve yürütücünün
ayrıntıları [prereg/README.md](../prereg/README.md) ("Holdout testi çalıştırma"),
[prereg/DECISIONS.md](../prereg/DECISIONS.md) (2026-09-28) ve
[prereg/FREEZE.md](../prereg/FREEZE.md) içindedir; bu liste onların yerine geçmez,
yalnız sırayı ve beklenen çıktıları toplar.

**Sentetik prova.** Bu sıra bugünden, gerçek veri olmadan denenebilir:
`tests/test_holdout_rehearsal.py` gerçek dondurulmuş spec'leri, gerçek test
planını ve gerçek ayrımı, `tests/holdout_fixtures.py`'nin geçici dizine ürettiği
sentetik dönem kayıtları ve fiyat geçmişiyle uçtan uca çalıştırır (dry-run →
`--execute` → kilit reddi → artefact şeması; yaklaşık 4 dk). Prova **hiçbir
sonuç hakkında bilgi vermez**; yalnız aracın doğru çalıştığını gösterir:

```bash
PYTHONPATH=src python -m unittest test_holdout_rehearsal   # tests/ dizininden: -s tests
```

## Değişmez kurallar (hiçbirini çalıştırma günü değiştirme)

- Spec'ler, eşikler (`0.01`, `±0.10`, `5` işlem günü, `10` bps/taraf) ve
  `test_plan_v1.json` değişmez. Bir şey yanlış görünüyorsa **dur ve Efe'ye bildir**;
  düzeltme yeni sürüm (v2) ve yeni ön kayıt gerektirir, bu testin yerine geçmez.
- Test **bir kez** çalışır. `LOCK` veya `manifest.json` varsa tekrar çalışmaz;
  ikisi de silinmez, üzerine yazılmaz.
- Sonuç ne olursa olsun raporlanır. 2027-03-31'e kadar ara performans
  değerlendirmesi yoktur (K4).
- Sonuç dosyaları ve karar metni terminale basılmaz; yalnız JSON dosyalarında okunur.

## Kontrol listesi

### 1. Zaman kapısı

- [ ] Takvim tarihi `2027-03-31` sonrasıdır **ve** her sembolün fiyat geçmişi
  `2027-03-31` sonrası en az 25 tradeable bar içerir (yaklaşık beş hafta; bu
  sayıyı adım 4'teki dry-run denetimi doğrular, elle saymayın).
- Beklenen: pencere bitmeden dry-run denetim 4'te ✗ verir
  (`0 tradeable bar(s) after 2027-03-31 (need >= 25)`). Bu normaldir; çalıştırmayın.

### 2. Ortam ve kod

- [ ] Python 3.12; `PYTHONPATH=src`.
- [ ] Çalışma ağacı temiz: `git status --porcelain` boş. `git rev-parse HEAD` not edilir
  (manifest `code_commit` alanı aynı değeri taşımalıdır).
- [ ] Tam paket yeşil:

  ```bash
  PYTHONPATH=src python -m unittest discover -s tests
  ```

  Beklenen: son satır `OK`; test sayısı bir önceki yeşil sayıdan az değil.
- [ ] Dondurulmuş dosya hash'leri (ham SHA-256) `docs/prereg/FREEZE.md` ve
  `DECISIONS.md` ile aynı:

  ```bash
  sha256sum configs/studies/prereg/*.json
  ```

  Beklenen: `h1…15766a16…`, `h2…21a62029…`, `h3…f0d4a597…`, `h4…05786bb9…`,
  `test_plan_v1.json` = `b5070b39ed17c5c432cb2567fdb1917d52c1a4e4f552c4ae114a747f11d9182a`.
  Biri farklıysa **çalıştırmayın**.

### 3. Veri toplama (30 sembol; DSTKF yok)

- [ ] Dönem kayıtları: `data/interim/period_analysis/<SYM>/<başlangıç>_<bitiş>.json`,
  her sembol için en az bir kayıt, `başlangıç >= 2026-10-01`; pencereyi
  (`2026-10-01..2027-03-31`) kapsar. Pencere öncesi başlayan kayıtlar çalıştırıcı
  tarafından kullanılmaz (varsa yalnız yok sayılır).
- [ ] Fiyat geçmişi: sembol başına **tam geçmiş** (karar 2026-09-28; dosya pencereye
  kırpılmaz), `2027-03-31` sonrası ≥ 25 tradeable bar. Tam geçmiş için
  başlangıç tarihi mevcut dosyayla aynı (`2023-01-02`) verilir:

  ```bash
  PYTHONPATH=src python scripts/fetch_price_history_batch.py \
    --universe configs/universe/bist30_2026-10-01_2026-12-31.json \
    --start-date 2023-01-02 --end-date <pencere sonrası ≥25 bar sağlayan tarih> --save
  ```

  Evren dosyası tam olarak 30 sembolü içerir (split'in 20 + 10'u; DSTKF yok).
- [ ] DSTKF için hiçbir kayıt toplanmaz ve kullanılmaz (`configs/exclusions/symbols.json`).

### 4. Dry-run (hiçbir şey yazmaz)

```bash
PYTHONPATH=src python scripts/run_holdout_test.py \
  --test-plan configs/studies/prereg/test_plan_v1.json
```

- [ ] Beklenen çıktı, çıkış kodu `0`, tam altı satır:

  ```
  [OK] 1. plan_and_spec_hashes: ok
  [OK] 2. git_working_tree_clean: clean, commit <HEAD>
  [OK] 3. period_records_after_window_start: ok
  [OK] 4. min_tradeable_bars_after_window_end: ok
  [OK] 5. akd_dates_cover_tradeable_window_dates: ok
  [OK] 6. no_lock_or_previous_output: ok
  ```

- [ ] Herhangi bir `[FAIL]` varsa `--execute` çalıştırılmaz. Satır, sembolü adıyla
  ve nedeniyle yazar:

  | Denetim | Tipik neden | Eylem |
  |---|---|---|
  | 1 | spec/plan hash uyuşmuyor | dur; dosya değiştirilmiş, Efe'ye bildir |
  | 2 | çalışma ağacı kirli | değişikliği commit'le ya da geri al, yeniden dry-run |
  | 3 | sembolde `start >= 2026-10-01` kayıt yok / birleştirilemedi | eksik sembolü topla |
  | 4 | `<N> tradeable bar(s) after 2027-03-31 (need >= 25)` | fiyatı güncelle / bekle |
  | 5 | `missing AKD server date(s): [...]` | eksik günleri topla |
  | 6 | `LOCK`/`manifest.json` var | **dur**; test zaten çalışmış ya da yarım kalmış, Efe kararı |

- [ ] Dry-run kendiliğinden `data/interim/studies/test/` oluşturmamıştır.

### 5. Execute (geri alınamaz)

Efe'nin açık onayı alındıktan sonra:

```bash
PYTHONPATH=src python scripts/run_holdout_test.py \
  --test-plan configs/studies/prereg/test_plan_v1.json --execute
```

- [ ] Süre: 30 sembolle 4–5 dakika (iid bootstrap ve sinyal günü başına AKD proxy
  hesabı ağırdır). Süreç kesilirse **yeniden başlatmayın** (adım 7).
- [ ] Beklenen stdout (altı `[OK]` satırından sonra), yalnız yollar ve durumlar:

  ```
  run_study <spec_id> scope=primary: ok
    saved: data/interim/studies/<spec_id>_test_primary.json
  run_study <spec_id> scope=secondary: ok
    saved: data/interim/studies/<spec_id>_test_secondary.json
  ... (H1, H2, H3, H4 için; toplam 8 çalıştırma)
  evaluate_study_family (primary H1+H2): ok
    saved: data/interim/studies/test/family_decision_primary.json
  Manifest written: data/interim/studies/test/manifest.json
  ```

  Çıkış kodu `0`. Stdout'ta getiri, p değeri ya da karar metni **yoktur**.

### 6. Çıktı doğrulaması (sonuç yorumlanmadan önce)

- [ ] `data/interim/studies/test/` içinde `LOCK`, `manifest.json`,
  `family_decision_primary.json`; `data/interim/studies/` içinde 8 sonuç dosyası
  (`*_test_primary.json` ×4, `*_test_secondary.json` ×4).
- [ ] `manifest.json`: `schema = fintr_holdout_test_manifest_v1`, `completed: true`,
  `error` alanı yok, `code_commit` = adım 2'deki HEAD, `test_plan_sha256` =
  `b5070b39…9182a`, `specs` altında dört spec için ham/kanonik hash ve seed
  (`20261001`), `split_file_sha256`, `input_file_sha256` (30 fiyat + 30 dönem kaydı,
  DSTKF yok), `results` altında 8 yol + SHA-256.
- [ ] Her sonuç dosyasında `spec_sha256` `FREEZE.md`'deki kanonik hash'e, `test_plan_sha256`
  plana, `reproducibility.bootstrap_seed` `20261001`'e eşit; `test_window_end =
  2027-03-31`; `split_guard.guard_bypassed = false`. `primary`: `symbol_scope =
  declared`, 30 sembol; `secondary`: `symbol_scope = holdout_only`, 10 holdout sembolü.
- [ ] Manifest'teki SHA-256'lar dosyalarla uyuşur:

  ```bash
  sha256sum data/interim/studies/*_test_*.json data/interim/studies/test/family_decision_primary.json
  ```

- [ ] `family_decision_primary.json`: `confirmatory: true`, `test_scope: primary`,
  `m: 2`, `primary` yalnız H1+H2, H3/H4 `secondary_descriptive`; `input_result_sha256`
  yalnız iki `*_test_primary.json` dosyasını içerir.
- [ ] `ratio_regime_quality` bloğu her sembol için okunur: düşürülen sinyal günü
  sayıları raporlanır (DECISIONS.md "veri kalitesi kuralı").

### 7. Hata ve kesinti durumları

- Execute yarıda kesilirse (`LOCK` var, `manifest.json` yok ya da `completed: false`):
  **hiçbir şey silinmez, yeniden çalıştırılmaz**. `Execute failed: ...` mesajı ve
  kısmi manifest (`error` alanı) Efe'ye iletilir; yarım kalmış çalıştırmanın nasıl
  ele alınacağı Efe'nin kararıdır.
- Execute ikinci kez çalıştırılırsa `[FAIL] 6. no_lock_or_previous_output` ve
  `Execute refused: not every dry-run check passed` ile reddedilir; çıktı
  dosyaları değişmez. Bu beklenen davranıştır.

### 8. Sonrası

- [ ] Sonuç, yönü ne olursa olsun, `family_decision_primary.json` ve sonuç dosyalarından
  aynen raporlanır (Holm kararı yalnız birincil 30 sembol, H1+H2; E5 yalnız
  tanımlayıcı).
- [ ] Yön etiketi ve `Opus:` değerlendirme kelimesi (DECISIONS.md 2026-10-02/03):
  sonuç beklenen yönü doğrulamazsa kaldırılır ve analiz notu adımı kapatılır;
  bu karar Efe'nindir.
- [ ] `docs/STATUS.md` güncellenir (commit, manifest hash'i, sonuç sınıfı).
