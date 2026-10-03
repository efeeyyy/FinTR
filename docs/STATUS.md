# Proje durumu (devir notu)

Son güncelleme: 2026-10-03 (akşam). Yeni bir yönetici oturumu uzun sohbet geçmişi yerine bu
dosyayı ve vault notunu (`Beynim/İkinci Beyin/notes/fintr-llm.md`) okuyarak devam eder.
Bu dosya finansal sonuç içermez; sonuçlar ayrı "finans" sohbetinde konuşulur.

## Amaç ve sınırlar

- Borsa İstanbul mikroyapı araştırma aracı: AKD, takas, sağlayıcı metadata'sı, fiyat/hacim;
  deterministik formüller, provenance ve sızıntıya karşı korumalı değerlendirme.
- Uzun vadeli hedef (Efe, 2026-09-26): tam zamanlı sinyal botu. Önce kanıt: ön kayıtlı
  çalışmalar → tarama/uyarı botu (gözlem bildirir, "al" demez) → yalnız kanıt ve Efe'nin
  AGENTS.md değişikliği kararıyla alım sinyali. 2026-10-02 Efe kararı: kanıttan önce,
  kişisel kullanım için bildirimde dondurulmuş kuralın hipotez yönü ("al/sat yönlü")
  etiketi gösterilir (doğrulanmamış; kurallar değişmez). Mini App günlük bir kez toplama
  için uygun, sürekli/gün içi toplama için değil (tek oturum, 429).
- AGENTS.md bağlayıcı: yatırım tavsiyesi yok (yalnız yukarıdaki bildirim etiketi istisnası;
  LLM bileşenleri hariç), kanıtsız manipülasyon/niyet iddiası yok, eval verisi geliştirmede
  kullanılmaz. Claude kişisel yatırım tavsiyesi vermez.

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
   (yukarıdaki metadata); bildirim dry-run temiz. 2026-10-02: tek komut
   `scripts/run_daily_pipeline.py` (collect → scan → notify; hata olursa durur, `--send` olmadan
   göndermez; aynı gün tekrar çalıştırma yeniden tarama/gönderim yapmaz; runbook 6h) ve Efe
   kararı ile bildirimde dondurulmuş kuralın `predicted_direction` etiketi ("beklenen yön:
   yukarı (al yönlü)/aşağı (sat yönlü)", doğrulanmamış hipotez uyarısı, `--no-direction-label`;
   AGENTS.md istisnası, DECISIONS.md 2026-10-02) birleşti; 895 test. Sırada: Efe her işlem günü
   gece yarısından sonra/sabah tek komutu çalıştırır; gözlem içerikleri finans sohbetinde. `tzdata` venv'de yok (UTC+3 yedeği
   çalışıyor). Saklama kararı (Efe, 2026-10-03): günlük dosyalar (daily_capture, _runs, scanner_daily,
   llm_notes, notify) hiç silinmez; ölçüm ~5 MB/gün (~1,2 GB/yıl), holdout testinden sonra
   (Nisan 2027) yeniden bakılır. Açık karar: Aşama B (ayrı ADR).
   2027-03-31'e kadar H1–H4 için ara performans değerlendirmesi yok (K4).
8. Günlük analiz notu (Efe kararı 2026-10-02): `scripts/llm_note.py` + `src/fintr/llm_note/`,
   `run_daily_pipeline.py --llm-note` (varsayılan kapalı; sıra collect → scan → deterministik
   notify → llm_note → notun ayrı mesajı, ayrı `note_receipt.json`) ve `notify_observations.py
   --llm-note-dir`. Opus 5.5, Efe'nin aboneliğiyle Claude Code CLI başsız mod (CLI'da giriş
   yapıldı 2026-10-03). Her `condition_met` gözlem için hisse başına bir çağrı: kuralın yönünü
   veri `destekliyor`/`zayıflatıyor`/`belirsiz` + `plain_explanation` (sade Türkçe); fail-closed
   doğrulayıcı (şema, kanıt yolu, yasak ifade, işaretli/yuvarlamalı rakam eşleşmesi). Ham veri
   Anthropic'e gider (Efe kararı, lisans kapsamı doğrulanmadı). Yalıtım bayrakları
   (`--tools "" --strict-mcp-config --disable-slash-commands --setting-sources local
   --exclude-dynamic-system-prompt-sections`, boş temp cwd, CLAUDE*/ANTHROPIC* env silinir)
   elle tuzak soruyla doğrulandı (bayraksız: CLAUDE.md görülüyor; bayraklı: görülmüyor).
   **DEVİR DURUMU (2026-10-03 akşam):**
   - Main'de (push edildi, `d3e6d83`): analiz notu, tuzak yöntemli yalıtım sınaması
     (`probe:clean`) ve prompt v2 (`configs/llm_note/prompt_v2.md`; gerekçe 1..6, yazıyla
     sayma yok, gözlemin kendi `as_of` tarihi serbest; doğrulayıcıda tek gevşetme bu tarih);
     1032 test OK.
   - İlk gerçek not (as_of 2026-10-02): `ok=2,rejected=3`; redler `number_word` ("iki"),
     `number_not_in_input` (kendi as_of tarihi), `schema` (reasons 1..6) → prompt v2 ile ele
     alındı. Kalan 3 hisse (KRDMD, PETKM, TRALT) için yeniden çalıştırma Efe'de
     (`llm_note.py --as-of 2026-10-02`; yönetici çalıştıramıyor, izin sınıflandırıcısı
     ham veri gönderimini engelliyor). Sonucu yalnız kategori düzeyinde oku.
   - Bildirim düzeni v2: bulut dalı `notify-layout-v2` sonnet-high ile doğrulandı ve main'le
     birleştirildi → `origin/notify-layout-v2-merge` (`a3e66df`, 1097 test OK). Düzeltilenler:
     etiketli her hisse mesajında uyarı satırı (AGENTS.md), kural satırlarında %10 bant,
     Ctrl-C sonrası receipt, v1/not hata yollarında chat id redaksiyonu. Efe kararları
     (2026-10-03): yüzde = |v|*100, özne VWAP ("Ortalama fiyat (VWAP proxy) kapanışın %X
     altında/üstünde", eşik tam %1); etiket AGENTS.md'deki gibi küçük harf. Bulutta
     `notify-layout-v2-pct` ile uygulandı; doğrulanıp main'e birleşti (`ebb3b51`). Bulut
     `notify-note-resume` (not mesajı devam edebilir, `note_receipt` v2) da birleşti
     (`95fdc13`, 1122 test OK). Yönetici artık doğrulanmış birleştirmeleri kendisi push eder
     (Efe izni 2026-10-03).
   - Kalan risk (belgeli): Telegram zaman aşımında tek mesaj devamda bir kez daha gidebilir;
     not yolu (`--llm-note-dir`) da devam edebilir `note_receipt` (v2) kullanır; kalan risk aynı
     (zaman aşımında tek not mesajı bir kez daha gidebilir). Bkz. `docs/notify.md`.
   - Sonra Efe'ye günlük komut: `run_daily_pipeline.py --allow-session-takeover --send
     --llm-note` (Telegram değişkenleri kalıcı User ortam değişkeni olarak kayıtlı).
7. Telegram botu: çalışıyor; ilk gerçek gönderim 2026-10-03 (as_of 10-02). Token/chat id Efe'nin
   Windows User ortam değişkenlerinde. `run_daily_pipeline.py --send` değişken yoksa toplamadan
   önce durur (`b7c0373`). Efe'nin geri bildirimi: tek uzun mesaj okunmuyor → `notify-layout-v2`.
   Efe kararı 2026-10-03 ("sinyal chati"): varsayılan düzen v3 (günde tek mesaj, hisse başına
   tek satır, `↑ al yönlü`/`↓ sat yönlü`, `Opus: <kelime|—>`); `run_daily_pipeline.py --llm-note`
   v3'te sırası collect → scan → llm_note → tek mesaj, not hatası/zaman aşımı mesajı düşürmez
   (`—`); analiz notu istemi prompt v3 (`getiri/kazanç/kâr` yasağı açık). v1/v2 ve `--llm-note-dir`
   yolu aynen duruyor (`--layout v2`). Bkz. `docs/notify.md`, `docs/prereg/DECISIONS.md` 2026-10-03.
5. KAP erişimi tarayıcıda engelli; KAP belgeleri Efe'den gelir (indirme Java serileştirme
   sarmalıdır, 27 bayt başlık atılınca PDF).
6. Canlı yanıtlarda bilinmeyen olay tipleri görüldü; fiyat/derinlik protokolü keşfedilmedi.

## Çalışma biçimi (özet)

- Yönetici = Opus 5.5, Extra effort; iş alt ajanlarda (`.claude/agents/`, model açık,
  zorluk kriterleri tanımlarda). Testleri/kontrolleri alt ajan yapar, yönetici spot-check eder.
- Geçici: bulut kredisi sürdükçe (en geç 2026-11-05; 2026-10-03'te ~25 USD kaldı) BÜTÜN kod
  yazımı claude.ai/code bulut oturumunda (private `efeeyyy/FinTR`, yeni dal, main'e push yok);
  doğrulamada bulunan küçük düzeltmeler dahil. Yerel alt ajanlar yalnız doğrular/test/veri
  çalıştırır, kod düzenlemez (Efe, 2026-10-03: "kendi limitim dolmasın").
- Efe'ye verilen her komut önce yönetici tarafından çalıştırılır (canlı olanlar `--dry-run`).
- Finansal sonuçlar yönetici sohbetinde gösterilmez.

## Sık komutlar

```powershell
$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe -m unittest discover -s tests
$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe scripts\analyze_period.py --all
$env:PYTHONPATH="src"; .\.venv\Scripts\python.exe scripts\live_collector_smoke.py --help
```
