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

## 6. Günlük çalıştırma (`collect_daily.py`)

Eklendi: 2026-09-28 (ADR [daily_miniapp_collection_adr.md](research/daily_miniapp_collection_adr.md)
"Uygulama sırası" madde 2, Aşama A — K5: Efe'nin Windows makinesi,
T+1 08:00–09:30 Europe/Istanbul). Bu bölüm 2a'daki tek seferlik AKFIS dönem
yakalamasının yerine geçmez; ayrı ve **her işlem günü** çalıştırılan bir
komuttur: yalnız etkin ayrımın 20 geliştirme hissesini, kayan (S=E-14g,
E=R-1g) bir pencerede toplar. Ayrıntılı tasarım için
[collector.md](collector.md#günlük-artımlı-toplama-collect_dailypy).

### 6a. Efe'nin her sabah yapacakları

1. **Sabah: önce `check_env.py`** (ağ kullanmaz, hiçbir şey göndermez, hiçbir
   şey yazmaz; ayrıntı 6j):

   ```powershell
   $env:PYTHONPATH = "$PWD\src"
   .\.venv\Scripts\python.exe scripts\check_env.py
   ```

   Çıkış kodu 1 (`HATA` var) ise düzeltmeden devam etmeyin.
2. **Mini App'i kapatın** (bölüm 1'deki tek-oturum uyarısı burada da geçerli).
3. **Mini App'i son kez açıp `initData`'yı alın**, sonra tekrar kapatın.
   `initData` yalnız bir kez sorulacak ve yalnız bellekte tutulacak (diske
   yazılmaz, loglanmaz).
4. **Tek komutu çalıştırın:**

   ```powershell
   $env:PYTHONPATH = "$PWD\src"
   .\.venv\Scripts\python.exe scripts\collect_daily.py --allow-session-takeover
   ```

   `--run-date` verilmezse çalıştırma günü R, sistem saat diliminden değil
   `Europe/Istanbul`'dan hesaplanır (bkz. 6c). Sembol/tarih pencereleri elle
   verilmez; planlayıcı bunları `configs/collection/daily_miniapp_v1.json`
   ve önceki çalıştırma kayıtlarındaki son başarılı pencereden (telafi)
   türetir. Önce her zamanki gibi `--dry-run` ile planı gözden geçirmek
   isteğe bağlı ama önerilir (initData istemez, bağlanmaz):

   ```powershell
   .\.venv\Scripts\python.exe scripts\collect_daily.py --dry-run
   ```

5. **Bitince exit code'a bakın:** `0` = her sembol `saved` veya beklenen bir
   `skipped_*` durumunda; `1` = en az bir `failed:*` var ya da çalıştırma
   429/oturum hatası nedeniyle erken durdu (bölüm 6d); `2` = plan bir insan
   kararı istiyor (ör. bir sembol 90 günlük azami aralığı aşıyor) — bu durumda
   hiç `initData` istenmedi, bağlantı açılmadı.

### 6b. Beklenen süre

Ölçüm değil, tasarım tahmini: 20 sembol × (config'teki `symbol_pause_seconds`,
öneri 30 sn) bekleme ≈ 10 dakika, artı her sembolün kendi bağlantı/istek
süresi. İnsan adımı (1-3) birkaç dakika. Toplam süre bütçesi
`max_wall_seconds`'ta sınırlıdır (config, öneri 3600 sn); aşılırsa kalan
semboller o gün için `skipped_wall_time_guard` olur ve ertesi günün telafi
penceresi eksikleri kapatır.

### 6c. Çalıştırma kaydının okunması

Her çalıştırma tek bir dosyaya yazılır:
`data/interim/daily_capture/_runs/<run_id>.json` (şema
`fintr_daily_collection_run_v1`; `run_id` çalıştırma başlangıcının UTC ISO
zaman damgasıdır, dosya adında `:` yerine `-`). Bu dosya **finansal değer,
kurum kodu/adı, initData veya URL içermez** — yalnız sabit durum
kategorileri, hash'ler ve sayaçlar (`validate_context_contains_no_secrets`
ile denetlenir; testte de aynı kontrol çalışır).

Okurken bakılacak alanlar:

- `symbols[].status`: sembol başına `saved`, `skipped_existing`,
  `skipped_holdout`, `skipped_excluded`, `skipped_rate_limited`,
  `skipped_wall_time_guard`, `skipped_session_failure_stop`, veya
  `failed:<kategori>` (ör. `failed:unknown_event_type`,
  `failed:handoff_<neden>`). `saved` olan her satırda `capture_path`
  (`data/interim/daily_capture/<SYM>/<S>_<E>.json`) ve
  `capture_packet_sha256` doludur.
- `symbols[].unknown_event_types` ve çalıştırma düzeyindeki
  `tolerated_control_event_types` (2026-10-01, ADR K6): `hb`, `request_queued`
  ve `welcome` kontrol frame'leri günlük çalıştırmada tolere edilir (anlamları
  ad ve sıradan çıkarımdır, doğrulanmamıştır) ve sembolü başarısız yapmaz;
  `tolerated` altında ad + sayı olarak görünür. `blocking` altındaki her ad
  (kurala uymayan ad `[UNKNOWN_EVENT_TYPE]`) sembolü `failed:unknown_event_type`
  yapar ve konsolda da (`AKFIS: unknown event type observed (<ad>); fail-closed`)
  yazılır. `tolerated_control_event_types.sha256` config listesinin hash'idir.
  Liste komut satırıyla genişletilemez; yeni bir ad için config değişikliği ve
  karar kaydı gerekir.
- `symbols[].window`: kayıt yokken bile temel yıllık kayıt
  (`data/interim/period_analysis/<SYM>/2025-09-26_2026-09-25.json`) varsa
  pencere başı en geç 2026-09-11'dir (`catch_up: true`; 09-25 − 14 gün), böylece
  temel kayıtla günlük kayıt arasında boşluk kalmaz; dosyası olmayan sembol
  eski `E-14g` penceresini alır. `--dry-run` bunu önceden gösterir.
- `rate_limit.stopped_on_429` / `http_429_count`: `true` ise ilk 429
  görüldüğünde kalan semboller denenmeden atlandı (ADR §6); aynı gün elle
  yeniden deneme yapılmaz, ertesi günün telafi penceresi eksikleri kapatır.
- `init_data_status.prompted`/`age_seconds_bucket`/`accepted`: initData bir
  kez sorulup kabul edildi mi (bkz. 6c-K1b); yaş kovası yalnız `<1h`,
  `1–24h`, `>24h` — asla değer ya da hash yazılmaz. (Şemada `init_data`
  değil `init_data_status` adı kullanılır: `validate_context_contains_no_secrets`
  anahtar adı `init_data`'yı tek başına hassas sayar.)
- `git_commit`/`git_tree_clean`/`config_sha256`: tekrar üretilebilirlik için;
  `git_tree_clean=false` ise çalıştırma sırasında commit edilmemiş yerel
  değişiklik vardı.
- `price_history`: fiyat adımı (bölüm 6e). `status` = `completed` /
  `partial` / `stopped_on_429` / `skipped` (`--skip-price` ya da planlanan
  sembol yok); `symbols[]` altında sembol başına kategori durum, `file_path`,
  `sha256`, `bar_count`. Fiyat/hacim değeri yazılmaz.

### 6d. Hata durumları (özet; ayrıntı bölüm 5)

| Durum | Anlamı | Ne yapın |
|---|---|---|
| Exit 2, hiç `initData` istenmedi | Plan bir sembolü 90 günlük azami aralık nedeniyle bloke etti, ya da `--allow-session-takeover` verilmedi. | Mesajı okuyun; ilkinde elle karar gerekir (bu sürümde `--allow-large-range` yoktur), ikincisinde flag'i ekleyin. |
| Exit 1, `rate_limit.stopped_on_429=true` | İlk 429'da çalıştırma durdu, kalan semboller `skipped_rate_limited`. | Aynı gün yeniden denemeyin; ertesi günün telafi penceresi kapatır. Sık tekrarlanıyorsa `symbol_pause_seconds`'ı artırmayı düşünün. |
| Exit 1, bir veya daha fazla `failed:*` | Fail-closed: parse hatası veya bilinmeyen event tipi o sembolün kaydını engelledi. | `diagnostics` alanına bakın (yalnız sabit kategori/aşama, ham metin yok); yeni bir şema varyantıysa bölüm 4'teki gibi kaydedin. |
| `skipped_session_failure_stop` | İki ardışık sembolde bağlantı hiç istek göndermeden kapandı (bölüm 5'teki aynı desen). | Mini App'in gerçekten kapalı olduğunu ve `initData`'nın taze olduğunu doğrulayın. |

### 6e. Fiyat adımı ve `--skip-price`

Tek komut Mini App adımından sonra otomatik olarak Yahoo fiyat geçmişini de
çeker (`2023-01-02..E`, aynı 20 geliştirme hissesi; ayrıntı
[collector.md](collector.md#günlük-fiyat-adımı)). Yahoo ayrı bir kaynaktır:
Mini App 429 ile durmuş olsa da fiyat adımı çalışır. Yalnız Mini App verisi
istiyorsanız `--skip-price` ekleyin. `--dry-run` fiyat için de yalnız plan
gösterir (hangi dosya yazılacak / zaten var). Fiyat adımında `partial` veya
`stopped_on_429` görürseniz exit code 1'dir; aynı gün yeniden denemeyin —
hedef dosya varsa üzerine yazılmaz, eksik semboller ertesi günün
çalıştırmasında (yeni `E`) ayrı dosya olarak gelir. Yahoo için izin beyanı
Mini App'ten ayrıdır (`user_asserted_2026-09-26_not_independently_verified`).

### 6f. D0 karşılaştırması (ilk canlı günlük çalıştırmadan sonra)

**Ne zaman:** ilk canlı günlük çalıştırma başarıyla bittikten sonra, kayan
pencere hâlâ 2026-09-25 ve öncesine düşen günleri kapsıyorken (pencere sonu
E ≥ 2026-09-26 olan ilk çalıştırmalarda `S = E-14g` bu günleri içerir; pencere
tamamen 09-25'in ötesine kayınca karşılaştırılacak gün kalmaz). Bir kez, temel
yıllık kayıtlar yerinde iken:

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\compare_daily_d0.py --dry-run   # hangi dosyalar
.\.venv\Scripts\python.exe scripts\compare_daily_d0.py
```

Kesme tarihi (2026-09-25) sabittir; sonraki günler karşılaştırılmaz ve
yalnız kesmeden önce başlayan pencere dosyaları açılır. Çıktı yalnız sayımdır
(`<daily-root>/_d0/<UTC zaman damgası>.json` + stdout tablosu): eşleşen/farklı
gün, farklı kod sayısı, `only_base`/`only_daily`; değer ve kurum kodu yoktur.
Exit 1 = en az bir sembol `failed:<kategori>` (ör. `packet_hash_mismatch`,
`base_missing`). Sonuç yorumu finans sohbetinde yapılır.

### 6g. Günlük sıra (özet)

0a. Sabah, her şeyden önce: `check_env.py` (6j).
0b. **Yeni veri var mı?** (yalnız `run_daily_pipeline.py`; ayrıntı 6h) Hiçbir
   bağlantı açılmadan ve initData istenmeden, planlayıcının E=R−1 penceresi
   için yeni işlem günü beklenip beklenmediği yerel dosyalardan kontrol edilir.
   Beklenmiyorsa 1-4. adımların hiçbiri çalışmaz (toplama, tarama, bildirim
   yok; gönderilmiş bir gün tekrar gönderilmez) ve çıkış kodu 0'dır.
1. `collect_daily.py --allow-session-takeover` (Mini App + fiyat adımı; 6a-6e).
2. İlk canlı çalıştırmadan sonra, bir kez: `compare_daily_d0.py` (6f).
3. `scan_observations.py --as-of YYYY-MM-DD --daily-root data\interim\daily_capture --format json --save`
   ([scanner.md](scanner.md#günlük-birleştirme---daily-root)).
4. `notify_observations.py --as-of YYYY-MM-DD` (varsayılan dry-run; gerçek
   gönderim için `--send`, [notify.md](notify.md)).
5. (İsteğe bağlı) `forward_log.py append --as-of YYYY-MM-DD` veya boru hattında
   `--forward-log`; sonra `forward_log/manifest.jsonl` commit'i ve düzenli
   `git tag forward-log-YYYY-MM-DD` ([forward_log.md](forward_log.md)).

### 6h. Tek komut: `run_daily_pipeline.py`

Yukarıdaki 1., 3. ve 4. adımı sırayla çalıştırır (her biri kendi alt
sürecinde; kendi korumaları ve çıkış kodları korunur):

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\run_daily_pipeline.py --allow-session-takeover
```

- **Yeni veri koruması (ilk adım, `--force-collect` ile atlanır):** boru hattı
  `collect_daily.py`'yi başlatmadan, yani initData istemeden ve bağlantı
  açmadan, E=R−1 için şunu kontrol eder. Yeni işlem günü **beklenmiyorsa**
  `yeni veri beklenmiyor (son işlem günü X); toplama atlandı` yazar, 0 ile
  çıkar; toplama, tarama ve bildirim çalışmaz. Beklenmeme nedenleri:
  1. E Cumartesi veya Pazar;
  2. E, onaylı BIST tatil takviminde (`configs\market\bist_holidays.json`,
     Efe onayı 2026-10-16) tam kapanış günü: `reason: official_holiday` ve
     `resmî tatil: <ad>` satırı yazılır. Yarım gün işlem günüdür, atlanmaz.
     E takvimin kapsamı (2023-01-01..2027-03-31) dışındaysa ya da takvim
     okunamazsa stderr'e not düşer ve bu neden kullanılmaz (sessiz karar yok);
     aşağıdaki fiyat geçmişi sezgisi eskisi gibi çalışır. 2027 günleri
     `expected_not_announced` (Diyanet listesi) ve BIST 2027 tablosu
     yayımlanınca (~Aralık 2026) yeniden kontrol edilecek;
  3. E hafta içi ama fiyat geçmişi E'yi kapsıyor ve hiçbir sembolün fiyat
     geçmişinde E için işlem görmüş bar yok (tatil; takvim bu sezgi için yalnız
     `data\interim\price_history` dosyalarından türetilir; takvimde olmayan
     olağanüstü kapanışları da yakalar, ama henüz çekilmemiş günü bilemez);
  4. her planlı sembolün son kaydedilen pencere sonu (çalıştırma kayıtları +
     taban yakalama, planlayıcıdaki kural) zaten E veya sonrası.

  **Kesinleşmemiş gün varsa toplama yine yapılır** (takas düzelmesi için):
  `data\interim\scanner_daily` altındaki en son `observations.json`'da herhangi
  bir sembolün `composition.unsettled.in_window_day_count` değeri 0'dan büyükse
  ya da bu dosya yok/okunamıyorsa durum "bilinmiyor" sayılır ve toplama
  yapılır. Koruma yalnız kanıtlayabildiğinde atlar; kontrol hata verirse
  (yapılandırma/split okunamadı vb.) stderr'e not düşer ve toplamaya devam
  eder. `--dry-run` ve `--skip-collect` korumadan geçmez.
  Sınırlar: fiyat geçmişi yalnız zaten çekilmiş günleri bildiğinden, henüz hiçbir
  fiyat çekiminin kapsamadığı bir tatil ilk denemede "yeni gün bekleniyor"
  görünür ve bir kez toplanır (o çalıştırmanın fiyat adımı tatili kaydeder; aynı
  günkü yeniden çalıştırma atlanır). Geçmiş bir `--run-date` ile yeniden toplama
  gerekiyorsa `--force-collect` verin.
- `collect_daily.py` initData'yı kendisi sorar (stdio devralınır). Çıkış kodu
  0 değilse dur; tarama ve bildirim çalışmaz, hiçbir şey gönderilmez.
- Tarama: `scan_observations.py --daily-root data\interim\daily_capture
  --out-dir data\interim\scanner_daily --save --format json` (varsayılan
  as_of); kullanılan as_of kaydedilen yoldan okunur, gözlem içeriği basılmaz.
- Bildirim: `notify_observations.py --as-of <as_of> --scanner-dir
  data\interim\scanner_daily` varsayılan **dry-run**; gerçek gönderim için
  boru hattına `--send` ver. `--resend` hiç geçilmez: mevcut makbuz
  engelliyorsa rapor edilir, aşılmaz.
- `--skip-collect`: yalnız tarama+bildirimi yeniden çalıştırır (toplama
  başarılıysa). `--skip-price`, `--run-date`, `--dry-run` collect'e iletilir;
  `--dry-run`da 2-3. adımlar yeni veri olmadığı için atlanır.
- Son özet: adım -> çıkış kodu, as_of, tarayıcı çıktı yolu, gönderildi mi.
  `--save` üzerine yazmaz; aynı as_of çıktısı zaten varsa adım 2
  "already_scanned" sayılır (yeniden taranmaz) ve bildirime geçilir; `--send`
  ile makbuz zaten varsa adım 3 "already_sent" olur (yeniden gönderilmez,
  çıkış 0). Başka her tarama/bildirim hatası boru hattını durdurur.
- `--accuracy` (varsayılan **kapalı**; [accuracy.md](accuracy.md)): taramadan hemen sonra
  `track_accuracy.py` çalışır ve geliştirme hisseleri için yürüyen yön isabetini
  `data\interim\accuracy\<koşu günü>\accuracy.json` + `accuracy.md` olarak yazar
  (üzerine yazmaz; aynı gün tekrarında özet `accuracy: exists`). Özet satırı
  `accuracy: saved|exists|failed:...`; yalnız çıktı yolu basılır (isabet/getiri
  değeri basılmaz). Hata ya da 900 sn zaman aşımı bildirimi ve çıkış kodunu
  değiştirmez; Telegram'a hiçbir şey gönderilmez. Holdout sembolleri izlenmez.
  Gerçek veride yaklaşık 4 dakika sürer (bildirimden önce çalıştığı için o kadar
  geciktirir); sonuçlar finans sohbetinde okunur, burada tartışılmaz.

Bu bölüm yalnız süreci anlatır; günlük toplamanın finansal sonuçları bu
belgede veya yönetici sohbetinde tartışılmaz (`docs/STATUS.md`).

### 6i. Günlük analiz notu: `--llm-note` (Opus 5.5, Claude Code CLI)

Varsayılan **kapalı**. Açmak için 6h'deki tek komuta `--llm-note` ekle:

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\run_daily_pipeline.py --allow-session-takeover --llm-note
```

- **Düzen v3 (varsayılan, Efe kararı 2026-10-03):** sıra toplama -> tarama -> `llm_note.py` ->
  **tek** sinyal mesajı (notun kanıt kelimesi satırın içinde; ayrı not mesajı yok). Not hatası
  ya da zaman aşımı mesajı düşürmez (`Opus: —`). Aşağıdaki sıra ve maddeler `--layout v2` içindir
  (`docs/notify.md` "Sinyal chati").
- **Sıra (v2):** toplama -> tarama -> **deterministik bildirim** (modeli beklemez) ->
  `llm_note.py` -> not bildirimi (`notify_observations.py --llm-note-dir
  data\interim\llm_notes`: kendi Telegram mesajı, kendi makbuzu
  `note_receipt.json`; mesaj başına durumlu, devam edebilir: aynı gün yeniden çalıştırma yalnız
  gönderilmemiş not mesajlarını gönderir, tamamlanmışı tekrar göndermez; içerik değiştiyse
  `--resend` ister). Not adımının veya not
  bildiriminin hatası gönderilmiş bildirimi ve çıkış kodunu değiştirmez; yalnız
  özette görünür. Hisse başına bir model çağrısı yapılır; özette `llm_note:
  <durum>`: `ok=A,rejected=B,failed=C` (+ `,already=D`: zaten doğrulanmış notu
  olan hisseler; `rejected` = doğrulamadan geçemedi, `failed` = alınamadı),
  `already_noted`, `no_observations`, `error:<kategori>`; ve `note_notify: 0 |
  already_sent | skipped | failed:exit_N`. Adım hisse sayısı x çağrı süresi kadar
  sürer (çağrı başına 900 sn zaman aşımı, tüm adım için `max_run_seconds` 1800).
- **Ön koşul:** Claude Code CLI Efe'nin aboneliğiyle giriş yapmış olmalı
  (`%USERPROFILE%\.local\bin\claude.exe`; farklı yol için
  `configs/llm_note/config_v1.json` `cli_path` ya da `scripts\llm_note.py
  --claude-path`). `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` çocuk sürece
  geçirilmez; `CLAUDE*`/`ANTHROPIC*` önekli bütün ortam değişkenleri de çıkarılır
  (üst süreç Claude Code içinde çalışıyor olabilir; abonelik girişi kullanılır). Çağrı: `claude -p --model claude-opus-5-5
  --effort high --tools "" --strict-mcp-config --disable-slash-commands
  --setting-sources local --exclude-dynamic-system-prompt-sections
  --output-format json --json-schema <şema> --no-session-persistence
  --system-prompt-file configs\llm_note\prompt_v1.md`, veri stdin'den; çalışma
  dizini her çağrıda yeni boş geçici dizin. `--bare` kullanılmaz (abonelik/OAuth
  girişini hiç okumaz). Bayrak adları CLI sürümüne göre
  `src/fintr/llm_note/runner.py` `build_command` içinde tek yerde değiştirilir
  (`claude --help` ile doğrula). `--setting-sources` değeri config'te
  (`setting_sources`, varsayılan `local`). `--exclude-dynamic-system-prompt-sections`
  özel sistem istemiyle yok sayılır (zararsız).
- **İzolasyon doğrulaması (CLI girişten sonra, bir kez):** `.\.venv\Scripts\python.exe
  scripts\llm_note.py --probe-isolation` modele veri vermeden tek sabit kanarya
  sorusu sorar (bağlamında anlatılmış kullanıcı adı / kişisel not klasörü adı var mı? yoksa
  yalnızca `YOK`); kanarya sözcükleri yalnızca `probe_canaries` config'indedir, istemde yoktur;
  cevabı `data\interim\llm_notes\_probes\attempt-NNN\record.json`'a
  yazar. Çıkış 0 ve `LLM_NOTE_STATUS: probe:clean` = izole (`YOK`); `probe:not_isolated`
  (cevapta bir kanarya sözcüğü geçti) veya `probe:unclear` ise günlük çalıştırma açılmamalı (`setting_sources`/bayrakları
  gözden geçir). `configs/llm_note/config_v1.json` içinde `"isolation_probe": true` her
  çalıştırmada sondayı fail-closed ön kontrol yapar (varsayılan kapalı). Not: cevap
  modelin kendi beyanıdır, kanıt değil; yine de bir kullanıcı talimatı sızıntısını yakalar.
- **Önce dry-run:** `.\.venv\Scripts\python.exe scripts\llm_note.py --as-of
  <as_of> --scanner-dir data\interim\scanner_daily --dry-run` paketi kurar,
  hisse başına boyutunu/sha256'sını, en büyük ve ortanca bayt sayısını yazar;
  model çağırmaz, dosya yazmaz.
- **Abonelik limiti:** limit/zaman aşımı/red durumunda o hisse için not "alınamadı
  (usage_limit | timeout | refusal | cli_error | cli_not_found | packet_too_large |
  symbol_limit | run_time_budget)" olur, bildirim etkilenmez. `usage_limit` veya
  `cli_not_found` sonrası kalan hisseler çağrılmadan aynı kategoriyle kaydedilir.
  Limit açıldığında aynı gün `--skip-collect --llm-note` ile yeniden çalıştırmak
  yalnız doğrulanmış notu olmayan hisseler için yeni `attempt-NNN` üretir
  (doğrulanmış notlu hisse için model **yeniden çağrılmaz**; makbuz varsa bildirim
  yeniden gönderilmez, 6h).
- **Kayıtlar:** `data\interim\llm_notes\<as_of>\<SYM>\attempt-NNN\` (hiçbir
  dosyanın üzerine yazılmaz): `packet.json` (modele giden o hissenin ham paketi),
  `response_raw.txt`, `note.json` (yalnız doğrulanmış not), `record.json`
  (paket/prompt/şema/config sha256, model, CLI sürümü, komut satırı, çağrı süresi,
  CLI'nin bildirdiği kullanım, doğrulama sonucu, determinizm notu). `record.json`
  en son yazılır; olmayan attempt yarım sayılır. LLM çıktısı deterministik
  değildir. Hisse başına paket sınırı `max_symbol_packet_bytes` (varsayılan
  200 000 bayt), çalışma başına hisse sınırı `max_symbols_per_run` (30);
  aşılırsa o hisse kırpılmaz, kendi "alınamadı" satırını alır.
- **Gizlilik:** ham AKD/takas satırları Anthropic'e gönderilir (Efe kararı;
  lisans kapsamı doğrulanmadı). Kayıtlarda secret yoktur (kayıt yazılmadan önce
  `validate_context_contains_no_secrets`).

### 6j. Sabah ortam kontrolü: `check_env.py`

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\check_env.py            # bugün (Europe/Istanbul)
.\.venv\Scripts\python.exe scripts\check_env.py --run-date 2026-10-05   # başka bir R günü için E
```

Salt okunur ve çevrimdışıdır: ağ bağlantısı açmaz, Telegram'a veya modele bir
şey göndermez, dosya yazmaz, Claude CLI'ı çalıştırmaz. Her madde için tek satır
(`OK` / `UYARI` / `HATA`) ve sonda özet basar; `HATA` varsa çıkış kodu 1'dir.

| Madde | Ne denetlenir | Sorun olursa |
|---|---|---|
| python, paket | `pyproject.toml`'daki `requires-python` ve bağımlılıklar (`websockets`, `tzdata`) kurulu ve sürüm kısıtına uygun mu (`importlib.metadata`) | eksik/uyumsuz paket `HATA`; `tzdata` yoksa `UYARI` (Europe/Istanbul için UTC+3 sabit yedeği kullanılır; Türkiye'de 2016'dan beri DST yok, sonuç doğru) — kurmak için `pip install tzdata` |
| ortam `FINTR_TELEGRAM_*` | `FINTR_TELEGRAM_BOT_TOKEN` ve `FINTR_TELEGRAM_CHAT_ID` tanımlı mı | yalnız `var`/`yok` yazılır; değer ve uzunluk hiçbir yerde basılmaz. Yoksa `UYARI` (gerçek gönderim `--send` için gerekir) |
| Claude CLI | `configs/llm_note/config_v1.json` `cli_path` işaret ettiği dosya var mı | yalnız varlık denetlenir, CLI çalıştırılmaz; yoksa `UYARI` (yalnız `--llm-note` için gerekir) |
| config, split, exclusions | etkin split, exclusions, toplama/analiz notu/bildirim config'leri okunuyor mu (mevcut yükleyiciler) | okunamıyorsa `HATA` |
| hash | spec'ler `docs/prereg/FREEZE.md` (ham + kanonik), etkin split dondurulmuş spec'lerdeki kayıt, test planı `DECISIONS.md`, tarayıcı kuralları `rules_v1.json` ile eşleşiyor mu | eşleşmezse `HATA`; dondurulmuş dosyalar değiştirilmez, nedeni araştırın |
| son çalıştırma | `daily_capture/_runs/` en yeni kaydın tarihi, durumu, `saved`/`failed` sayıları; son `scanner_daily` as_of; `data/interim/notify/<as_of>/` altındaki son notify receipt (tamamlandı/kısmi) ve varsa not receipt'i (ayrı satır) | yalnız tarih, durum ve sayı yazılır, finansal içerik ve mesaj metni yazılmaz; `failed > 0`, kısmi receipt, hiç kayıt yoksa veya scanner as_of'u son receipt as_of'undan yeniyse (`gönderilmemiş tarama var`) `UYARI` |
| pencere sonu (E) | bugünkü R için planlayıcının (`collect_daily.py --dry-run` ile aynı saf planlayıcı, ağsız) hedefleyeceği E | E hafta sonuysa veya tüm planlı semboller için zaten toplanmışsa `yeni veri beklenmiyor` yazar; azami aralığı aşan sembol varsa `UYARI` |
| disk | proje diskinde boş alan | < 1 GiB `UYARI`, < 100 MiB `HATA` |

**Sınır:** repoda borsa tatil takvimi yoktur; E'nin resmi tatil olup olmadığı
`check_env.py` tarafından denetlenmez (satır bunu açıkça söyler). Tatil, E'ye
ait yeni veri gelmemesi anlamına gelir; Borsa İstanbul resmi takviminden elle
doğrulayın. Bu betik yeni bir eşik, kural veya spec eklemez.

`tzdata` kurulduğunda yeni fiyat geçmişi capture'larında
`normalized.date_source` alanı `gmtoffset_fallback` yerine
`exchange_timezone_name` olur ([price_history.md](price_history.md)); yalnız
kayıtlı yöntem alanı değişir.

### 6k. Haftalık replay uzlaştırması: `replay_scans.py` (E3)

Haftada bir (ör. Cumartesi; günlük boru hattına **eklenmez**), salt okunur ve
çevrimdışı:

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\replay_scans.py            # son 5 as_of, iki mod
```

Çıkış kodu: 0 = hepsi aynı, 2 = fark var, 1 = hata. Son satır
`REPLAY: as_of=K identical=I diff=D errors=E`. `point-in-time` modunda fark
**kod/kural kayması** (ya da özgün girdi dosyalarının sonradan değiştirilmesi)
anlamına gelir ve araştırılmadan geçilmez; `current` modundaki fark, girdi
dosyası kümesi değiştiyse veri revizyonudur. Rapor
`data/interim/scanner_replay/<UTC>/replay_report.json` (üzerine yazmaz).
Ayrıntı: [scanner.md](scanner.md) "Haftalık replay".
