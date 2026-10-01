# Proje durumu (devir notu)

Son güncelleme: 2026-10-01. Yeni bir yönetici oturumu uzun sohbet geçmişi yerine bu
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
- `notify-telegram` birleşti (2026-09-28): `scripts/notify_observations.py`, `src/fintr/notify/`,
  `docs/notify.md`. Gözlem dosyasını Efe'nin Telegram botuyla düz metin gönderir; varsayılan
  dry-run, `--send` gerekir; token/chat id yalnız `FINTR_TELEGRAM_*` ortam değişkeninde, tüm
  hata yolları redakte (yerel kontrolde IncompleteRead sızıntısı bulunup düzeltildi);
  receipt ile tekrar gönderim engeli (`--resend`).

## Açık işler

1. (~Nisan 2027) Holdout dönemi verisi: çok hisseli kayıt 2026-10-01..2027-03-31
   (`--allow-large-range`, `--symbol-pause-seconds 30`), fiyat geçmişi güncelleme, test.
2. AKFIS kurumsal işlemi (probable 1→6) için Efe'den KAP linki, çarpan, işlem ve takas
   tarihleri → `scripts/register_corporate_action.py`.
3. Finans sohbeti: AKFIS `analysis.md` eski ("fiyat/hacim yok") — o sohbette güncellenir.
4. Aşama 2 devamı: günlük Mini App toplama (ADR `docs/research/daily_miniapp_collection_adr.md`,
   K1–K5 Efe onaylı 2026-09-28; kaynak Mini App, lisans Efe beyanı, doğrulanmadı). PR1
   (planlayıcı, `scripts/collect_daily.py --dry-run`) ve PR3 (`compose_period_capture`)
   birleşti 2026-09-28 (781 test; gerçek 1 yıllık kayıt tek başına birleştirilince seriler
   birebir). PR2 (canlı çalıştırıcı `collect_daily.py`, Europe/Istanbul, 429'da durma,
   çalıştırma kaydı `daily_capture/_runs/`) ve PR4 (`scan_observations.py --daily-root`,
   gözlem şeması v2, bildirim v1+v2) birleşti 2026-09-28 (816 test). 2026-10-01: PR5
   (`collect_daily.py` fiyat adımı + `--skip-price`, Istanbul tarihi; `scripts/compare_daily_d0.py`,
   kesme sabit 2026-09-25, yalnız sayım), bildirim pencere sayımı (`composition.window`,
   `in_window_*`; şema v2 ekleme) ve PR6 (`docs/architecture.md` özeti, runbook 6g günlük sıra)
   birleşti; 846 test. ADR uygulama sırası tamam. İlk canlı günlük çalıştırma (2026-10-01
   akşamı): 20/20 `failed:unknown_event_type` (bilinmeyen tipler yalnız `welcome`, `hb`,
   `request_queued`), fiyat 20/20 kaydedildi. Efe kararı K6 (2026-10-01): bu üç kontrol
   mesajı izinli (config `tolerated_control_event_types`), başka bilinmeyen tip yine engeller;
   çalıştırma kaydı tip adlarını yazar; telafi tabanı temel kaydın sonu 2026-09-25 (ilk pencere
   2026-09-11..E). Birleşti `b0c7d24`, 868 test. İkinci canlı çalıştırma (2026-10-01,
   `_runs/2026-10-01T20-38-30...`): 20/20 saved, 429 yok, engelleyen tip yok. D0
   (`_d0/2026-10-01T20-59-57Z.json`, 11 gün ≤09-25): akd/akd_market/custody 220/220 eşleşti;
   metadata her hissede yalnız 2026-09-25'te farklı (temel kayıtta retail/institutional/
   investor_count o gün henüz null'dı, günlükte dolu; zararsız, yeni kayıt kazanır).
   `scan_observations.py --daily-root` → `data/interim/scanner_daily/2026-09-30/`: pencere
   2026-09-11..09-30, boşluk 0, pencere içi kesinleşmemiş 3 (09-26..09-30 tek gözlem), revize 1
   (yukarıdaki metadata); bildirim dry-run temiz. Sırada: Efe her işlem günü çalıştırır
   (runbook 6g sırası); gözlem içerikleri finans sohbetinde. `tzdata` venv'de yok (UTC+3 yedeği
   çalışıyor). Açık kararlar: günlük dosya saklama/temizlik politikası, Aşama B (ayrı ADR).
   2027-03-31'e kadar H1–H4 için ara performans değerlendirmesi yok (K4).
7. Telegram botu: Efe BotFather'dan açıp token/chat id'yi ortam değişkenine koyacak
   (`docs/notify.md`); ilk gerçek `--send` denemesi Efe'de.
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
