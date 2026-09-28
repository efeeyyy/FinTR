# Proje durumu (devir notu)

Son güncelleme: 2026-09-28. Yeni bir yönetici oturumu uzun sohbet geçmişi yerine bu
dosyayı ve vault notunu (`Beynim/İkinci Beyin/notes/fintr-llm.md`) okuyarak devam eder.
Bu dosya finansal sonuç içermez; sonuçlar ayrı "finans" sohbetinde konuşulur.

## Amaç ve sınırlar

- Borsa İstanbul mikroyapı araştırma aracı: AKD, takas, sağlayıcı metadata'sı, fiyat/hacim;
  deterministik formüller, provenance ve sızıntıya karşı korumalı değerlendirme.
- Uzun vadeli hedef (Efe, 2026-09-26): tam zamanlı sinyal botu. Önce kanıt: ön kayıtlı
  çalışmalar → tarama/uyarı botu (gözlem bildirir, "al" demez) → yalnız kanıt ve Efe'nin
  AGENTS.md değişikliği kararıyla alım sinyali. Sürekli toplama için Telegram Mini App
  (tek oturum, 429 hız sınırı) uygun değil; başka veri kaynağı gerekecek.
- AGENTS.md bağlayıcı: yatırım tavsiyesi yok, kanıtsız manipülasyon/niyet iddiası yok,
  eval verisi geliştirmede kullanılmaz.

## Veri (Git dışında, `data/interim/`)

| Veri | Kapsam |
|---|---|
| Dönem kayıtları `period_analysis/<SYM>/2025-09-26_2026-09-25.json` | 30 BIST 30 hissesi (DSTKF hariç), ~251 işlem günü, paket v2 (kurum dizini dahil), hepsi hash/bütünlük kontrolünden geçti |
| AKFIS kayıtları | 2026-09-08..09-25 (v1) ve 2026-06-29..09-25 (v2); geliştirme materyali (`src/fintr/evaluation/dev_registry.json`) |
| Fiyat geçmişi `price_history/<SYM>/2023-01-02_2026-09-25.json` | 31 hisse, Yahoo chart v8 (gayriresmî); tatil yer tutucu satırları işaretli |
| Çalışma çıktıları `studies/*_train.json` | H1–H4 train sonuçları (okunmadı, finans sohbetinde) |
| Araştırma `research/retail_pct_validation.md` | retail_pct yöntemi tek hissede tutarlı bulundu |
| Tarama çıktıları `scanner/2026-09-25/`, `scanner_lb5/2026-09-25/` | 20 geliştirme hissesi, as_of 2026-09-25 (ve son 5 gün); okunmadı, finans sohbetinde |

## Konfigürasyon (Git'te)

- Evren: `configs/universe/bist30_2026-07-01_2026-09-30.json`, `..._2026-10-01_2026-12-31.json`.
- Etkin ayrım: `configs/splits/current.json` → `bist30_2026_holdout_v2` (seed 20260926;
  holdout: AEFES, AKBNK, ASELS, ASTOR, EKGYO, EREGL, PGSUS, SASA, TAVHL, VAKBN; kalan 20
  geliştirme). v1 geçersiz (DSTKF). Kodla zorunlu: analiz/çalışma/eval holdout'u reddeder,
  toplama izinlidir.
- Dışlama: `configs/exclusions/symbols.json` → DSTKF her yerde reddedilir (Efe kararı;
  toplanmış verisi karşılaştırma referansı olarak saklanır, yalnız Efe'nin açık talimatıyla).
- Kurumsal işlem kaydı: `configs/corporate_actions/` — henüz kayıt yok.

## Ön kayıtlı çalışma

- H1–H4 donduruldu (`docs/prereg/FREEZE.md`, kararlar `docs/prereg/DECISIONS.md`).
  Birincil aile H1+H2 (Holm m=2, vade 5 gün, E1 blok bootstrap, maliyet taraf başına 10 bps);
  H3/H4 ikincil (asgari olay eşiğini karşılamadı).
- Veri kalitesi kuralı: fiyat/AKD ölçek rejimi günleri düşülür (yalnız BIMAS etkilendi).
- Train: 20 geliştirme hissesinde çalıştı. Holdout testi: 2026-10-01..2027-03-31 dönemi
  bittikten sonra 30 hissenin verisi toplanıp tek sefer (`run_study.py --split test`).
- Test kapsamı kararı (Efe, 2026-09-28): birincil = 30 hissenin tamamı (zaman dışı test,
  Holm H1+H2), ikincil (E5) = yalnız 10 holdout; pencere sonu 2027-03-31 zorlanır; tek sefer.
  Plan `configs/studies/prereg/test_plan_v1.json` (hash DECISIONS.md'de); `run_study.py --split
  test` artık `--test-plan/--test-scope` ister; yürütücü `scripts/run_holdout_test.py`
  (dry-run varsayılan, `--execute` + O_EXCL kilit, yalnız pencere içi kayıtlar). Birleşti
  2026-09-28; train sonuçları değişmedi (yapısal karşılaştırma). Holdout fiyat
  dosyasının tamamı yalnız düzeltme/ratio-regime tespitinde kullanılır (Efe, 2026-09-28).

## Aşama 2: tarama/uyarı

- `scanner-v1` birleşti (2026-09-28): `scripts/scan_observations.py`, `src/fintr/scanner/`,
  `docs/scanner.md`. Çevrimdışı; kurallar dondurulmuş H1–H4 koşulları (hash kontrollü), yalnız
  geliştirme hisseleri, gözlem bildirir (getiri/puan/al-sat yok), `validation_status` =
  holdout testi bekleniyor.

## Açık işler

1. (~Nisan 2027) Holdout dönemi verisi: çok hisseli kayıt 2026-10-01..2027-03-31
   (`--allow-large-range`, `--symbol-pause-seconds 30`), fiyat geçmişi güncelleme, test.
2. AKFIS kurumsal işlemi (probable 1→6) için Efe'den KAP linki, çarpan, işlem ve takas
   tarihleri → `scripts/register_corporate_action.py`.
3. Finans sohbeti: AKFIS `analysis.md` eski ("fiyat/hacim yok") — o sohbette güncellenir.
4. Aşama 2 devamı (bulutta, 2026-09-28): `notify-telegram` (bildirim kanalı Telegram botu,
   Efe kararı; dry-run varsayılan, token yalnız ortam değişkeninde) ve `research-data-sources`
   (sürekli veri kaynağı araştırması, yalnız `docs/research/`). Raporlar gelince doğrula/birleştir.
5. KAP erişimi tarayıcıda engelli; KAP belgeleri Efe'den gelir (indirme Java serileştirme
   sarmalıdır, 27 bayt başlık atılınca PDF).
6. Canlı yanıtlarda bilinmeyen olay tipleri görüldü; fiyat/derinlik protokolü keşfedilmedi.

## Çalışma biçimi (özet)

- Yönetici = Opus 5.5, Extra effort; iş alt ajanlarda (`.claude/agents/`, model açık,
  zorluk kriterleri tanımlarda). Testleri/kontrolleri alt ajan yapar, yönetici spot-check eder.
- Geçici: 100 USD bulut kredisi sürdükçe (en geç 2026-11-05) yalnız-kod işleri claude.ai/code
  bulut oturumunda (private `efeeyyy/FinTR`, yeni dal, main'e push yok).
- Efe'ye verilen her komut önce yönetici tarafından çalıştırılır (canlı olanlar `--dry-run`).
- Finansal sonuçlar yönetici sohbetinde gösterilmez.

## Sık komutlar

```powershell
$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe -m unittest discover -s tests
$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe scripts\analyze_period.py --all
$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe scripts\live_collector_smoke.py --help
```
