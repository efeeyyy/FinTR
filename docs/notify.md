# Telegram gözlem bildirimi (`fintr.notify`)

Aşama 2'nin ikinci adımı: `scripts/scan_observations.py` tarafından
kaydedilmiş gözlem dosyasını (`data/interim/scanner/<as_of>/observations.json`,
şema `fintr_scanner_observations_v1` ya da `_v2` -- `_v2` yalnız tarayıcı
`--daily-root` ile çalıştırıldıysa, bkz. `docs/scanner.md`) okuyup Efe'nin
kendi Telegram botuyla, yalnız Efe'nin sohbetine düz metin bildirim olarak
gönderir. `fintr.notify.format.SUPPORTED_OBSERVATIONS_SCHEMAS` her iki
şemayı da kabul eder; eski (`_v1`, `--daily-root` öncesi ya da onsuz
üretilmiş) dosyalar aynen okunmaya devam eder.

## Sinyal chati: düzen v3 (varsayılan)

Efe kararı (2026-10-03, `docs/prereg/DECISIONS.md`): Telegram "sinyal chati" olarak
kullanılır; **günde tek mesaj, hisse başına tek satır**. `--layout v3` varsayılandır
(`src/fintr/notify/layout_v3.py`); `--layout v1` ve `--layout v2` aynen kalır. Uzun açıklama,
kural metni ve "Ne demek" bu düzende yoktur.

```
📊 02.10 sinyaller (doğrulanmamış hipotez, tavsiye değil)
AKFIS ↑ al yönlü · H2+H4 · Opus: destekliyor
BIMAS ↓ sat yönlü · H1 · Opus: —
veri: 2 gün kesinleşmemiş
```

Gözlem yoksa tek satır: `📊 02.10 sinyal yok` (veri satırı olmadan).

- **Yön:** yalnız dondurulmuş kuralın `predicted_direction` alanından (`positive` →
  `↑ al yönlü`, `negative` → `↓ sat yönlü`; hash doğrulamalı yükleyici). Yön bilinmiyorsa ya da
  `--no-direction-label` verilmişse yön kısmı yazılmaz (`AKFIS · H2 · Opus: —`); tahmin yok.
- **Birleştirme:** bir hissede aynı yönlü birden çok kural tek satırda `H2+H4` olur; yönleri
  farklıysa ayrı satır kullanılır (önce yukarı, sonra aşağı, sonra yönsüz). Aynı hisse iki farklı
  günde koşul sağlarsa (çok günlük tarama) sembolün yanına `(GG.AA)` yazılır.
- **`Opus:`** yalnız o hisse, kural ve gün için **kabul edilmiş (`accepted`)** analiz notunun
  kanıt değerlendirmesi kelimesidir (`destekliyor`/`zayıflatıyor`/`belirsiz`); not yoksa,
  reddedilmişse, başarısızsa ya da bütünlük yeniden doğrulamasını geçemiyorsa `—`. Birleştirilen
  kurallarda kelimeler farklıysa kural başına yazılır (`Opus: H2 destekliyor, H4 belirsiz`); kod hiçbir
  kelime üretmez. Kelimeler `--note-assessments-dir data/interim/llm_notes` ile okunur
  (`fintr.llm_note.render.accepted_assessments`); bayrak yoksa hepsi `—`. `--llm-note-dir`'den
  farklı olarak ayrı not mesajı göndermez; `--note-assessments-dir` yalnız v3 ile geçerlidir.
- **Veri satırı:** yalnız pencerede kesinleşmemiş gün, boşluk ya da revize varsa, sona tek kısa
  satır (`veri: 2 gün kesinleşmemiş, 1 gün boşluk, 1 gün revize`). Sayılar kayıtlı `composition`
  alanındaki pencere içi sayımlardır; taranan hisseler arasında her tür için **en büyük** sayı
  gösterilir (toplanmaz). Pencere bilgisi olmayan hisse varsa `durum bilgisi yok` eklenir.
- **Uzunluk:** normalde tek mesaj; 4096 karakteri aşacak kadar hisse varsa satırlar sonraki
  mesajlara bölünür ve her mesaj başlık satırını tekrarlar (20 hisselik en kötü durum testle
  sınırın altında tutulur).
- **Yasak:** getiri, puan, hedef fiyat, stop, pozisyon büyüklüğü eklenmez. LLM al/sat ya da karar
  üretmez (AGENTS.md); satırdaki kelime doğrulanmış notun alanının aynen aktarımıdır.
- **Receipt / devam:** düzen v2 ile aynı mekanizma (`receipt*.json`, `fintr_notify_receipt_v2`,
  `layout: "v3"`; `--note-assessments-dir` verildiyse `note_assessments` alanı klasörü ve gösterilen
  değerlendirme sayısını kaydeder). Aynı `as_of` için tamamlanmış receipt varsa yeniden gönderilmez
  (`--resend` gerekir).
- **Pipeline:** `run_daily_pipeline.py --llm-note` v3'te sırası toplama → tarama → `llm_note.py` →
  tek v3 mesajı; `--llm-note` olmadan v3 yine gönderilir (hepsi `—`). `llm_note.py` hata verir, zaman
  aşımına uğrar (config'teki `max_run_seconds` + 300 sn) ya da bir hisse reddedilirse mesaj yine gider,
  eksik değerlendirmeler `—` olur; deterministik bildirim nota bağlı değildir. Aynı gün yeniden
  çalıştırmada receipt varsa not adımı da tekrarlanmaz (`llm_note: skipped_already_sent`) ve mesaj
  yeniden gönderilmez. `--layout v2` eski sırayı korur (bildirim, sonra ayrı not mesajı).

## Mesaj düzeni v2 ve `--layout v1`

Düzen v2 (`src/fintr/notify/layout.py`; `--layout v2` ile seçilir, v3'ten önceki varsayılandı):
**önce bir özet mesajı, sonra koşulu sağlayan her hisse için ayrı mesaj**.
`--layout v1` önceki tek düz metni birebir korur (aşağıdaki "Beklenen yön
etiketi" ve "Veri durumu satırı" bölümleri v1 metnini anlatır). Eski v1/v2
gözlem dosyaları (`fintr_scanner_observations_v1`/`_v2`) her iki düzende de
okunur. Metin düz metindir (`parse_mode` yok), bu yüzden HTML escape gerekmez.

Özet mesajı (etiketli düzen):

```
FinTR · 02.10.2026 (Cuma kapanışı)
Koşulu sağlanan: 5 hisse — 3 yukarı, 2 aşağı
Veri: 20 hissenin hepsi tam (boşluk 0) · 2 gün kesinleşmemiş
⚠ Yön etiketi doğrulanmamış hipotezdir (test: Nisan 2027); yatırım tavsiyesi değildir.
```

Koşulu sağlayan her hisse için bir mesaj (örnek, sentetik):

```
▲ ABCDE — beklenen yön: yukarı (al yönlü)
⚠ Doğrulanmamış hipotez; yatırım tavsiyesi değildir.
Kural H2: ortalama fiyat (VWAP proxy) kapanışın en az %1, en çok %10 üstünde (daha büyük sapmalar veri/ölçek uyumsuzluğu ihtimali nedeniyle kural dışı)
Bugün: Ortalama fiyat (VWAP proxy) kapanışın %1,53 üstünde
Ne demek: Gün içinde lotlar ortalama olarak kapanıştan daha yüksek fiyattan el değiştirdi; ...
Vade: 5 işlem günü · kural sürümü 42fca88a
```

Aşağı yön için `▼` ve `beklenen yön: aşağı (sat yönlü)`. Hiçbir hissede koşul
yoksa tek mesaj: `FinTR · <tarih> (<gün> kapanışı): bugün hiçbir hissede koşul
sağlanmadı` + veri satırı (`--skip-empty` önceki gibi hiçbir şey göndermez).

- **Ham değer yok.** `vwap_proxy_vs_close` ve `top1_akd_net_share_of_volume` ham
  hâliyle mesajda görünmez; ham değer gözlem dosyasında (ve receipt'teki
  `observations_sha256` ile) kalır. Mesajdaki yüzdeler *hesaplanmış* değerlerdir.
- **Yüzde formülü** (kodda `layout.vwap_vs_close_percent`/`share_percent`; 2026-10-03
  Efe kararıyla `-v/(1+v)` yeniden tabanlaması bırakıldı):

  ```
  v   = vwap_proxy_vs_close = (vwap_proxy - close) / close      (docs/prereg/H1.md; saklanan ham değer)
  pct = |v| * 100                                               (işaretsiz gösterilir)
  ```

  Mesajdaki yüzde dondurulmuş özelliğin kendisidir; özne VWAP proxy'dir ve
  kapanışın yüzdesi olarak ölçülür. `v > 0` ⇒ VWAP proxy kapanışın **üstünde**
  (H2, H4 koşulları: `v ≥ 0,01`); `v < 0` ⇒ **altında** (H1, H3: `v ≤ -0,01`).
  "üstünde/altında" kelimesi kural kimliğinden değil `v`'nin işaretinden gelir.
  Örnek (sentetik): `v = 0,01534` ⇒ "Ortalama fiyat (VWAP proxy) kapanışın %1,53
  üstünde"; `v = -0,02` ⇒ "… kapanışın %2,00 altında"; eşik `v = ±0,01` ⇒ "%1,00".
  Kural satırlarındaki "en az %1" ve "en çok %10" dondurulmuş spec'ten gelir
  (`|v| ≥ 0,01` ve veri kalitesi bandı `|v| ≤ 0,10`, `configs/studies/prereg/h1..h4_*.json`);
  yeni rakam yoktur. `v ≤ -1` (VWAP proxy ≤ 0, gerçek olamaz) okunamaz sayılır.
  `top1_akd_net_share_of_volume` zaten 0..1 arası paydır, `pay * 100` olarak
  gösterilir ("tek kurum net payı %12,35 (günlük toplama oranı)"). Yuvarlama: iki
  ondalık, yarıdan yukarı, Türkçe virgül. Okunamayan değer (yok, sayı değil,
  `v ≤ -1`, pay 0..1 dışı) tahmin edilmez: "okunamadı" yazılır.
- **Veri durumu toplanır.** Sembol başına `composition` sayıları (`boşluk`,
  `kesinleşmemiş gün`, `revize gün`; pencere-sınırlı, bkz. aşağıdaki v1 bölümü)
  aynıysa tek satır ("20 hissenin hepsi tam (boşluk 0) · 2 gün kesinleşmemiş");
  değilse en büyük grup `Veri: 19/20 hisse ...` satırında sayılır ve farklı her
  grup `Farklı: SYM1, SYM2 — eksik (boşluk 1) · ...` satırında listelenir. `tam` =
  pencerede boşluk yok. Revize gün yalnız sıfırdan büyükse yazılır. `composition`
  yoksa ya da pencere yoksa bunu söyler. Eksik tazelik alanı, bastırılan ve
  değerlendirilemeyen gözlem satırları yalnız doluysa eklenir; T+2 takas satırı v2'de
  yoktur (H1–H4 takas kullanmaz; bilgi gözlem dosyasında durur).
- **Uyarı etiket taşıyan her mesajda.** AGENTS.md istisnası etiketin "her mesajda
  doğrulanmamış hipotez uyarısıyla" verilmesini ister: özet mesajı
  `⚠ Yön etiketi doğrulanmamış hipotezdir (test: Nisan 2027); yatırım tavsiyesi değildir.`
  satırını, etiketli her hisse mesajı başlığın hemen altında sabit
  `⚠ Doğrulanmamış hipotez; yatırım tavsiyesi değildir.` satırını taşır (etiketsiz
  düzen ve "yön bilinmiyor" mesajlarında yok). Ek olarak "Ne demek" cümlesi
  "Ön kayıtlı, henüz doğrulanmamış hipotez:" ibaresini taşır. "Kural sürümü" = `rules_file_sha256`
  değerinin ilk 8 hanesi (tam hash receipt'te); "Vade" = dondurulmuş spec'in
  `primary_horizon_trading_days` alanı (hash doğrulamalı yükleyiciden).
- **Sıra.** Hisse mesajları sembole göre alfabetiktir; büyüklüğe göre sıralama
  (rank) yoktur. Bir hissede birden çok kural sağlanırsa (ör. H2 + H4) tek mesaj
  olur: birden çok `Kural`/`Ne demek (Hx)` satırı, tek `Bugün` satırı. Çok günlü
  taramada (`--lookback-days`) başlık tarih aralığını verir, hisse başlığı tarih
  taşır ve sayım "hisse-gün" olur.
- **Etiketsiz düzenler.** `--no-direction-label`: ok, etiket, "Ne demek", "Vade" ve
  uyarı yok (`<SYM> — koşul sağlandı`, kural satırı, `Bugün`, `Kural sürümü`; özet
  önceki sabit uyarı + `Durum:` ile biter). Yön çözülemezse (kural dosyası
  doğrulanamadı, bir hissedeki kurallar çelişkili) hisse `• <SYM> — yön bilinmiyor`
  başlığıyla gösterilir, "Ne demek" ve "Vade" yazılmaz.
- **Uzunluk.** Her mesaj en çok 4096 karakter; aşan metin satır sınırında bölünür.

### Sabit açıklama metinleri (`configs/notify/rule_explanations_v1.json`)

Şema `fintr_notify_rule_explanations_v1`; kural başına `rule_line` ("Kural" satırı)
ve `meaning` ("Ne demek", 1–2 cümle). Kaynak yalnız `docs/prereg/H1.md`–`H4.md`
"Hipotez"/"Gerekçe" bölümleri ve spec `condition` alanıdır; çalışma anında metin
üretilmez, akademik atıf ve niyet/manipülasyon ima eden ifade yoktur. Yükleyici
(`fintr.notify.explanations`) dosyayı bayt olarak okur, her metni yasak ifade
korumasından (`fintr.notify.guard`: score/rank/return/target/al/sat/öneri/kesin/
hedef/stop/kâr; yalnız iki yön etiketi dışarıda) geçirir ve SHA-256'yı receipt'e
yazar. Bir kuralın `meaning` alanı yoksa "Ne demek" satırı atlanır (uydurulmaz);
`rule_line` yoksa `rule_texts_v1.json` açıklamasına düşülür.

### Teslimat: bekleme, kısmi gönderim, receipt v2

- Mesajlar sırayla gider; ardışık iki mesaj arası `configs/notify/delivery_v1.json`
  `inter_message_delay_seconds` (varsayılan 1 sn; `--message-delay-seconds` ile
  geçersiz kılınır). Bir run'ın ilk mesajından önce beklenmez.
- Receipt (`fintr_notify_receipt_v2`) tüm mesajları kapsar: `message_count`,
  `message_sha256` (hepsi), mesaj başına `message_status` (`sent` + `telegram_message_id`
  ya da `not_sent`), `complete`, `failed_at_index`, `error_kind` (yalnız istisna sınıf
  adı), girdi dosyası SHA-256'sı, tam `rules_file_sha256`, `rule_explanations`
  (yol + SHA-256), `delivery_config` (yol + SHA-256 + bekleme). Token/chat id hiçbir
  zaman yazılmaz.
- **Kısmi gönderim.** 3. mesajda hata olursa ilk iki mesajın kaydı `receipt.json`'a
  `complete: false` ile yazılır, komut hata koduyla çıkar. Aynı `as_of` için komut
  tekrar çalıştırılınca (`--resend` olmadan) yalnız gönderilmemiş mesajlar gönderilir
  (Ctrl-C ile kesilen gönderim de aynı şekilde kaydedilir; `error_kind` = `KeyboardInterrupt`)
  ve `receipt.part-002.json` (sonra `-003`, ...) yazılır; her dosya o andaki tüm
  mesaj durumunu taşır. Receipt dosyalarının üzerine hiçbir zaman yazılmaz
  (`O_EXCL`). Tüm mesajlar gidince bir sonraki çalıştırma eskisi gibi "receipt zaten
  var" ile reddedilir. İlk mesaj bile gitmediyse receipt yazılmaz (durum değişmez).
- Devam için yeni mesajların SHA-256'ları ve gözlem dosyası SHA-256'sı kısmi
  oturumunkiyle birebir aynı olmalıdır; değilse hiçbir şey gönderilmez ve baştan
  göndermek için `--resend` istenir. `--resend` her zaman tam, yeni bir gönderim
  oturumu başlatır (`receipt-<utc-damga>.json`).
- Bilinen sınır: Telegram mesajı kabul edip yanıt zaman aşımına uğrarsa o mesaj
  kayıtsız kalır ve devam çalıştırmasında bir kez daha gidebilir (çift gönderim
  bu tek durumda mümkündür).

### Bayraklar (v2)

`--layout {v1,v2,v3}` (varsayılan v3), `--note-assessments-dir DIR` (yalnız v3),
`--rule-explanations PATH`, `--delivery-config PATH`,
`--message-delay-seconds N`; mevcut `--no-direction-label`, `--skip-empty`, `--send`,
`--resend`, `--rules-file` aynen çalışır.

## Bu bir yatırım tavsiyesi değildir

Bildirim, tarayıcının ürettiği gözlemin aynen özetidir. `docs/scanner.md`'deki
sınırlar burada da geçerlidir: **getiri, hedef, puan, sıralama, stop seviyesi,
pozisyon büyüklüğü veya serbest metin al/sat önerisi üretmez**; tek istisna
aşağıdaki "Beklenen yön etiketi" bölümüdür. Her mesajın sonunda sabit Türkçe
uyarı metni ve `validation_status` yer alır. Mesaj yalnız `condition_met: true` olan
gözlemleri, tazelik uyarılarını (T+2, eksik veri), bastırılan gözlem
sayılarını (nedenlerine göre) ve -- yalnız `--daily-root` ile üretilmiş
dosyalarda -- sembol başına tek satırlık bir veri durumu satırı listeler.

### Beklenen yön etiketi (Efe kararı, 2026-10-02; yalnız kişisel kullanım)

Varsayılan olarak her `condition_met: true` satırı, ilgili dondurulmuş kuralın
hipotez yönüyle etiketlenir:

```
AKFIS H2: <kural açıklaması> — vwap_proxy_vs_close=... — beklenen yön: yukarı (al yönlü)
AKFIS H1: <kural açıklaması> — vwap_proxy_vs_close=... — beklenen yön: aşağı (sat yönlü)
```

- **Kaynak:** dondurulmuş spec'in `preregistration.predicted_direction` alanı
  (`positive` → "yukarı (al yönlü)", `negative` → "aşağı (sat yönlü)"). Alan,
  tarayıcının hash doğrulamalı yükleyicisiyle (`fintr.scanner.rules.load_scanner_rules`,
  `fintr.notify.direction.load_rule_directions`) `configs/scanner/rules_v1.json`
  üzerinden okunur (`--rules-file` ile değiştirilebilir). Kural çözülemezse, spec
  hash'i uymazsa ya da alan tam olarak `positive`/`negative` değilse satır etiketsiz
  kalır ve `— yön bilinmiyor` işareti taşır; yön asla tahmin edilmez.
- **Uyarı:** etiket içeren her mesaj bir kez
  "Uyarı: yön etiketi doğrulanmamış ön kayıtlı hipotezdir (holdout testi 2027 Nisan);
  yatırım tavsiyesi değildir." satırını taşır (mesaj bölünürse etiketli her parça).
- **Alt bilgi:** etiketli mesajda sabit metin "…kişisel kullanım; doğrulanmamış hipotez
  yönü; yatırım tavsiyesi, niyet veya manipülasyon iddiası değildir…" olur (tarayıcının
  "al/sat sinyali değildir" metni etiketle çeliştiği için yerine geçer); etiketsiz
  mesajda önceki metin aynen kalır. `Durum:` satırı değişmez.
- **Değişmeyenler:** kural, eşik ve spec değişmez; tarayıcı çıktı şeması değişmez;
  etiket gözleme yeni bilgi eklemez, yalnız spec'te kayıtlı yönü ifade eder. Getiri,
  puan, sıralama, hedef fiyat, stop seviyesi, pozisyon büyüklüğü eklenmez. Holdout
  testi ve K4 geçerlidir; etiket bir avantaj iddiası değildir.
- **Kapatma:** `--no-direction-label` önceki metni birebir geri getirir (etiket, işaret
  ve ek uyarı yok). Karar kaydı: `docs/prereg/DECISIONS.md` 2026-10-02.

### Veri durumu satırı (yalnız `--daily-root` çıktısında)

Gözlem dosyasının `symbols.<SYM>.composition` alanı varsa (tarayıcı
`--daily-root` ile çalıştırılmışsa), o sembol için şu biçimde tek bir
satır eklenir:

```
AKFIS veri durumu (2026-09-08..2026-09-25): boşluk=1, kesinleşmemiş gün=2, revize gün=1
```

Üç sayı da `docs/scanner.md`'deki `composition` bloğundan gelir ve
`composition.window` aralığıyla (günlük girdilerin en erken sunucu tarihi..
as_of, iki uç dahil) sınırlıdır: `boşluk` = `composition.gaps.in_window_count`,
`kesinleşmemiş gün` = `composition.unsettled.in_window_day_count` (tekil gün
sayısı, aile bazlı değil), `revize gün` = `composition.revisions
.in_window_day_count`. Pencere alanları olmayan eski `_v2` dosyasında satır
tüm dönem toplamlarına düşer ve `(tüm dönem)` diye etiketlenir; `window: null`
(günlük girdi yok) ise satır `veri durumu: pencere yok (günlük kayıt yok)`
olur. Bu satır yalnız sayı taşır; hiçbir
değer, iddia ya da al/sat ifadesi eklemez -- mevcut yasak kelime testleri
(`tests/test_notify.py`; yalnız iki etiket ifadesine izin verir) bu satırı da kapsar. `composition` alanı yoksa
(varsayılan, `--daily-root` verilmeden üretilmiş her dosya) bu satır hiç
eklenmez.

### Analiz notu (`--llm-note-dir`, Efe kararı 2026-10-02; varsayılan kapalı)

**Not verisi çalışmalara girmez (Efe kararı 2026-10-14).** Notun değerlendirme kelimesi ve
metni hiçbir istatistiksel çalışmaya, holdout testine, model/kural/eşik seçimine ya da holdout
verisiyle istem optimizasyonuna girdi olmaz (`docs/prereg/DECISIONS.md` 2026-10-14). Her
`record.json` model kimliğini (`model_requested`, `model_reported`) ve zamanı (`started_at_utc`)
taşır; üretim günü (Europe/Istanbul) `as_of`'tan sonraki ilk hafta içi günden (cuma → pazartesi)
sonraysa `generated_late: true` yazılır (sonradan üretilmiş not). Tatiller yok sayılır (repoda
takvim yok).

`notify_observations.py --llm-note-dir data/interim/llm_notes` verilirse **yalnız**
Opus 5.5 analiz notu, kendi mesajı ve kendi makbuzuyla (`note_receipt.json`)
yazdırılır/gönderilir (`scripts/llm_note.py`, `src/fintr/llm_note/`; tasarım:
`docs/architecture.md`, karar: `docs/prereg/DECISIONS.md`). Deterministik bildirim
ayrı bir çağrıdır (`--layout v2` ya da `v1` ile; düzen v3'te not değerlendirmesi zaten
tek mesajın satırındadır, yukarıdaki "Sinyal chati" bölümü) ve modeli beklemez. Not modu
`--layout`'u yok sayar: not mesajları kendi biçimindedir ve
yalnız `note_receipt*.json` dosyalarını yazar. Not gönderimi, deterministik bildirimin
düzen v2 yoluyla aynı devam edebilir mekanizmayı kullanır (`src/fintr/notify/delivery.py`;
aşağıdaki "Receipt" maddesi); iki bildirimin receipt'leri birbirinden bağımsızdır
(`receipt*.json` deseni `note_receipt*.json` dosyalarını görmez).
`run_daily_pipeline.py --layout v2 --llm-note` sırası: toplama, tarama, deterministik bildirim,
`llm_note.py`, not bildirimi (varsayılan v3'te sıra farklıdır: not önce, tek mesaj sonra). Not adımının hatası gönderilmiş bildirimi veya çıkış
kodunu değiştirmez; yalnız özette görünür (`note_notify: 0 | already_sent | partial |
content_changed | skipped | failed:exit_N`). Aynı gün yeniden çalıştırmada tamamlanmış not
tekrar gönderilmez (`already_sent`), yarım kalmış notun yalnız gönderilmemiş mesajları
gider (`0`); yarıda kalan bu çalıştırma `partial`, içerik değiştiği için reddedilen
`content_changed` görünür. Pipeline `--resend` geçmez; tam yeniden gönderim elle
`notify_observations.py --llm-note-dir ... --resend` ile yapılır.

```
Opus 5.5 analiz notu — doğrulanmamış hipotez değerlendirmesi, yatırım tavsiyesi değildir
as_of: 2026-09-30
Uyarı: yön etiketi doğrulanmamış ön kayıtlı hipotezdir (holdout testi 2027 Nisan); yatırım tavsiyesi değildir.

AKFIS H2 (beklenen yön: yukarı (al yönlü)) — değerlendirme: destekliyor
Sade anlatım: <2-4 kısa cümle: koşul gündelik dille ne demek, bugünkü sayılar ne söylüyor, hipotez ne varsayıyor ve neden>
• <doğrulanmış gerekçe metni>
Veri notları:
• <veri sınırlamaları>
Durum: preregistered_hypothesis_holdout_test_pending
```

Her notlu hisse kendi mesajıdır (yukarıdaki biçim); en sonda kapanış mesajı gelir:

```
Opus 5.5 analiz notu — doğrulanmamış hipotez değerlendirmesi, yatırım tavsiyesi değildir
as_of: 2026-09-30
GARAN: analiz notu doğrulamadan geçemedi (number_not_in_input).
THYAO: analiz notu alınamadı (usage_limit).
Özet: doğrulanan 1, doğrulamadan geçemeyen 1, alınamayan 1.

Rakamlar ve kanıt yolları girdi paketiyle kodla denetlendi; model çıktısı deterministik değildir; ...
Kayıt: data/interim/llm_notes/2026-09-30
Durum: preregistered_hypothesis_holdout_test_pending
```

- **Hisse başına bir model çağrısı:** her hissenin kendi paketi, kendi doğrulaması ve
  kendi durumu vardır; bir hissenin reddi/hatası diğerlerini etkilemez. Paket hiçbir
  zaman kısaltılmaz: tek hissenin paketi `max_symbol_packet_bytes` (varsayılan 200 000
  bayt) sınırını aşarsa o hisse "alınamadı (packet_too_large)" olur; `max_symbols_per_run`
  (varsayılan 30) aşan hisseler "alınamadı (symbol_limit)" olur; sessizce dışarıda
  bırakılan hisse yoktur. Özet satırı sayıları verir (doğrulanan / doğrulamadan geçemeyen
  / alınamayan).
- **Ne içerir:** her `condition_met` gözlem için dondurulmuş kuralın yönünü veri
  `destekliyor` / `zayıflatıyor` / `belirsiz`, finans bilgisi olmayan biri için sade
  Türkçe anlatım (`plain_explanation`: koşul gündelik dille ne demek, bugünkü sayılar
  ne söylüyor, hipotez ne varsayıyor ve neden; hipotez/gerekçe metni `docs/prereg/H*.md`
  "Hipotez" ve "Gerekçe" bölümlerinden pakete konur) ve kısa, kanıt yollu gerekçeler.
  Değerlendirme dışında yeni finansal ifade eklemez; al/sat emri, hedef, stop,
  pozisyon, getiri tahmini veya puan üretmez. Yön etiketi, deterministik
  mesajdaki aynı iki sabit ifadedir ve `--no-direction-label` ile notta da
  kalkar (uyarı satırı ve etiket birlikte).
- **Doğrulama:** not yalnız kodla doğrulandıysa gösterilir (şema, izinli değer,
  kapsam, kanıt yolları, yasak ifade, yazıyla sayı, rakam/tarih denetimi). Rakam
  denetimi: bir gerekçedeki her rakam/tarih, o gerekçenin kendi `evidence_paths`
  yollarının altındaki değerlerden biri olmalıdır (yol çözülmezse veya 600'den çok
  değeri kapsıyorsa ret); işaret eşleşir (negatif değer yalnız `-x`/`−x` ile; "net
  satış 5" yazıp paketteki `-5`'i kastetmek reddedilir — katı kural, anlam
  okunmaz); `%` için `p%` paketteki `p` ile aynen, `p/100` yalnız `|değer| <= 1` olan
  paket değerleriyle eşleşir. Yuvarlama yalnız yazılan hassasiyette kabul edilir:
  en az bir ondalık yazılmış sayı (`10,46`, `%5,12`), o hassasiyette yuvarlanınca eşit
  olan bir paket değeriyle eşleşir (işaret dahil); tam sayılar aynen eşleşir (`5`,
  `4.6` ile eşleşmez); 4'ten fazla ondalıklı ham değer (`number_format`) reddedilir —
  mesajda 30 haneli ham sayı çıkmaz. `plain_explanation` aynı kurallara tabidir
  (yasak sözcük, yazıyla sayı, rakam); rakamları o gözlemin gerekçelerinin yollarından
  veya kuralın kendi paket girdisinden (`/rules/<kural>`: koşul eşiği, hipotez
  metni) gelmelidir. `data_caveats` yol göstermez: oradaki rakamlar paketteki
  herhangi bir değer olmalıdır. **Bilinen
  sınır:** küçük tam sayılar paketteki başka bir değerle tesadüfen eşleşebilir;
  "bir", "yarım/çeyrek" benzeri sözcükler ve türetilmiş anlatımlar (ör. "artış")
  kodla denetlenemez. Gösterim sırasında kayıt bütünlüğü yeniden denetlenir (paket
  sha256 + yeniden doğrulama); değişmiş bir not gösterilmez.
- **Notu olmayan hisse için kendi satırı:** `<SYM>: analiz notu alınamadı
  (<kategori>).` (model reddi, abonelik limiti, zaman aşımı, CLI hatası, paket çok
  büyük, hisse sınırı), `<SYM>: analiz notu doğrulamadan geçemedi (<kategori>).`
  (reddedilen not; içeriği gönderilmez), `<SYM>: bu as_of için not yok (adım
  çalıştırılmadı).` Hiçbir hisse için not yoksa mesaj `Opus 5.5 analiz notu:
  doğrulanan 0, ...` özetiyle başlar ve hisse satırlarını sıralar; kayıt hiç yoksa
  tek satır `... bu as_of için not yok (adım çalıştırılmadı).`, gözlem yoksa `...
  bu taramada gözlem yok; not üretilmedi.` Reddedilen/eksik not deterministik
  mesajı hiçbir zaman engellemez.
- **Receipt (devam edebilir, şema `fintr_notify_note_receipt_v2`):** not mesajları
  sırayla gider; her çalıştırma `data/interim/notify/<as_of>/` altına create-only
  (`O_EXCL`, hiç yeniden yazılmaz) bir dosya bırakır: oturumu başlatan çalıştırma
  `note_receipt.json` (`--resend` için `note_receipt-<damga>.json`), aynı oturumu
  sürdüren her sonraki çalıştırma `note_receipt.part-NNN.json`. Her dosya o çalıştırma
  sonrası mesajların tamamının SHA-256'sını (`message_sha256`) ve mesaj başına durumu
  (`message_status`: `sent` + Telegram `message_id`, ya da `not_sent`), ayrıca
  `complete`, `failed_at_index`, `error_kind` (yalnız istisna sınıf adı; token/chat id
  hiçbir yere yazılmaz) taşır. Gönderim yarıda kalırsa (ağ/Telegram hatası ya da Ctrl-C)
  o ana kadar gidenler yine kaydedilir; aynı as_of için komut tekrar çalıştırılınca
  **yalnız gönderilmemiş** not mesajları gider (`--resend` gerekmez). Devam, yalnız
  yeni not mesajları oturumunkilerle aynı hash'e ve aynı observations SHA-256'ya
  sahipse yapılır; içerik farklıysa (ör. aynı gün not yeniden üretildi) hiçbir şey
  gönderilmez ve `--resend` istenir (`--resend` baştan tam gönderimdir, yeni oturum
  başlatır). Tamamlanmış oturum varken ikinci gönderim reddedilir ("analiz notu
  receipt zaten var"). **Eski `fintr_notify_note_receipt_v1` dosyaları** devam edilebilir
  oturum sayılmaz; tamamlanmış gönderim gibi davranır: sessizce yeniden gönderim olmaz,
  gönderim reddedilir, yalnız `--resend` yeniden gönderir. Notun gönderimi deterministik
  bildirime, bildirimin gönderimi nota bağlı değildir; deterministik bildirim `receipt.json`
  ailesinde kalır. Not modunda mesajlar arası bekleme yoktur (`--message-delay-seconds`
  verilirse uygulanır). Hiç `condition_met` yoksa not mesajı gönderilmez.
- **Kalan risk (not ve bildirim için aynı):** Telegram isteği zaman aşımına uğrar ama
  mesaj aslında iletilmişse, istemci bunu bilemez; o mesaj `not_sent` kalır ve devam
  çalıştırması tek mesajı bir kez daha gönderir (yalnız o mesaj; tamamlanmış diğerleri
  tekrarlanmaz). Mesaj gönderildikten sonra receipt yazılamazsa (disk hatası) devam
  durumu da yazılamaz; hata mesajı bunu ve gönderilen mesaj sayısını bildirir ve o
  çalıştırmanın mesajları sonraki çalıştırmada tekrar gidebilir.
- **Veri Anthropic'e gider:** ham AKD/takas verisinin üçüncü tarafa gönderimi
  Efe'nin kararıdır; Mini App lisans beyanının bunu kapsadığı doğrulanmadı.

## 1. BotFather ile bot açma

1. Telegram'da `@BotFather` ile sohbet başlat.
2. `/newbot` komutunu gönder; bot için bir görünen ad ve `_bot` ile biten
   benzersiz bir kullanıcı adı iste (ör. `fintr_efe_scanner_bot`).
3. BotFather sana bir **token** verir (`123456789:AA...` biçiminde). Bu
   token'ı kimseyle paylaşma; ekran görüntüsü alma, commit'e ekleme, loga
   yazma. Yalnızca aşağıdaki ortam değişkenine koyacaksın.

## 2. Botla sohbet başlatma ve chat id bulma

1. Telegram'da yeni botunu ara (kullanıcı adıyla) ve sohbeti başlatmak için
   `/start` gönder (ya da herhangi bir mesaj yaz).
2. Tarayıcıda şu adresi aç (TOKEN'ı kendi token'ınla değiştir):
   `https://api.telegram.org/botTOKEN/getUpdates`
3. Dönen JSON'da `result[0].message.chat.id` alanı senin chat id'ndir
   (pozitif veya negatif bir tam sayı). Bunu not al.
4. `getUpdates` boş dönerse, botla az önce mesajlaştığından emin ol ve
   sayfayı yenile.

Bot yalnız bu chat id'ye yazar; başka hiçbir sohbete veya kanala mesaj
göndermez (kod bunu zorlamaz, ama `FINTR_TELEGRAM_CHAT_ID` yalnız senin
chat id'ni içermelidir).

## 3. Ortam değişkenlerini PowerShell'de ayarlama

```powershell
$env:FINTR_TELEGRAM_BOT_TOKEN = "123456789:AA...senin-token'ın..."
$env:FINTR_TELEGRAM_CHAT_ID = "123456789"
```

Bu değişkenler yalnız o PowerShell oturumunda geçerlidir; token hiçbir
dosyaya yazılmaz. Kalıcı yapmak istersen `[Environment]::SetEnvironmentVariable`
kullanabilirsin, ama token'ı bir script dosyasına gömme.

## 4. Önce dry-run, sonra `--send`

Varsayılan davranış **dry-run**: mesaj(lar) yalnız stdout'a basılır, ağ
hiç kullanılmaz. Gerçek gönderim için `--send` açıkça gerekir.

```powershell
$env:PYTHONPATH = "$PWD\src"

# 1) Önce dry-run: mesajı gözden geçir, hiçbir ağ çağrısı yapılmaz.
.\.venv\Scripts\python.exe scripts\notify_observations.py --as-of 2026-09-25

# 2) Sorun yoksa gerçekten gönder.
.\.venv\Scripts\python.exe scripts\notify_observations.py --as-of 2026-09-25 --send

# Bu taramada condition_met=true gözlem yoksa hiçbir şey göndermek istersen:
.\.venv\Scripts\python.exe scripts\notify_observations.py --as-of 2026-09-25 --send --skip-empty

# Aynı as_of için receipt zaten varsa ve yine de tekrar göndermek istersen
# (üzerine yazmaz, yeni bir receipt dosyası oluşturur):
.\.venv\Scripts\python.exe scripts\notify_observations.py --as-of 2026-09-25 --send --resend
```

(Layout v2'nin receipt'i için yukarıdaki "Teslimat" bölümüne bakın; aşağıdaki paragraf `--layout v1` içindir.)

Başarılı bir `--send` sonrası `data/interim/notify/<as_of>/receipt.json`
yazılır: girdi dosyasının SHA-256'sı, gönderilen mesaj metinlerinin
SHA-256'sı, gönderim zamanı ve Telegram `message_id`'leri. Token veya chat
id receipt'e hiçbir zaman yazılmaz. Aynı `as_of` için receipt zaten varsa
ikinci `--send` reddedilir (`--resend` verilmedikçe); receipt dosyasının
üzerine hiçbir zaman yazılmaz.

## Güvenlik notları

- Token asla paylaşılmaz, loglanmaz, receipt'e veya hata mesajına
  yazılmaz; yalnız `FINTR_TELEGRAM_BOT_TOKEN` ortam değişkeninden okunur.
- Bot yalnız `FINTR_TELEGRAM_CHAT_ID` ile belirttiğin chat'e yazar.
- Mesaj düz metin gönderilir (`parse_mode` kullanılmaz; v1 ve v2); Telegram tarafında
  biçimlendirme (kalın, link vb.) uygulanmaz.
- HTTP zaman aşımı 15 saniyedir. Telegram 429 (hız sınırı) döndürürse ve
  `retry_after` 30 saniyeden kısaysa tek bir yeniden deneme yapılır; aksi
  halde bildirim açık bir hatayla durur (siz tekrar çalıştırırsınız).
- Token'ı bir yere yanlışlıkla yapıştırdıysan BotFather'da `/revoke`
  ile iptal edip yeni token alabilirsin.
