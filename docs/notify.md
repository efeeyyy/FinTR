# Telegram gözlem bildirimi (`fintr.notify`)

Aşama 2'nin ikinci adımı: `scripts/scan_observations.py` tarafından
kaydedilmiş gözlem dosyasını (`data/interim/scanner/<as_of>/observations.json`,
şema `fintr_scanner_observations_v1` ya da `_v2` -- `_v2` yalnız tarayıcı
`--daily-root` ile çalıştırıldıysa, bkz. `docs/scanner.md`) okuyup Efe'nin
kendi Telegram botuyla, yalnız Efe'nin sohbetine düz metin bildirim olarak
gönderir. `fintr.notify.format.SUPPORTED_OBSERVATIONS_SCHEMAS` her iki
şemayı da kabul eder; eski (`_v1`, `--daily-root` öncesi ya da onsuz
üretilmiş) dosyalar aynen okunmaya devam eder.

## Mesaj düzeni v2 (varsayılan) ve `--layout v1`

2026-10-03'ten beri varsayılan düzen v2'dir (`src/fintr/notify/layout.py`):
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
▲ ABCDE — beklenen yön: YUKARI (al yönlü)
Kural H2: kapanış, günün ortalama işlem fiyatının (AKD VWAP) en az ~%1 altında
Bugün: kapanış ortalama fiyatın %1,48 altında
Ne demek: Gün içinde lotlar ortalama olarak kapanıştan daha yüksek fiyattan el değiştirdi; ...
Vade: 5 işlem günü · kural sürümü 42fca88a
```

Aşağı yön için `▼` ve `beklenen yön: AŞAĞI (sat yönlü)`. Hiçbir hissede koşul
yoksa tek mesaj: `FinTR · <tarih> (<gün> kapanışı): bugün hiçbir hissede koşul
sağlanmadı` + veri satırı (`--skip-empty` önceki gibi hiçbir şey göndermez).

- **Ham değer yok.** `vwap_proxy_vs_close` ve `top1_akd_net_share_of_volume` ham
  hâliyle mesajda görünmez; ham değer gözlem dosyasında (ve receipt'teki
  `observations_sha256` ile) kalır. Mesajdaki yüzdeler *hesaplanmış* değerlerdir.
- **Yüzde formülü** (kodda `layout.close_vs_vwap_percent`/`share_percent`):

  ```
  v   = vwap_proxy_vs_close = (vwap_proxy - close) / close      (docs/features.md; saklanan ham değer)
  pct = -v / (1 + v) * 100  = (close - vwap_proxy) / vwap_proxy * 100
  ```

  `pct`, kapanışın günün ortalama işlem fiyatından (`vwap_proxy`, AKD türevi
  doğrulanmamış proxy) ne kadar uzak olduğunu, **o ortalama fiyatın yüzdesi**
  olarak verir. `v < 0` ⇒ kapanış ortalama fiyatın **üstünde** (H1, H3 koşulları:
  `v ≤ -0,01`); `v > 0` ⇒ **altında** (H2, H4: `v ≥ 0,01`). "üstünde/altında" kelimesi
  kural kimliğinden değil `v`'nin işaretinden gelir. Örnek (sentetik): `v = 0,015`
  ⇒ "%1,48 altında"; `v = -0,02` ⇒ "%2,04 üstünde". Dondurulmuş eşik `v = 0,01`
  ortalama fiyatın %0,99 altına, `v = -0,01` %1,01 üstüne denk gelir; kural
  satırları bu yüzden "~%1" der. `top1_akd_net_share_of_volume` zaten 0..1 arası
  paydır, `pay * 100` olarak gösterilir ("tek kurum net payı %12,35 (günlük toplama
  oranı)"). Yuvarlama: iki ondalık, yarıdan yukarı, Türkçe virgül. Okunamayan
  değer (yok, sayı değil, `1 + v ≤ 0`, pay 0..1 dışı) tahmin edilmez:
  "okunamadı" yazılır.
- **Veri durumu toplanır.** Sembol başına `composition` sayıları (`boşluk`,
  `kesinleşmemiş gün`, `revize gün`; pencere-sınırlı, bkz. aşağıdaki v1 bölümü)
  aynıysa tek satır ("20 hissenin hepsi tam (boşluk 0) · 2 gün kesinleşmemiş");
  değilse en büyük grup `Veri: 19/20 hisse ...` satırında sayılır ve farklı her
  grup `Farklı: SYM1, SYM2 — eksik (boşluk 1) · ...` satırında listelenir. `tam` =
  pencerede boşluk yok. Revize gün yalnız sıfırdan büyükse yazılır. `composition`
  yoksa ya da pencere yoksa bunu söyler. Eksik tazelik alanı, bastırılan ve
  değerlendirilemeyen gözlem satırları yalnız doluysa eklenir; T+2 takas satırı v2'de
  yoktur (H1–H4 takas kullanmaz; bilgi gözlem dosyasında durur).
- **Uyarı yalnız özet mesajında, bir kez.** Hisse mesajlarında ayrı uyarı/dipnot
  yoktur; bunun yerine "Ne demek" cümlesi her seferinde "Ön kayıtlı, henüz
  doğrulanmamış hipotez:" ibaresini taşır. "Kural sürümü" = `rules_file_sha256`
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

`--layout {v1,v2}` (varsayılan v2), `--rule-explanations PATH`, `--delivery-config PATH`,
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
