# ADR taslağı: Mini App ile günlük artımlı toplama

Tarih: 2026-09-28. Durum: **kabul edildi** (Efe, 2026-09-28 — bkz. "Efe'nin
kararları" K1–K5). Özet, `docs/architecture.md`'ye ayrı bir uygulama adımıyla
eklenecek ("Uygulama sırası" madde 6; bu turda yalnız madde 1 ve 3
uygulanmıştır, madde 6 açık iş olarak kalır).
Dayanak: [continuous_data_sources.md](continuous_data_sources.md#karar-2026-09-28)
(Efe, 2026-09-28: kaynak Telegram Mini App; lisans var; veri çekimi ücretsiz).

## Problem

Tarama/bildirim botu her işlem günü güncel AKD, takas ve fiyat verisine ihtiyaç
duyar. Bugün veri yalnız elle başlatılan, uzun aralıklı dönem kayıtlarıyla
(`data/interim/period_analysis/<SYM>/<S>_<E>.json`) geliyor. Günlük kullanım için
eksik olanlar:

1. Günlük plan: hangi hisse, hangi tarih aralığı, eksik gün telafisi.
2. Takasın AKD'nin gerisinde kesinleşmesi (sunucu tarihinde T+2;
   `fintr.scanner.freshness.CUSTODY_STALE_SERVER_DATE_GAP = 2`) ve sağlayıcı
   revizyonları.
3. Kısa aralıklı kayıtların uzun geçmişle birleştirilmesi: tarayıcı
   (`scripts/scan_observations.py::find_period_capture`) sembol başına **en son
   biten tek** dosyayı seçer; kısa bir günlük dosya bir yıllık dosyayı gölgeler.
4. Oturum/kimlik sınırları: `initData` insan girer, tek oturum, 429.
5. Çalıştırma kaydı (ne planlandı, ne kaydedildi, hangi hata, kaç 429).

Mevcut güçlü parça yeniden yazılmamalı: `_collect_multi_symbol` (sembol başına tek
bağlantı, `initData` tek sefer ve yalnız bellekte), `_period_analysis_packet`
(istek profili, yerel gönderim provenance'ı ve sunucu echo doğrulaması),
`_save_period_analysis_capture` (secret kontrolü, atomik, no-overwrite) ve
`load_period_capture` (paket hash doğrulaması) — hepsi `scripts/live_collector_smoke.py`
ve `src/fintr/data/period_capture.py` içinde.

## Alternatifler

- **(a) Yeni günlük kayıt şeması** (`data/interim/daily/<SYM>/<T>.json`, tek gün,
  yeni yazıcı/yükleyici). Temiz görünür ama doğrulanmış paket/kayıt yolunu ikinci kez
  yazmayı gerektirir; takas gecikmesi ve revizyon için yine örtüşen istekler gerekir.
  Reddedildi.
- **(b) Her gün tek bir büyük dönem kaydını yeniden çekmek** (ör. son 1 yıl). Şema
  değişmez, ama günlük yük ve 429 riski en yüksek; `custody_analysis` 90 gün üstü
  `--allow-large-range` ister; mesaj boyutu büyür. Reddedildi.
- **(c) Kayan pencereli dönem kayıtları + deterministik birleştirici.** Her gün
  mevcut yazıcıyla, mevcut şemada (`fintr_period_analysis_capture_v1` /
  `handoff_v2`) kısa ve örtüşen bir pencere kaydedilir; ayrı bir dizinde tutulur.
  Saf bir fonksiyon, temel (uzun) kayıtları ve kayan kayıtları tarih×aile bazında
  birleştirip bellek içi bir `PeriodCapture` ve soy (lineage) kaydı üretir. Örtüşme
  gecikmeli takası ve revizyonları doğal olarak yakalar. **Önerilen.**

## Karar (önerilen)

### 1. Toplama birimi: kayan pencere

- Çalıştırma günü `R` (Europe/Istanbul). Pencere sonu `E = R − 1 gün` (aynı gün
  verisi eksik olabilir; gelecek tarih koruması zaten var). Pencere başı
  `S = E − 14 takvim günü` (≈10 işlem günü: T+2 takas gecikmesi + tatiller + revizyon
  payı). Değerler config'te.
- Telafi: son başarılı pencere sonu `L` ise `S = min(S, L − 14)`; `E − S > 90` gün
  olursa planlayıcı durur ve elle `--allow-large-range` kararı ister (sessiz büyük
  istek yok).
- Telafi tabanı (2026-10-01): çalıştırma kaydı yokken `S = E − 14` yüzünden ilk
  başarı ~2026-10-09'dan sonra olursa temel kaydın bitişinden (2026-09-25) sonraki
  günler hiç toplanmazdı. Bu yüzden temel yıllık kaydın dosyası varsa (config
  `base_capture`: `root` + `filename`; bitiş tarihi dosya adından türetilir,
  planlayıcıya sabit yazılmaz) o sembolün `L`'si en az bu bitiş tarihi sayılır:
  `L = max(son başarılı pencere sonu, temel kayıt bitişi)`. Dosya yoksa taban
  yoktur. Planlayıcı saf kalır (taban tarihleri dışarıdan verilir; dosya
  içeriği açılmaz). 90 gün kuralı değişmedi.
- İstek profili değişmez: `get_brokerages`, `get_akd(S,E)`,
  `custody_analysis(S,E)`, `get_custodian_diff(S,E)`; sembol başına ayrı bağlantı.
- Boyut bütçesi (ölçüm değil): runbook'a göre 14 işlem günlük `custody_analysis`
  ≈133 KB; 20 hisse ≈ 3 MB/gün.

### 2. Depolama

- Kayan kayıtlar: `data/interim/daily_capture/<SYM>/<S>_<E>.json`, şema **aynı**
  (`fintr_period_analysis_capture_v1`). Ayrı kök, tarayıcının "en son biten dosya"
  seçimini bozmaz. Aynı `(S,E)` ikinci kez yazılmaz (mevcut no-overwrite).
- Yazıcıya tek değişiklik: kayıt kökü parametresi (bugün `period_analysis` sabit)
  ve `capture_metadata`'ya kaynak/izin bloğu (aşağıda). Paket şeması değişmez.
- Çalıştırma kaydı: `data/interim/daily_capture/_runs/<run_id>.json`, şema
  `fintr_daily_collection_run_v1` (aşağıda). Finansal değer, kurum adı/kodu,
  `initData`, URL içermez.

### 3. Birleştirici (`compose_period_capture`, saf fonksiyon)

Girdi: bir hissenin temel kayıtları (`period_analysis/<SYM>/*.json`) ve kayan
kayıtları (`daily_capture/<SYM>/*.json`); hepsi `load_period_capture` ile hash
doğrulanarak yüklenir. Çıktı: `PeriodCapture` + `CompositionLineage`.

Kurallar:

1. Günlük seriler (`daily_akd_net`, `daily_akd_market`, `daily_custody_lots`,
   `metadata`) **tarih × aile** bazında seçilir: o tarihi o ailede içeren kayıtlar
   arasında `captured_at_utc` en yeni olan kazanır. Aileler bağımsız seçilir (model
   zaten ayrı seriler olarak tutuyor); seçimin kaynağı lineage'e yazılır.
2. `server_dates` = seçilen kayıtların `server_dates` birleşimi. İşlem takviminde
   olup hiçbir kayıtta olmayan gün **doldurulmaz**; `gaps` listesine girer.
3. Revizyon: aynı tarih×aile iki kayıtta farklı satır kümesi (kanonik JSON hash'i)
   taşıyorsa `revisions`'a yazılır (tarih, aile, eski/yeni kayıt hash'i, farklı kod
   sayısı — değer yok). En yeni kazanır; eski kayıt silinmez.
4. Kesinleşme (settled): AKD günü, ardışık iki kayıtta aynı hash'le görüldüyse;
   takas günü, kendisinden sonra en az 2 sunucu günü içeren bir kayıtta aynı hash'le
   görüldüyse (T+2). Kesinleşmemiş günler lineage'de işaretlenir.
5. Dönem toplamları (`period_akd_net`, `custodian_diff`) birleştirilemez; bileşik
   nesnede boş kalır ve `period_aggregates_available=False` işaretlenir. Bu
   toplamları kullanan araçlar (ör. `analyze_period.py` dönem raporu) bileşik nesneyi
   reddeder. Özellikler zaten bunları kullanmaz (`truncate_period_capture` düşürüyor).
6. Kurum dizini: en yeni kaydınki; hash'i lineage'de.
7. Birleştirici ağ, saat veya rastlantı kullanmaz; aynı girdi dosyaları → aynı çıktı
   ve aynı `composition_sha256`.

Model etkisi: `PeriodCapture`'a varsayılanı `None` olan tek opsiyonel alan
(`composition: CompositionLineage | None`) eklenir; mevcut yükleyici ve testler
değişmez.

### 4. Tarayıcı entegrasyonu

`scan_observations.py`'ye opsiyonel `--daily-root` (varsayılan kapalı): verilirse
sembol verisi birleştiriciden gelir; gözlem çıktısı `composition_sha256`, girdi
dosya hash'leri, `gaps`, kesinleşmemiş ve revize günleri taşır. Varsayılan davranış
aynen kalır. Revize/kesinleşmemiş günlerin gözlemde bastırılıp bastırılmayacağı ayrı
karardır (varsayılan: yalnız işaretle).

### 5. Fiyat

OHLCV kaynağı değişmez (Yahoo chart v8, gayriresmî; Mini App fiyat protokolü
keşfedilmedi). Günlük çalıştırmanın ikinci adımı `fetch_price_history_batch.py`
ile `2023-01-02..E` aralığını yeni dosya olarak kaydeder (no-overwrite, yeni ad);
`discover_price_history` en kısa kapsayan dosyayı zaten seçiyor. Eski günlük fiyat
dosyalarının temizliği ayrı karar (saklama politikası).

### 6. Oturum, kimlik ve hız

- **Aşama A (önerilen başlangıç):** Efe her işlem günü sabit bir pencerede (öneri
  T+1 08:00–09:30, seans öncesi) Mini App'ten `initData`'yı alır, Mini App'i kapatır
  ve tek komutu çalıştırır; `initData` bir kez sorulur (mevcut
  `_CachedSecretProvider`), tüm hisseler insansız biter. Tahmini insan süresi birkaç
  dakika; toplam süre bütçesi 20 hisse × 30 s bekleme ≈ 10 dk + istekler (ölçüm değil).
- **Aşama B (ayrı ADR):** tam insansız çalışma için `initData`'nın işletim sistemi
  anahtar deposunda saklanması. Ön koşul: `initData` ömrünün ölçülmesi (K1b).
- **429:** `handshake_rejected_http_429` ilk görüldüğünde çalıştırma kalan hisseleri
  denemeden durur (`skipped_rate_limited`); bugün bu durum yalnız dolaylı olarak
  (iki ardışık `failed:session_no_send`) durduruyor. Aynı gün otomatik yeniden deneme
  yok; ertesi çalıştırmanın telafi penceresi eksikleri kapatır.
- Semboller arası bekleme config'te (öneri 30 s, mevcut üst sınır); `--max-wall-seconds`
  korunur; `--allow-session-takeover` onayı korunur.
- Fail-closed: beklenmeyen şema (parse hatası) veya **tolere edilmeyen** bilinmeyen
  olay tipi o hisse için kaydı engeller; çalıştırma kaydında kategori olarak
  görünür. Tolere edilen tek küme config'teki `tolerated_control_event_types`
  listesidir (K6: `hb`, `request_queued`, `welcome`); bu üç adın anlamı ad ve
  sıradan çıkarımdır, sağlayıcı dokümanıyla doğrulanmadı, payload'ları hiçbir
  parser tarafından kullanılmaz. Listedeki adlar `^[a-z][a-z_]{0,39}$` kuralına
  uymalı, sıralı ve tekil olmalı, desteklenen veri event tiplerinden (veya
  `error`/`banned`/`market_update`'ten) biri olmamalıdır; aksi hâlde config
  yüklenmez. Listeyi genişletecek CLI bayrağı yoktur: yeni bir ad ancak config
  değişikliği (ve karar kaydı) ile eklenir. Listede olmayan her bilinmeyen tip
  (güvensiz adlar `[UNKNOWN_EVENT_TYPE]` olarak) hâlâ `failed:unknown_event_type`
  verir. Çalıştırma kaydı tolere edilen ve engelleyen adları sayılarıyla ayrı
  yazar (aşağıda, "Şemalar").

### 7. Kapsam ve veri bütünlüğü

- Günlük toplama yalnız **20 geliştirme hissesi** (etkin ayrım
  `bist30_2026_holdout_v2`); holdout hisseleri günlük toplanmaz, DSTKF hiçbir zaman.
  Gerekçe: tarayıcı zaten yalnız geliştirme hisselerini okur; yük ve 429 riski azalır;
  holdout verisine gereksiz temas olmaz.
- Holdout testi kaynağı **değişmez**: 2027-03-31 sonrası tek seferlik dönem kaydı
  (`STATUS.md` açık iş 1). Günlük kayıtlar `run_holdout_test.py` girdisi olmaz.
- Test penceresi (2026-10-01..2027-03-31) boyunca geliştirme hisselerinin günlük
  gözlemleri H1–H4 spec'lerini, tarayıcı kurallarını veya eşikleri değiştirmek için
  kullanılmaz (dondurulmuş hash kontrolü zaten zorunlu; bu ADR bunu süreç kuralı
  olarak da yazar).
- D0 revizyon karşılaştırması (günlük ↔ temel kayıt) holdout testinden önce yalnız
  **2026-09-25 ve öncesi** tarihlerde yapılır.
- Efe kararı K4 (2026-09-28), ek süreç kuralı: 2027-03-31'e kadar H1–H4 için
  test penceresi verisiyle **ara performans değerlendirmesi yapılmaz**
  (gözlem sonrası getiri hesabı veya isabet sayımı dahil, günlük toplanan
  geliştirme hisseleri için de); bu, kural ve eşiklerin dondurulmuş halini
  değiştirmez, yalnız test penceresi bitmeden sonuç bakılmasını engeller.

## Şemalar

### Kayan kayıt `capture_metadata` eki

Paket (`handoff_v2`) değişmez. `capture_metadata`'ya ek alanlar (eski kayıtlarda yok;
yükleyici opsiyonel okur):

```json
{
  "source": {
    "source_id": "veri_terminali_miniapp_ws_v1",
    "protocol_reference": "docs/protocol_inventory.md",
    "collection_mode": "daily_rolling_window_v1"
  },
  "permission_provenance": {
    "status": "user_asserted_2026-09-28_not_independently_verified",
    "basis": "Efe beyanı 2026-09-28: Mini App lisansı var, veri çekimi ücretsiz",
    "record": "docs/research/continuous_data_sources.md#karar-2026-09-28",
    "independently_verified": false
  },
  "run": {
    "run_id": "<çalıştırma başlangıcı, UTC ISO zaman damgası>",
    "run_date_local": "YYYY-MM-DD",
    "window_rule": "E=R-1; S=E-14d; catch-up from last successful end",
    "config_sha256": "<configs/collection/daily_miniapp_v1.json kanonik hash>",
    "git_commit": "<HEAD>",
    "git_tree_clean": true
  }
}
```

### Çalıştırma kaydı `fintr_daily_collection_run_v1`

```json
{
  "schema": "fintr_daily_collection_run_v1",
  "run_id": "...",
  "started_at_utc": "...", "finished_at_utc": "...",
  "run_date_local": "YYYY-MM-DD", "timezone": "Europe/Istanbul",
  "git_commit": "...", "git_tree_clean": true,
  "config_path": "configs/collection/daily_miniapp_v1.json", "config_sha256": "...",
  "split_config_sha256": "...", "exclusions_sha256": "...",
  "environment": {"python": "3.12.x", "websockets": "17.x", "os": "Windows ..."},
  "init_data": {"prompted": true, "age_seconds_bucket": null},
  "symbols": [
    {
      "symbol": "AKFIS", "role": "development",
      "window": {"start": "...", "end": "...", "catch_up": false},
      "status": "saved | skipped_existing | skipped_excluded | skipped_holdout | skipped_rate_limited | skipped_wall_time_guard | failed:<kategori>",
      "capture_path": "data/interim/daily_capture/AKFIS/<S>_<E>.json",
      "capture_packet_sha256": "...",
      "frames_used": 0,
      "diagnostics": [{"attempt": 1, "stage": "connect", "category": "handshake_rejected_http_429"}],
      "unknown_event_types_count": 0,
      "unknown_event_types": {
        "tolerated": [{"name": "hb", "count": 3}],
        "blocking": [{"name": "[UNKNOWN_EVENT_TYPE]", "count": 1}]
      }
    }
  ],
  "tolerated_control_event_types": {"names": ["hb", "request_queued", "welcome"], "sha256": "..."},
  "rate_limit": {"http_429_count": 0, "stopped_on_429": false},
  "price_history": {"status": "saved | skipped | failed", "files": ["..."]},
  "notes": "yalnız sabit kategoriler; finansal değer yok"
}
```

`init_data.age_seconds_bucket`: yalnız K1b onaylanırsa doldurulur (aşağıda).

Kontrol frame alanları (2026-10-01, K6): `unknown_event_types` o sembolün
bilinmeyen event tiplerinin adlarını ve sayılarını, tolere edilen / engelleyen
olarak ayrı, ada göre sıralı yazar; `unknown_event_types_count` ikisinin
toplamı olarak aynen kalır. Adlar `_event_status_label` ile aynı kuralla
temizlenir (`^[a-z][a-z_]{0,39}$`'e uymayan ad `[UNKNOWN_EVENT_TYPE]` yazılır,
ham metin kaydedilmez). Çalıştırma düzeyindeki `tolerated_control_event_types`
config listesini ve sha256'sını taşır (sha256: sıralı adların kompakt JSON
dizisinin, ör. `["hb","request_queued","welcome"]`, UTF-8 baytları). Alanlar
yalnız ekleme olduğundan **şema sürümü artırılmadı** (`fintr_daily_collection_run_v1`):
okuyucular (`scan_last_success`) yalnız `schema`, `symbols[].symbol/status/window`
okur; eski kayıtlarda bu alanlar yoktur. Yeni config alanları da opsiyoneldir:
`tolerated_control_event_types` yoksa hiçbir tip tolere edilmez, `base_capture`
yoksa telafi tabanı uygulanmaz.

### Birleştirme soyu `fintr_period_composition_v1`

```json
{
  "schema": "fintr_period_composition_v1",
  "symbol": "AKFIS",
  "composer_version": "compose_period_capture_v1",
  "inputs": [{"path": "...", "packet_sha256": "...", "captured_at_utc": "...", "kind": "base | rolling"}],
  "selection": [{"date": "YYYY-MM-DD", "family": "akd | akd_market | custody | metadata", "packet_sha256": "..."}],
  "gaps": ["YYYY-MM-DD"],
  "revisions": [{"date": "...", "family": "custody", "superseded_packet_sha256": "...", "winning_packet_sha256": "...", "differing_code_count": 3}],
  "unsettled": [{"date": "...", "family": "custody"}],
  "period_aggregates_available": false,
  "composition_sha256": "..."
}
```

Bellek içinde üretilir; tarayıcı çıktısına gömülür. Diske ayrı dosya yazımı gerekmez.

### Config `configs/collection/daily_miniapp_v1.json` (öneri)

```json
{
  "schema": "fintr_daily_collection_config_v1",
  "source_id": "veri_terminali_miniapp_ws_v1",
  "symbol_scope": "split_development_only",
  "split_config": "configs/splits/current.json",
  "exclusions_config": "configs/exclusions/symbols.json",
  "window": {"end_offset_days": 1, "lookback_calendar_days": 14, "max_span_days": 90},
  "pacing": {"symbol_pause_seconds": 30, "max_wall_seconds": 3600},
  "collector": {"max_frames": 16, "timeout_seconds": 15, "max_message_mib": 4},
  "stop_on_http_status": [429],
  "capture_root": "data/interim/daily_capture",
  "base_capture": {"root": "data/interim/period_analysis", "filename": "2025-09-26_2026-09-25.json"},
  "tolerated_control_event_types": ["hb", "request_queued", "welcome"],
  "permission_status": "user_asserted_2026-09-28_not_independently_verified",
  "price_history": {"start_date": "2023-01-02", "pacing_seconds": 1.5}
}
```

## Doğrulama (uygulama PR'larıyla)

Offline (her PR'da, `unittest`):
- Planlayıcı: pencere hesabı, telafi, 90 gün sınırı, tatil/hafta sonu `R`, holdout/
  dışlama atlama, dry-run'ın ağ açmaması ve `initData` sormaması.
- Birleştirici: sentetik kayıtlarla en-yeni-kazanır, aile bağımsız seçim, revizyon,
  boşluk (asla doldurulmaz), kesinleşme kuralları, determinizm (`composition_sha256`
  sabit), hash uyuşmazlığında red, dönem toplamlarının boş ve işaretli olması.
- 429 durdurma, çalıştırma kaydının secret/finansal değer içermemesi
  (`validate_context_contains_no_secrets` ile aynı denetim), yazıcının yeni kökte
  no-overwrite davranışı.
- Tarayıcı: `--daily-root` verilmezse çıktı şeması ve içerik mevcut davranışla aynı.

Canlı (Efe, kabul sonrası):
- İlk hafta her gün Aşama A çalıştırması; çalıştırma kaydında 429 sayısı, süre,
  kaydedilen/başarısız hisse.
- D0: kayan kayıtların 2026-09-25 ve öncesine düşen günleri, temel bir yıllık
  kayıtlarla tarih×aile hash'i ve kurum bazında karşılaştırılır (yalnız geliştirme
  hisseleri; değer yazdırılmaz, eşleşme oranı ve farklı kod sayısı raporlanır).

## Sonuçlar

- Yeni kayıt şeması yok; doğrulanmış yazıcı/yükleyici yeniden kullanılır. Yeni kod:
  planlayıcı + çalıştırıcı, birleştirici, tarayıcı bayrağı, çalıştırma kaydı.
- Günlük toplama hâlâ bir insan adımı içerir (Aşama A). "Tam insansız" hedefi Aşama B
  kararına bağlıdır.
- Kaynak tek; protokol kırılırsa günlük veri durur. Yedek analiz
  `continuous_data_sources.md`'de.
- Disk: ≈3 MB/gün kayan kayıt + günlük fiyat dosyaları; saklama/temizlik politikası
  ayrıca kararlaştırılmalı.
- **Risk notu (2026-09-28):** Yahoo'nun kullanım koşulları otomatik veri
  çekimini yasaklıyor olabilir (doğrulanmadı; bkz.
  [continuous_data_sources.md](continuous_data_sources.md#6-kamuya-açık-web-kaynakları)).
  Efe'nin izin beyanı (`user_asserted_2026-09-28_not_independently_verified`)
  yalnız Mini App kaynağı içindir; Yahoo `price_history` için ayrı ve önceki
  (`user_asserted_2026-09-26_not_independently_verified`) beyanla sınırlıdır,
  bu ADR onu genişletmez.

## Uygulama sırası (küçük, ayrı PR'lar)

1. Config + planlayıcı (yalnız `--dry-run`; ağ yok) ve testleri.
2. Yazıcıya kayıt kökü parametresi + `capture_metadata` eki; günlük çalıştırıcı
   (`_collect_multi_symbol` yeniden kullanılır), 429 durdurma, çalıştırma kaydı.
3. `compose_period_capture` + `CompositionLineage` + testler.
4. `scan_observations.py --daily-root`.
5. Günlük fiyat adımı ve D0 karşılaştırma betiği.
6. `docs/architecture.md` ADR özeti, `docs/collector.md` ve runbook güncellemesi.

## Efe'nin kararları

| # | Soru | Öneri | Efe'nin yanıtı (2026-09-28) |
|---|---|---|---|
| K1 | Aşama A (günlük tek `initData` girişi) ile başlansın mı? | Evet; B ayrı ADR | Evet (Aşama A). |
| K1b | `initData` ömrünü ölçmek için değer içinden yalnız `auth_date` bellekte okunup çalıştırma kaydına yaş kovası (ör. `<1h`, `1–24h`, `>24h`) ve kabul/ret yazılsın mı? Değer ve hash asla yazılmaz. | Evet; B kararının ön koşulu | Evet, ama yalnız kayıt amaçlı. Ömür ölçümü Aşama B'de ayrı bir deneydir; taze girilen değerin yaşı ömrü göstermez. |
| K2 | Günlük kapsam yalnız 20 geliştirme hissesi mi? | Evet | Evet. |
| K3 | Depolama: kayan pencere + birleştirici (c) mi? | Evet | Evet. |
| K4 | Holdout testi kaynağı Nisan 2027 tek seferlik kayıt olarak kalsın; test penceresi gözlemleri spec/kural değişikliğine girdi olmasın mı? | Evet | Evet. Ek süreç kuralı: 2027-03-31'e kadar H1–H4 için test penceresi verisiyle ara performans değerlendirmesi yapılmaz (gözlem sonrası getiri hesabı veya isabet sayımı dahil); kural ve eşik değişikliği yok. |
| K5 | Çalıştırma penceresi ve makine (Efe'nin Windows makinesi, T+1 08:00–09:30)? | Efe belirler | Efe'nin Windows bilgisayarı, T+1 08:00–09:30 (Europe/Istanbul). Sağlayıcının yayın saati doğrulanmadı; son günün eksik gelmesi boşluk sayılır ve sonraki çalıştırmada tamamlanır, bu durum çalıştırma kaydında izlenir. |
| K6 | İlk canlı günlük çalıştırmada (2026-10-01) 20 sembolün tamamı `failed:unknown_event_type` oldu; dört beklenen yanıt gelmişti. Tek sembollü probda yalnız `welcome` (ilk frame, bir kez), `hb` ve `request_queued` (her istekten sonra bir kez) bilinmeyen tip olarak görüldü. Bu üç tip günlük çalıştırmada tolere edilsin mi? | Evet, yalnız bu üç ad; başka her bilinmeyen tip fail-closed kalsın | **2026-10-01, Efe: "evet olsun".** Yalnız `hb`, `request_queued`, `welcome` tolere edilir (config `tolerated_control_event_types`; CLI ile genişletilemez). Üç adın anlamı (karşılama / heartbeat / istek kuyruğa alındı) ad ve sıradan çıkarımdır, sağlayıcı dokümanıyla doğrulanmadı; payload'ları hiçbir parser tarafından kullanılmaz. Tolere/engelleyen adlar çalıştırma kaydına yazılır (§6, "Şemalar"). |

K4'ün ek süreç kuralı §7'ye de işlendi (aşağıda).
