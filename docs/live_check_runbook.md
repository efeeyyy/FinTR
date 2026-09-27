# Canlı kontrol çalışma kılavuzu

Hazırlık tarihi: 2026-09-26 (Europe/Istanbul). Bu sayfa, kısa bir canlı
`scripts/live_collector_smoke.py` denemesinden önce/sırasında/sonrasında
izlenecek somut adımları toplar. Kendisi canlı bağlantı kurmaz, sağlayıcı
veri doğruluğu iddia etmez ve [collector.md](collector.md) ile
[protocol_inventory.md](protocol_inventory.md) belgelerinin yerine geçmez;
onları tamamlar.

## 1. Bağlanmadan önce

1. **Telegram Mini App'i kapatın.** Sağlayıcı aynı hesap/`initData` için tek
   aktif WebSocket oturumuna izin verir; collector bağlantısı açık Mini App
   oturumunu kapatabilir ([collector.md](collector.md#live-smoke)).
2. **Offline testleri çalıştırın:**

   ```powershell
   $env:PYTHONPATH = "$PWD\src"
   .\.venv\Scripts\python.exe -m unittest discover -s tests
   ```

3. **Her planlanan komutun `--dry-run`'ını çalıştırın.** `--dry-run`,
   `--allow-session-takeover` istemez, `initData` sormaz, kimlik doğrulamalı
   URL kurmaz ve bağlantı açmaz; yalnız argüman doğrulamasını (gerçek
   çalıştırmayla aynı hata mesajlarıyla) çalıştırıp planlanan istekleri
   (`CollectorRequest.to_json()` ile tam payload), beklenen response'ları,
   frame/timeout sınırlarını ve (varsa) kalıcı kayıt hedefinin var olup
   olmadığını yazar; sonda "Dry run: no connection opened" basar. Aşağıdaki
   2. bölümdeki her komutu önce aynı flag'lerle `--dry-run` ekleyerek
   çalıştırın; hedef dosya zaten varsa gerçek çalıştırma "already exists"
   ile başarısız olacaktır — bunu dry-run çıktısında görüp tarih aralığını
   veya dosyayı önceden düzeltin.

## 2. Planlanan komutlar

### 2a. AKFIS dönem yakalama v2

Hedef: `--save-period-analysis-context` ile `get_brokerages`, `get_akd`,
`custody_analysis`, `get_custodian_diff` isteklerinin dördünü de tek
bağlantıda doğrulayıp `data/interim/period_analysis/AKFIS/2026-07-01_2026-09-29.json`
altına kaydetmek. Efe 2026-09-26'da doğrudan veri analizi için son üç ayın
çekilmesini seçti: `2026-07-01..2026-09-29` 90 gündür, `--allow-large-range`
gerekmez. Mevcut `2026-09-08_2026-09-25` kaydı farklı dosya adıdır; üzerine
yazma çakışması beklenmez — yine de çalıştırmadan önce `--dry-run` ile
doğrulayın.

**Gelecek tarih koruması (2026-09-26'da eklendi):** `--end-date`/`--to-date`/
`--custody-date` bugünün yerel tarihinden ileriyse script, `--dry-run` dahil,
initData istemeden veya bağlanmadan `parser.error` ile durur (exit code 2).
Bu koruma, aşağıdaki `2026-07-01..2026-09-29` komutunun **29 Eylül takası
kesinleşene kadar (2026-09-30'dan önce) her denemesini reddeder** — Run A'da
gözlenen `event=akd_data; field=event.data.data.source; expected=present;
actual=missing_or_null` hatasının kök nedeni budur (sağlayıcının o tarih için
henüz verisi yoktu). Bitiş tarihi tam bugünse (`--end-date`/`--to-date`/
`--custody-date` == bugün) komut çalışır ama "Note: same-day data (...) may be
incomplete or not yet available from the provider." notu basılır; bu gün
sonu verisi eksik olabilir.

**Mesaj boyutu:** Varsayılan WebSocket mesaj sınırı 1 MiB'dir. 14 işlem
günlük `custody_analysis` verisi kayıtlı pakette ~133 KB tuttu (günde
~9,5 KB); üç ayda ~63 işlem günü ~600 KB, sağlayıcı biçimiyle daha fazla
olabilir. Bu tahmin sağlayıcının gerçek boyutunu kanıtlamaz; payı açmak için
`--max-message-mib 4` kullanılır (izinli aralık 1–8). Tek büyük yanıt nedeniyle
receive süresi de `--timeout 15` olarak yükseltildi.

**Takvim notu (yönetici tarafından Borsa İstanbul'un resmi 2026 Pay Piyasası
tatil takviminden doğrulandı):** 30 Ağustos–28 Ekim 2026 arasında piyasa
tatili yok; 24 Eylül 2026 (Perşembe) işlemleri 28 Eylül'de, 25 Eylül 2026
(Cuma) işlemleri T+2 ile 29 Eylül'de takasa düşer. Bu nedenle komutu **29
Eylül takasının kesinleştiği 30 Eylül 2026 veya sonrasında** çalıştırmanız
önerilir. Bu yalnız takas takvimi hakkındadır; **sağlayıcının bu dönem için
gerçekte hangi tarihlere kadar veri sunduğu hâlâ doğrulanmamıştır** — dry-run
veya offline test bunu kanıtlamaz, yalnız canlı deneme gösterir.

**`--timeout`/`--max-frames` seçimi:** Dört istek + en az bir üst düzey
`welcome` = deneme başına en az 5 frame gerekir. Script varsayılanı 8 frame /
5 saniyedir (kod tavanı: frame için 50, timeout için 30 saniye;
[collector.md](collector.md) içindeki `--max-frames` bölümü). Geçmiş canlı
denemelerde beklenen yanıt gelmediğinde araya çok sayıda `hb` sıkışabildiği
gözlenmiştir (bkz. collector.md "Önceki smoke": 35 frame sınırında 1 `welcome`
+ 34 `hb`, hiç `brokerages_data` yok). Ayrıca Run B'de (bkz. giriş) dört
beklenen yanıtın **her biri** kendi `[UNKNOWN_EVENT_TYPE]` çift frame'iyle
geldi (`akd_data` → unknown, `custody_analysis_data` → unknown, vb.). Bu
yüzden çok-yanıtlı dönem yakalamalarında (2a gibi) **`--max-frames 16`**
önerilir — dört yanıt + dört olası eşlik eden unknown event + birkaç `hb`
+ `welcome` payı. **`--timeout 8`** aynı kalır. En kötü senaryoda (her frame
tam timeout sınırında gelirse) bu, deneme başına en fazla ~128 saniyelik
bekleme demektir; sabit 2 retry ile toplam üst sınır birkaç dakikayı geçmez.
Bu yalnız bounded bekleme bütçesidir, sağlayıcının gerçekte bu kadar sürede
yanıt vereceğinin kanıtı değildir. **Erken durdurma (2026-09-26'da eklendi):**
Collector artık dört beklenen yanıtın tümü bilinen/normalize/pending-olmayan
ve (yerel provenance bağlayan aileler için) provenance'lı hâle geldiği anda
denemeyi bitirip bağlantıyı kapatır; Run B'de gözlenen "üç fazla `hb`
beklenmesi" artık olmaz. Bu yalnız script tarafının bağlantıyı ne zaman
kapattığını değiştirir — `--max-frames`/`--timeout` üst sınırları hâlâ
geçerlidir ve hiçbir yanıt gelmezse aynı şekilde tüketilir.

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py `
  --dry-run `
  --symbol AKFIS `
  --start-date 2026-07-01 `
  --end-date 2026-09-29 `
  --save-period-analysis-context `
  --allow-session-takeover `
  --timeout 15 --max-frames 16 --max-message-mib 4
```

**Not (2026-09-26 itibarıyla):** Bu komut bugün çalıştırılırsa yeni gelecek
tarih koruması onu **reddeder** — `--dry-run` dahil, hiçbir initData istemi
veya bağlantı olmadan:

```text
live_collector_smoke.py: error: --end-date 2026-09-29 is after today
(2026-09-26); the provider has no data for future dates yet (a live run
previously failed with a missing akd_data event.data.data.source field for
a future --end-date)
```

Bu, yukarıdaki takvim notuyla tutarlıdır: komutu **29 Eylül takası kesinleşen
2026-09-30 veya sonrasında** çalıştırın; o tarihten önce hem takvim notu hem
de bu kod-seviyesi koruma aynı komutu engeller.

Dry-run `Persistence target does not exist yet` yazdırdıktan sonra, Mini
App'i kapatıp aynı komutu `--dry-run` olmadan çalıştırın:

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py `
  --symbol AKFIS `
  --start-date 2026-07-01 `
  --end-date 2026-09-29 `
  --save-period-analysis-context `
  --allow-session-takeover `
  --timeout 15 --max-frames 16 --max-message-mib 4
```

**AKFIS T+2 takip komutu (2026-09-30 veya sonrasında çalıştırın):** Yukarıdaki
komutla aynıdır; yalnız gelecek tarih koruması geçtikten sonra tekrar
listelenmiştir — bu komut, 29 Eylül takasının T+2 ile kesinleştiği güne kadar
her denemede reddedilecektir:

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py `
  --symbol AKFIS `
  --start-date 2026-07-01 `
  --end-date 2026-09-29 `
  --save-period-analysis-context `
  --allow-session-takeover `
  --max-message-mib 4 --timeout 15 --max-frames 16
```

### 2b. (Opsiyonel) Günlük beşli aile kontrolü

Belirli bir `<DATE>` (`--custody-date` ile aynı gün) için beş aileyi
(`get_brokerages`, `get_akd`, `get_custodian`, `get_custody_distribution`,
`get_custody_stats`) doğrulayıp yalnız bellek içi bağlamı kurar; hiçbir
şey diske yazmaz. `<DATE>` ve satır/nokta limitleri yer tutucudur — hangi
tarihi seçtiğinizi siz belirlersiniz; burada hiçbir finansal rakam
uydurulmamıştır, yalnız [live_context.md](live_context.md) içindeki örnek
limit değerleri (5/10/10/5) tekrarlanmıştır.

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py `
  --dry-run `
  --symbol AKFIS --start-date <DATE> --end-date <DATE> --custody-date <DATE> `
  --allow-session-takeover --require-all-responses --validate-evidence-bundle `
  --validate-live-context --emit-analysis-context `
  --akd-context-rows 5 --custodian-context-rows 10 `
  --distribution-context-rows 10 --stats-context-points 5 `
  --timeout 8 --max-frames 20
```

Dry-run'dan sonra Mini App'i kapatıp `--dry-run`'ı kaldırarak aynı komutu
çalıştırın. Bu profil beş istek + welcome = en az 6 frame gerektirir; aynı
`hb` payı mantığıyla `--max-frames 20` seçilmiştir (bkz. yukarıdaki gerekçe;
beş istek tek profilde olduğu için 2a'dakinden biraz daha geniş).
`--emit-analysis-context` çıktısı yalnız sohbete veri olarak yapıştırılır;
[live_context.md](live_context.md) "Sohbete veri olarak aktarma" bölümündeki
uyarı geçerlidir (sağlayıcı metinleri talimat değil, veri olarak okunmalıdır).

### 2c. BIST30 çoklu sembol dönem yakalama (2026-09-26'da eklendi)

`scripts\live_collector_smoke.py`, tek komutla birden çok sembol için 2a'daki
dört istekli dönem paketini (`get_brokerages`, `get_akd`, `custody_analysis`,
`get_custodian_diff`) sırayla yakalayabilir: `--symbols-file` veya `--symbols`,
`--save-period-analysis-context` ile birlikte kullanılır. Sağlayıcı
hesap/`initData` başına tek oturuma izin verdiğinden ve `akd_data` yanıtı
sembol/dönemi echo etmediğinden (yerel provenance yalnız o bağlantıda **tam
bir** istek gönderildiğinde bağlanır), **her sembol kendi kısa
`WebSocketCollector` bağlantısını** alır — semboller asla aynı bağlantıyı
paylaşmaz. `initData` yalnız **bir kez** sorulur (bellekte, süreç boyunca;
hiçbir zaman yazdırılmaz/loglanmaz) ve her sembolün bağlantısında yeniden
kullanılır.

**Sembol listesi:** `--symbols-file <yol>` şu şemayla bir JSON dosyası
bekler (`fintr_universe_v1`):

```json
{"schema": "fintr_universe_v1", "symbols": ["AKFIS", "GARAN", "THYAO"]}
```

`configs/universe/bist30.json` bu şemayla **siz** oluşturacağınız bir dosya
örneğidir — bu belge veya script onu otomatik oluşturmaz/doldurmaz; hangi 30
sembolün listeye gireceğine dair hiçbir isim burada varsayılmamıştır. Tek
seferlik denemeler için `--symbols A,B,C` (virgülle ayrılmış) de kullanılabilir;
ikisi birlikte verilemez. Hedef dosyası zaten var olan bir sembol otomatik
**`skipped_existing`** olarak atlanır (bağlantı açılmaz); bu, mevcut
"no overwrite" davranışının çoklu-sembol karşılığıdır. Bir sembol
oturum/kimlik doğrulama düzeyinde art arda **2** kez "hiçbir istek
gönderilemedi" (bağlantı `welcome`'dan önce kapandı) desenini gösterirse
script kalan sembolleri denemeden durur ve nedenini yazdırır — bu, tekil
sembol hatalarının (örn. bir sembolün paket doğrulaması başarısız olması)
kalan sembolleri etkilememesinden **farklıdır**: yalnız oturum/kimlik
doğrulama düzeyindeki tekrarlanan başarısızlık deseni tüm çalıştırmayı
durdurur. Sembol sayısı sabit bir üst sınıra (`MAX_SYMBOLS_PER_RUN = 40`)
tabidir ve `--max-wall-seconds` (varsayılan 1800s / 30 dakika) toplam
çalışma süresini sınırlar; sınır aşılırsa kalan semboller denenmeden
**`skipped_wall_time_guard`** olarak işaretlenir. Semboller arasında
`--symbol-pause-seconds` (varsayılan 3, üst sınır 30) kadar sabit bir
bekleme vardır.

**Holdout ve dışlama (2026-09-27'de eklendi):** Dondurulmuş BIST 30
ayrımının (`configs/splits/current.json`) holdout hisseleri toplamada
**engellenmez** — veri toplanabilir; yalnız analiz/çalışma araçları onları
reddeder (bkz. [evaluation_protocol.md](evaluation_protocol.md)).
`configs/exclusions/symbols.json`'daki hisseler ise geçersiz kılma seçeneği
olmadan toplanmaz: `--symbol` ile tek hisse modunda script initData
istemeden ve bağlantı açmadan hata verir; `--symbols`/`--symbols-file`
çoklu modunda (dry-run dahil) o hisse istek gönderilmeden
**`skipped_excluded`** olarak atlanır ve özet tabloda görünür. Listedeki
tüm hisseler dışlanmışsa initData hiç sorulmaz. Aynı kural
`fetch_price_history.py` (red) ve `fetch_price_history_batch.py`
(`skipped_excluded`) için de geçerlidir. Dışlama kaydındaki `retained_data`
notu yalnız bilgi amaçlıdır; toplamaya istisna açmaz.

**Örnek çalıştırma — 2026-06-29..2026-09-25 aralığı, BIST30 evreni:**

Önce dry-run (initData istemez, bağlantı açmaz; her sembol için planlanan
istek özeti + hedef dosya yolu + var/yok bilgisini yazdırır):

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py `
  --dry-run `
  --symbols-file configs\universe\bist30.json `
  --start-date 2026-06-29 `
  --end-date 2026-09-25 `
  --save-period-analysis-context `
  --allow-session-takeover `
  --max-frames 16 --timeout 15 --max-message-mib 4 --show-unknown-event-types `
  --max-wall-seconds 5400
```

Dry-run çıktısında her sembol için `exists=False`/`exists=True` ve hedef
yolu görüldükten sonra, Mini App'i kapatıp `--dry-run`'ı kaldırarak aynı
komutu çalıştırın:

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py `
  --symbols-file configs\universe\bist30.json `
  --start-date 2026-06-29 `
  --end-date 2026-09-25 `
  --save-period-analysis-context `
  --allow-session-takeover `
  --max-frames 16 --timeout 15 --max-message-mib 4 --show-unknown-event-types `
  --max-wall-seconds 5400
```

`--max-frames 16`/`--timeout 15`/`--max-message-mib 4` seçimi 2a'daki aynı
gerekçeye dayanır (dört istek + `welcome` + olası `[UNKNOWN_EVENT_TYPE]`
eşlik eden frame'ler + `hb` payı; büyük `custody_analysis` yanıtı için
yükseltilmiş mesaj sınırı). `--show-unknown-event-types` yalnız bölüm
4a'daki katı `^[a-z][a-z_]{0,39}$` deseniyle eşleşen event **adlarını**
gösterir; payload değerlerini asla basmaz.

**Beklenen süre (kanıtlanmamış üst sınır, gerçek gözlem değil):** Bu
flag'lerle bir sembolün en kötü senaryo üst sınırı `--timeout × --max-frames`
= 15 × 16 = 240 saniye/deneme; sabit 2 retry ile (en fazla 3 deneme) bir
sembol için teorik tavan ~720 saniyedir. 30 sembollük tam bir BIST30
evreninde, bu tavan + `--symbol-pause-seconds` (varsayılan 3s) teorik olarak
birkaç saate kadar çıkabilir — **bu gerçek sağlayıcı davranışının kanıtı
değildir**, yalnız bounded bekleme bütçesidir (2a'daki aynı uyarı). 2a'daki
tek-sembol canlı denemeler bu tavanın çok altında tamamlanmıştı, ama BIST30
ölçeğinde gerçek toplam süre henüz ölçülmemiştir — bu yüzden yukarıdaki
örnek komut varsayılan `--max-wall-seconds 1800`'ü **1800'den 5400 saniyeye
(1.5 saat) yükseltir**; aksi halde 30 sembollük bir çalıştırma varsayılan
30 dakikalık bütçeyle erken durabilir (`skipped_wall_time_guard`). Kendi
evreninizin boyutuna göre bu değeri (60–21600 saniye aralığında) ayarlayın.

**Yakalamadan sonra `dev_registry.json` ve eval split etkisi (önemli —
analizden ÖNCE karar verin):** Aşağıdaki bölüm 3'teki 2. adımda olduğu gibi,
gerçekten development
(kural/prompt geliştirme) amacıyla kullanılan her sembol
`src/fintr/evaluation/dev_registry.json`'a eklenmelidir — ama yalnız
**gerçekten development için kullanılanlar**, yakalanan her sembol otomatik
değil. `leakage_guard.py` sembol kontrolü **şirket düzeyindedir**: registry'ye
eklenen her sembol, tarihi ne olursa olsun, kalıcı olarak eval'den (held-out
değerlendirmeden) çıkar (`development_symbol` nedeniyle reddedilir; bkz.
[evaluation_protocol.md](evaluation_protocol.md), AKFIS örneği bölüm 3.2'de).
Bu nedenle **30 sembolün tamamını yakalayıp analiz ettikten sonra** hangi
sembollerin held-out eval için ayrılacağına karar vermek zaten çok geç
olabilir — bir sembole herhangi bir kural geliştirme/prompt iyileştirme
amacıyla bakmak, onu registry'ye eklemeden bile fiilen "development'ta
kullanılmış" hale getirir. Bu belge hangi sembollerin ayrılacağını **seçmez**
(bu bir ürün/değerlendirme kararıdır) — yalnız şunu önerir: **evrenin bir
alt kümesini (örn. BIST30'un bir bölümünü), üzerinde hiçbir analiz veya
kural geliştirme yapmadan önce, held-out eval için ayırdığınızı yazılı olarak
kaydedin** (ör. bir not/ADR'de) ve yalnız geri kalanı üzerinde development
çalışması yapın. Yakalamanın kendisi (dosyayı diske kaydetmek) sembolü
otomatik olarak eval'den çıkarmaz — onu development registry'sine eklemek
veya üzerinde development amacıyla çalışmak çıkarır; ama karışıklığı önlemek
için ayrımı yakalamadan önce, veri elinizdeyken netleştirmeniz önerilir.

## 3. Yakalamadan sonra

1. **Offline raporu üretin:**

   ```powershell
   $env:PYTHONPATH = "$PWD\src"
   .\.venv\Scripts\python.exe scripts\analyze_period.py --all --save
   ```

   Bu, `data/interim/period_analysis` altındaki her `<SYM>/<start>_<end>.json`
   kaydını okuyup yanında `.report.json` üretir (varsa üzerine yazmaz) ve
   `data/interim/period_analysis/index.json` dosyasını günceller.

2. **Yeni yakalamayı `dev_registry.json`'a development materyali olarak
   ekleyin.** `src/fintr/evaluation/dev_registry.json` içindeki mevcut
   `items` dizisine, var olan `akfis_period_capture_2026-09-08_2026-09-25`
   girdisiyle aynı biçimde şu girdiyi ekleyin:

   ```json
   {
     "item_id": "akfis_period_capture_2026-07-01_2026-09-29",
     "symbol": "AKFIS",
     "start": "2026-07-01",
     "end": "2026-09-29",
     "source_path": "data/interim/period_analysis/AKFIS/2026-07-01_2026-09-29.json",
     "classification": "prompt_output_development_period_capture"
   }
   ```

   Bu kayıt yalnız **development/prompt-output** materyali içindir; held-out
   eval, training veya gold vaka değildir. `src/fintr/evaluation/leakage_guard.py`
   sembol kontrolünü şirket seviyesinde yapar: registry'de zaten AKFIS
   pilotu ve `2026-09-08..2026-09-25` period capture'ı kayıtlı olduğundan,
   **AKFIS sembolündeki her vaka — tarihi ne olursa olsun — `development_symbol`
   nedeniyle reddedilir** (bkz. [evaluation_protocol.md](evaluation_protocol.md)).
   Yani bu yeni kayıt AKFIS'i eval'e daha da kapatır, açmaz: **AKFIS bu
   registry değişmeden hiçbir zaman eval vakası olamaz.**

3. **Beklenen kontroller (rakam uydurmadan, yalnız raporun kendi çıktısına
   bakarak):**
   - `lagged_akd_custody_match.akd_dates_without_later_custody` (gecikme 2
     için "henüz takas karşılığı olmayan AKD günleri" listesi, `analyze_period.py`
     çıktısında `detail_lag=2` varsayılanıyla) — dönem `2026-09-25`'ten
     `2026-09-29`'a uzadığı için, ortak tarih aralığındaki bekleyen (pending)
     günlerin bu yeni raporda **küçülmesi** beklenir; yeni pencerenin son
     birkaç günü kendi içinde hâlâ bekleyen kalabilir. Küçülmezse veya
     büyürse, iki raporu yan yana karşılaştırıp nedenini araştırın —
     otomatik olarak "düzeldi" varsaymayın.
   - Kurum etiketleri: rapor satırlarında `label.status` alanı
     `"resolved"` (kod, dizindeki `short_title` ile eşleşti) veya
     `"unresolved"`/eksik olabilir (bkz. `scripts/analyze_period.py`
     `_coded()`). Yeni yakalamada hangi kodların çözüldüğünü/çözülmediğini
     eskisiyle karşılaştırın; bu yalnızca tarihsiz, tam kod eşleşmesiyle
     kabul edilmiş bir dizin karşılaştırmasıdır — kurum adının o tarihte
     geçerli olduğunu kanıtlamaz.

## 4. Mini App protokol gözlem kontrol listesi (DevTools)

Mini App'i (kapatmadan önce, ayrı bir gözlem oturumunda) tarayıcı/Telegram
DevTools'ta `Network > WS Messages` ile izlerken, [protocol_inventory.md](protocol_inventory.md)
ile aynı terimleri kullanarak yalnız şunları kaydedin:

**Kaydedin:**
- Outbound mesajın `"type"` adı (örn. `get_akd`) ve `data` nesnesinin
  **alan adları** (örn. `company`, `start`, `end` — değerleri değil).
- Inbound mesajın `event.data.type`/üst düzey `"type"` adı (örn. `akd_data`).
- `event`, `event.data`, `event.data.data` seviyelerindeki **üst düzey key
  adları ve JSON tipleri** (`string`/`number`/`object`/`array`/`null`;
  protocol_inventory.md'deki gibi "alan: tip" biçiminde).

**Asla kaydetmeyin veya sohbete/dosyaya yapıştırmayın:**
- `initData`, `query_id`, `hash`, `auth_date` ve bunların geçtiği tüm URL
  sorgu dizeleri (query string'li tam URL'ler dahil).
- Çerezler (cookies), oturum/kullanıcı kimlikleri.
- Alan **değerleri** — kurum adı/kodu, sayısal AKD/Takas rakamı, tarih
  değeri, `retail_pct`/`institutional_pct` gibi ham sayılar. Yalnız şema
  (alan adı + tip) kayda değer; içerik değil.

Bu gözlemler doğrudan [protocol_inventory.md](protocol_inventory.md)'ye
işlenir (ilgili "İşlem" satırına veya "Gözlenen response yapıları"
bölümüne, aynı kanıt etiketleriyle — WS/JS/REPO). Yeni bir alan veya event
tipi görürseniz, **canlı veriye karşı hemen parser değiştirmeyin**: önce
`tests/fixtures/market/` altında gerçek değer içermeyen, sanitize edilmiş
bir fixture (sentetik kod/sayı/tarih) hazırlanmalı ve offline testler o
fixture'a karşı yazılmalıdır. Bu kılavuz o parser değişikliğini yapmaz;
yalnız hangi gözlemin nereye ve nasıl (şema mı, değer mi) kaydedileceğini
tanımlar.

### 4a. `--show-unknown-event-types` ile protokol gözlemi (2026-09-26'da eklendi)

DevTools yerine (veya ona ek olarak) `scripts/live_collector_smoke.py`'nin
kendi çıktısından bilinmeyen event tiplerinin **adını** görmek için
`--show-unknown-event-types` ekleyin. Varsayılan davranış değişmez: bu flag
olmadan her bilinmeyen event `[UNKNOWN_EVENT_TYPE]` olarak basılır (2026-09-25
tarihli güvenlik kararı — sağlayıcıdan gelen keyfi metin terminale
yazdırılmaz). Flag verildiğinde bile bir isim yalnız `^[a-z][a-z_]{0,39}$`
ile **tam eşleşirse** (1–40 küçük harf/alt çizgi, rakam veya büyük harf yok)
gösterilir; rakam, büyük harf, URL benzeri veya secret benzeri hiçbir metin
(`query_id=...`, `https://...` vb.) bu koşulu geçemez ve yine
`[UNKNOWN_EVENT_TYPE]` basılır. Bu yalnız event **tip adı** için geçerlidir;
payload alan adları/değerleri bu flag ile de asla basılmaz. Dispatcher/client
bu flag için yeni bir log satırı eklemez.

```powershell
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py --allow-session-takeover --show-unknown-event-types
```

Görülen bir isim `subscribe_ack` gibi tanıdık bir kontrol frame'iyse ve
tekrarlanan gözlemlerde kararlıysa, bölüm 4'teki gibi
[protocol_inventory.md](protocol_inventory.md)'ye işleyin; script'in kendisi
bunu otomatik kaydetmez.

## 5. Hata durumunda ne anlama gelir

`scripts/live_collector_smoke.py` çıktısındaki sabit hata metinleri
(kaynak: script'in kendisi; parantez içi satır numaraları bu belgenin
yazıldığı sürüme aittir):

| Mesaj | Anlamı | Ne yapın |
|---|---|---|
| `--end-date/--to-date/--custody-date <tarih> is after today (<bugün>); the provider has no data for future dates yet (...)` | Gelecek tarih koruması (2026-09-26'da eklendi): belirtilen tarih yerel bugünden ileri. `--dry-run` dahil her modda, initData istemeden/bağlanmadan `parser.error` ile durur (exit code 2). | Tarihi bugüne veya öncesine çekin ya da takas/veri kesinleşene kadar bekleyin (bkz. bölüm 2a takvim notu). Tarih tam bugünse komut çalışır ama "Note: same-day data (...) may be incomplete" notu basılır. |
| `<takeover uyarısı> Close the Mini App first, then rerun with --allow-session-takeover.` | `--allow-session-takeover` verilmedi; `--dry-run` değilse script bağlanmadan (exit code 2) durur. | Mini App'i kapatıp flag'i ekleyin. `--dry-run` bu kontrolden muaftır. |
| `Smoke failed: {redakte edilmiş bağlantı hatası}` | Bounded retry (en fazla 2 deneme) sonunda `connect`/`receive`/`decode`/`parse`/`send` aşamalarından biri kalıcı olarak başarısız oldu; ham exception/response metni yok, yalnız aşama ve sabit kategori. | Yazdırılan `Attempt N: failure at <stage> (<category>)` satırına bakın; `transport_error`/`transport_closed` ise ağ/oturum sorunudur (Mini App gerçekten kapalı mı, oturum çakışması mı), `internal_error`/`schema_error` ise kod tarafında araştırma gerekir. |
| `No request send completed in any attempt.` | Hiçbir istek gönderilemedi (ör. `welcome` hiç gelmedi). | `send` satırlarında `not sent: no top-level welcome` var mı kontrol edin; sağlayıcı taraf welcome göndermiyor olabilir. |
| `Smoke failed: no event received before the bounded timeout.` | Bağlantı açıldı ama `--max-frames`/`--timeout` sınırı içinde hiç frame gelmedi. | Timeout/max-frames'i (kod tavanları içinde) artırmayı düşünün; sağlayıcının o an event yayınlamıyor olması da mümkündür. |
| `Smoke failed: requested event parsed, but safe local send context is unavailable.` | Beklenen event tipi geldi ve tanınmış ama bu bağlantıda **tam olarak bir kez gerçekten gönderilmiş** ilgili isteğe bağlanamadı (yerel provenance eksik/çoklu). | Aynı istek tipini birden fazla göndermediğinizden emin olun; tekrar deneyin. |
| `Smoke failed: no requested response normalized within collection limits.` | Beklenen response tiplerinden hiçbiri tam normalize olmadı (bilinmeyen event veya eksik alan olabilir). | `Event: ... (unknown)` / `normalization pending` satırlarına bakın; yeni bir şema varyantıysa bölüm 4'teki gibi kaydedip fixture hazırlayın. |
| `Smoke failed: requests not sent: ...` / `responses not received: ...` | `--require-all-responses` ile, planlanan isteklerden biri bu denemede **gönderilmedi** ya da beklenen yanıtlardan biri **normalize olmadı**. | Adı geçen istek/yanıtı tek başına yeniden deneyin (`--*-only` modları); rate limiter/dedup'a takılmış olabilir. |
| `Smoke failed: in-memory evidence view: {redakte edilmiş neden}` | `--validate-evidence-bundle` beşli kanıt görünümünü kuramadı (ör. sembol/tarih uyuşmazlığı). | Neden metnini okuyun (ör. `custodian_date_mismatch`); istenen tarihle sunucu tarihi farklıysa bu beklenen bir sağlayıcı davranışı olabilir. |
| `Smoke failed: in-memory LLM context validation` | `--validate-live-context` bağlam/prompt kurulumu başarısız oldu; payload yazdırılmaz. | Kod/şema uyuşmazlığı olabilir; yeniden çalıştırıp tekrarlanıyorsa araştırın. |
| `Smoke failed: safe analysis context export validation` | `--emit-analysis-context` çıktısı secret-tarama veya serileştirmeden geçemedi; JSON bloğu hiç basılmaz. | Kurum adı gibi bir alanda URL/`initData` benzeri metin olabilir; ham veriyi paylaşmayın, bölüm 4'e göre bildirin. |
| `Pilot context capture failed: {sabit neden}` | `--capture-pilot-context` reddedildi (`pilot_scope_mismatch`, `invalid_context`, `capture_already_exists`, `pilot_capture_directory_unavailable`, `capture_write_failed` vb.). | `capture_already_exists` ise dosya zaten var, üzerine yazılmaz; diğerlerinde kapsam (yalnız AKFIS/2026-09-23, beş aile) tam eşleşmiyor olabilir. |
| `Smoke failed: period-analysis handoff validation ({sabit neden})` | Dört dönem isteğinden biri eksik/çift geldi, normalize olmadı, istek profili beklenenle eşleşmedi veya sunucunun echo ettiği sembol/tarih yerel istekle uyuşmadı. | Neden metnine göre: `not_all_requests_sent` ise tekrar deneyin; `*_server_echo_mismatch` ise yazdırılan `requested_range`/`server_reported_range` karşılaştırmasına bakın — bu sağlayıcı tarafı bir gecikme/farklılık olabilir, hata değil. |
| `Smoke failed: period-analysis capture already exists (no overwrite)` | `--save-period-analysis-context` hedefi (`data/interim/period_analysis/<SYM>/<start>_<end>.json`) zaten var. | Dosyayı silmeyin/üzerine yazmayın; farklı bir tarih aralığı kullanın veya `--dry-run` ile önceden kontrol edin (bölüm 1.3). |
| `Smoke failed: period-analysis capture could not be safely saved` | Paket geçersiz kapsamlı, dizin kullanılamıyor veya secret-tarama/serileştirme başarısız oldu. | Ham hata metni yazdırılmaz; sembol/tarih formatını ve dizin izinlerini kontrol edin. |
| `Smoke failed: safe period-analysis export validation` | Dönem paketi secret-tarama veya serileştirmeden geçemedi (ör. `source` alanında URL benzeri metin). | JSON hiç basılmaz/kaydedilmez; hangi alanın tetiklediğini ham veriyi paylaşmadan araştırın. |

Kontrol frame'lerinin (`welcome`, `hb`) yalnız event adı gösterilir; payload
şemaları yorumlanmaz. `Attempt N: <type> - <label>` satırındaki `send
completed`, yalnız yerel `websocket.send()` çağrısının başarıyla döndüğü
anlamına gelir — sunucunun isteği kabul ettiğinin kanıtı değildir.
