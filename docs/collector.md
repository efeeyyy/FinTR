# WebSocket collector

## Kapsam

Collector, Veri Terminali Mini App tarafından izin verilmiş piyasa mesajlarını
kısa süreli olarak almak ve mevcut parser katmanına iletmek için tasarlanmıştır.
Authentication üretmez veya atlatmaz; kullanıcı tarafından runtime'da sağlanan
Telegram `initData` değerini yalnız bağlantı kurulurken bellekte kullanır.

Bu aşamada sürekli veri toplama ve kalıcı saklama yoktur.

## Mimari

```text
InteractiveSecretProvider
          |
          v
WebSocket transport ----> raw JSON frame
                              |
                              v
                       event validation
                              |
                              v
                         dispatcher
                              |
                              v
                       existing parsers
                              |
                              v
                     normalized objects
                              |
                              v
              MarketSnapshotAssembler (optional)
```

Transport, event doğrulama ve parser katmanları ayrıdır. `ReplayTransport`, canlı
bağlantı olmadan aynı dispatcher/parser yolunu kullanır.

Canlı transport, request gönderimini ilk üst düzey `welcome` event'inin sonrasına
erteler; gönderim döngüsü her bağlantı denemesinde yalnız bir kez çalışır.
Tekrarlanan `welcome` veya `hb` yeniden gönderimi tetiklemez. `hb`, `error`,
`banned` ve nested `market_update` içindeki bir `welcome` hazır sinyali sayılmaz.
Welcome payload'ına özel şema eklenmez veya payload loglanmaz; event mevcut
dispatcher'dan unknown result olarak geçer. Welcome gelmezse receive timeout'u
veya toplam frame sınırı içinde request gönderilmeden dönülür. Bu sınırlar
welcome beklenirken de geçerlidir; retry/dedup davranışı değişmemiştir.

Welcome-sonrası gönderim kullanıcı tarafından canlı denendi; beklenen response
yine alınmadı. Bu sıra değişikliği sorunu çözmüş sayılmaz. Offline testler yalnız
gönderim sırasını, tek seferlik tetiklemeyi ve bekleme sınırlarını doğrular.

## Canlı doğrulama durumu ve protokol sınırları

Aşağıdaki canlı sonuçlar kullanıcı/yönetici tarafından bildirilen gözlemlerdir;
bu dokümantasyon güncellemesinde yeni bağlantı kurulmadı. Public Mini App JS
bulguları da yönetici tarafından aktarılan kaynak incelemesidir; canlı response
örneği veya sunucu protokol garantisi değildir.

- **Transport:** Origin düzeltmesinden sonra WebSocket handshake başarılıdır;
  bildirilen denemelerde HTTP 403 giderilmiştir. `welcome` ve `hb` alınması,
  piyasa request'inin sunucuda kabul edildiğini kanıtlamaz.
- **Önceki smoke:** Mini App kapalıyken 35 frame sınırında bir `welcome` ve 34 `hb`
  alınmış, `brokerages_data` alınmamış ve araç `Smoke failed` ile sonlanmıştır.
  Önceki 8 frame sınırındaki ve welcome-sonrası gönderim denemelerinde de
  beklenen response yoktur. Oturum çakışması son gözlemi açıklamıyor; asıl neden
  henüz belirlenmedi.
- **Son brokerages smoke:** Kullanıcının bildirdiği en son default smoke'ta
  `get_brokerages - send completed`, `brokerages_data (known)` ve
  `Smoke completed: requested response received: brokerages_data` görüldü;
  persistent storage disabled idi. Bu, mevcut nested-string parser'ının bu tek
  yanıttaki satırları kabul ettiğini doğrular. Daha önce bildirilen güvenli shape
  `event.data.data` string'inin JSON array olduğunu göstermiştir. Sonuç tüm
  provider varyantlarını, alanların/isimlerin doğruluğunu veya kullanım lisansını
  garanti etmez; ham satır, kurum adı/kodu, finansal değer ve secret kaydedilmedi.
- **Son custody-stats smoke:** Kullanıcının aktardığı manuel
  `--custody-stats-only --symbol AKFIS` çıktısında `send completed`,
  `custody_stats_data (known)` ve `Smoke completed` görüldü. `AKFIS/true` yalnız
  yerel gönderilmiş istek provenance'ıydı. Bu turda yeniden bağlantı kurulmadı;
  ham frame, finansal değer veya secret kaydedilmedi. Çıktı güncellik/doğruluk
  ya da sunucu-tarafı korelasyon kanıtı değildir.
- **Toplu canlı smoke (2026-09-24, kullanıcı çıktısı):** `AKFIS` ve
  `2026-09-23` tarihiyle, `--require-all-responses` kullanılarak aynı bağlantı
  denemesinde `get_brokerages`, `get_akd`, `get_custodian`,
  `get_custody_distribution` ve `get_custody_stats` gönderimleri tamamlandı.
  Beş beklenen yanıt da `known` olarak normalize edildi ve araç
  `Smoke completed` döndürdü. Günlük Takas yanıt tarihi istenen tarihle
  eşleşti. Kalıcı kayıt kapalıydı. Bu, bildirilen tek oturumun bounded
  transport/dispatcher/parser kabul testidir; ham veri, değer doğruluğu,
  server-side request-id korelasyonu, lisans ve başka sağlayıcı varyantları
  doğrulanmış sayılmaz.
- **Gönderim kanıtının sınırı:** Repo kodu welcome sonrasında
  `{"type":"get_brokerages"}` için `websocket.send()` çağrısını await eder.
  Bu istemci davranışı tek başına sunucunun isteği işlediğini kanıtlamaz.
  Son kullanıcı smoke'unda aynı denemede gönderim tamamlandı ve
  `brokerages_data` geldi; request-id korelasyonu yoktur. Önceki sessiz
  denemelerin nedeni hâlâ bilinmemektedir.
- **Client-side cache:** Aktarılan public JS'teki `fetchBrokerages()` önce
  in-memory ve güncel localStorage cache'ini kontrol eder. Yalnız cache yoksa
  OPEN socket'e `{"type":"get_brokerages"}` gönderir. Mini App'te kurum
  listesinin görünmesi tek başına yeni bir WebSocket response'u kanıtlamaz;
  cache davranışı da collector'da yanıt gelmemesinin nedeni olarak doğrulanmış
  değildir.

Brokerage şemasında ayrıca çözülmemiş bir uyuşmazlık vardır:

| Kanıt düzeyi | Beklenen biçim |
|---|---|
| Aktarılan public JS `brokerages_data` handler'ı | Candidate alanlar: `code`, `title`, `short_title`; bir kullanıcı-bildirimli live response mevcut parser'dan geçti, genel provider garantisi değil |
| Repo fixture/parser sözleşmesi | `date` + `data.results` array'i; satırlarda `code`, `name` |
| Kullanıcının bildirdiği canlı `brokerages_data` | Nested JSON array ve satırlar mevcut candidate typed parser'dan geçti; yalnız bu tek response gözlemi |

Repo beklentisi `tests/fixtures/market/brokerages_data.json` ve
`src/fintr/data/parsers/brokerages.py` ile doğrulanabilir. Fixture canlı kaynaktan
doğrulanmış bir şema olarak sunulmamalıdır. Yeni live parser gömülü JSON'u tek
sefer ve boyut sınırı içinde çözer; üst düzey array ile candidate
`code`/`title`/`short_title` string alanlarını bekler. Kullanıcı tarafından
bildirilen bir başarılı response bu alan kontrollerinden geçmiştir; başka
response varyantları, adların doğruluğu ve lisans/provenance doğrulanmış değildir.
Eski fixture/parser yolu ayrı kalır.

Bu provider yolu uçtan uca piyasa verisi erişimi, kaynak doğruluğu veya persistent
collection için hazır değildir. Offline test başarısı canlı şema, veri
bütünlüğü, lisans/izin ve provenance doğrulamasının yerine geçmez. Kalıcı
toplama ve analizden önce gerçek response sözleşmesi ile kaynak/izin/provenance
koşulları doğrulanmalıdır.

### Güncel AKD ve takas WS envanteri

Kullanıcının Mini App `Network > WS Messages` üzerinden doğruladığı yeni
request/response alanları [protokol envanterinde](protocol_inventory.md)
kayıtlıdır. Bu WS gözlemleri, yukarıdaki public JS incelemesinden ayrı kaynaktır;
yerel collector'ın uçtan uca başarısı olarak sunulmaz.

- AKD sorgusu `get_akd {company,start,end}` → `akd_data`dır. Envelope iç verisinde
  artık `as_of`, `results` ve `source: live`; kurum satırında `brokerage`,
  `net:{size,percentage,cost}` ve `total:{size,percentage,cost,volume}`
  alanları gözlenmiştir. Önceki “tam kurum satırı paylaşılmadı” notu güncel değildir.
- `subscribe_akd` ve `akd_snapshot` da gözlenmiştir; snapshot available true/false
  olabilir. Snapshot, asıl sorgu cevabından ve repo'daki aynı adlı offline
  fixture'dan ayrıdır. Abonelik request'inin tam payload'ı hâlâ eksiktir.
- Günlük takas, dönem farkı, takas analizi, dağılım ve istatistik response'ları
  gözlenmiştir. Günlük takasta fraction yüzde ölçeği ve kesirli değerler;
  dönem farkında farklı yüzde ölçeği; analizde ayrı AKD/custody dalları;
  dağılımda altı tarih ve nullable değerler; istatistiklerde cached envelope ve
  null gözlemler doğrulanmıştır. Artık “takas alan adları hiç doğrulanmadı” denemez.
- Birim/ölçeklerin tamamı, sembol/dönem korelasyonu ve her event'in optional/null
  varyantları henüz tamamlanmamıştır. Bir `brokerages_data` yanıtı typed parser'dan
  geçmiş olsa da diğer response varyantları ve tam provider şeması açık eksiktir;
  depth request şeması da bilinmemektedir.

`akd_data` için gözlenen `brokerage/net/total/as_of/source` alanları artık ayrı
typed modele parse edilir. Sembol ve tarih aralığı response'ta doğrulanmadığı
için yalnız aynı bağlantıda gerçekten gönderilmiş tek `get_akd` isteğine ait
yerel provenance olarak bağlanır. Bu bağ yoksa yanıt typed olsa da pending kalır;
eski `AKDSnapshot` modeline dönüştürülmez.

Günlük `custodian_data` canlı yanıtında `event.data.data.date` istenen tarihten
farklı olabilir. Ayrı `LiveCustodianResponse` modeli yanıt tarihini, kesirli
`value` ve oran ölçeğindeki `percentage` değerlerini `Decimal` olarak korur.
Sembol ile istenen tarih yalnız aynı bağlantıda gerçekten gönderilmiş tek
`get_custodian` isteğinin yerel provenance'ıdır. Legacy düz fixture ayrı parser
yolunda kalır; canlı zarfın eksik alanları legacy'ye düşürülmez.

`custodian_diff_data` canlı yanıtı ayrı `LiveCustodianDiffResponse` modeline
parse edilir. Altı sayısal satır alanı kesirli değerler dahil `Decimal` olarak
korunur; oran veya birim dönüşümü yapılmaz. `start_date/end_date` yanıtın kendi
dönemidir. Sembol ve istenen `start/end` yalnız aynı bağlantıda gerçekten
gönderilmiş tek `get_custodian_diff` isteğinin yerel provenance'ıdır. Legacy
`first_date/last_date` fixture'ı ayrı parser yolunda kalır.

`custody_analysis_data` canlı yanıtı ayrı AKD ve custody tarihli serilerini
korur. Kesirli sayılar `Decimal`dır; custody satırına `daily_net` veya `cost`
türetilmez, görünmeyen günler doldurulmaz. `data_range` için yalnız
`requested_from/requested_to/actual_from` yorumlanır. Metadata'da görülen
null alanlar sıfır sayılmaz; aynı alanların non-null biçimi henüz desteklenen
şema değildir. Canlı sembol, tek gerçekten gönderilmiş isteğin yerel sembolüyle
eşleşmelidir. Legacy `CustodyHistory` fixture'ı ayrı kalır.

`custody_distribution_data` canlı yanıtı ayrı nested parser/model yolundadır.
`periods` tarihleri ve satır `values` dizileri değişken uzunlukta olabilir ama
uzunlukları eşleşmelidir; sayılar `Decimal`, null değerler `None` olarak korunur.
Slotlara ufuk, values'a para/lot birimi anlamı atanmaz. Yanıt sembol taşımadığı
için başarı yalnız aynı denemede tek `get_custody_distribution` gerçekten
gönderildiyse tamamlanır; yerel provenance sunucu eşleşmesi değildir. Beş integer
slotlu legacy fixture, parser ve assembler yolu değişmeden kalır. Canlı nested
payload için doğrudan (`event.data.data`) ve kanıtlı üçüncü-seviye
(`event.data.data.data`) yolları açıkça seçilir; ikisi çakışırsa parser hata
verir. Parse hata shape özeti üçüncü seviyeye kadar yalnız allowlist alan/tür
kategorilerini içerir; yeni bir live smoke gözlemi yerine geçmez.

Son kullanıcı canlı smoke gözleminde bir `akd_data` event'i geldi; `as_of` alanı
eksik veya null olarak raporlandı (hangisi olduğu ayırt edilmedi). Bu nedenle
modelde `as_of=None` kabul edilir; diğer gözlenen alanların doğrulaması aynen
korunur. Bu tek gözlem tam response şemasını doğrulamaz.

## Dependency seçimi

`websockets>=17.1,<18` kullanılır. 17.1, Python 3.12 Windows wheel'i sağlar ve
Python 3.11+ destekler. İstemci güncel `websockets.asyncio.client.connect`
API'sini kullanır; bağlantı, mesaj boyutu, receive ve close timeout'ları
sınırlıdır.

Canlı Mini App handshake'i tarayıcı origin'ini gönderir. Cloudflare, bu başlık
olmadan isteği uygulamaya iletmeden HTTP 403 ile reddettiği için collector
`Origin: https://7k2v9x1r0z8t4m3n5p7w.com` başlığını açıkça gönderir. Origin
config'de query, fragment veya path kabul etmeyen ayrı bir HTTPS origin olarak
doğrulanır; auth verisi yine yalnız URL'deki geçici `init_data` parametresidir.

- Resmî paket: <https://pypi.org/project/websockets/>
- Resmî asyncio client API:
  <https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html>

## Desteklenen response event'leri

Aşağıdaki liste kod/fixture düzeyindeki desteği gösterir; canlı sağlayıcıyla
uçtan uca doğrulanmış event listesi değildir.

- `depth_update`
- `akd_snapshot` (eski offline sözleşme)
- `akd_data` (gözlenen envelope doğrulaması; tam normalizasyon bekliyor)
- `custodian_data`
- `custodian_diff_data`
- `custody_distribution_data`
- `custody_analysis_data`
- `custody_stats_data`
- `brokerages_data`

`market_update` wrapper'ı içindeki `data.type` güvenli biçimde açılır. Bilinmeyen
event crash oluşturmaz; yalnız güvenli event type debug seviyesinde loglanır.
Raw payload varsayılan olarak loglanmaz veya diske yazılmaz.

## Desteklenen request'ler

- `get_akd`
- `get_custodian`
- `get_custodian_diff`
- `get_custody_distribution`
- `custody_analysis`
- `get_custody_stats`
- `get_brokerages`

Request builder'lar sembol ve ISO tarih doğrulaması yapar ve deterministik JSON
üretir. Bu liste her request payload'ının bütünüyle doğrulandığı anlamına gelmez;
güncel WS gözlemleri ve açık alanlar [envanterde](protocol_inventory.md) ayrıdır.
Depth subscription request payload'ı doğrulanmış olmadığı için bilinçli
olarak uygulanmamıştır. Fiyat grafiği ve tahmin edilen diğer mesajlar da kapsam
dışıdır.

## Secret yönetimi

- `initData`, `getpass` ile hidden terminal input olarak alınır.
- Source code, config, command-line argument, fixture veya Git'e yazılmaz.
- Bağlantı URL'si yalnız `init_data=[REDACTED]` biçiminde gösterilir.
- Collector `repr()` çıktısı provider veya secret içermez.
- Terminal exception'ları secret ve query string temizlendikten sonra üretilir.
- `websockets` transport logger'ı collector içinde dışarıya yayılmaz.

Örnek redacted görünüm:

```text
wss://ws.7k2v9x1r0z8t4m3n5p7w.com/?init_data=[REDACTED]
```

## Replay modu

Replay transport, sanitize fixture frame'lerini internetsiz çalıştırır:

```python
from fintr.collector import replay_frames

results = await replay_frames(json_frames)
```

Akış canlı transport ile aynıdır: frame → validation → dispatcher → parser →
normalized object. Replay hiçbir auth bilgisi gerektirmez.

## Live smoke

Canlı sağlayıcı aynı hesap/initData için tek aktif WebSocket oturumuna izin verir.
Collector bağlantısı açık Telegram Mini App oturumunu kapatabilir ve Mini App'te
“Başka Cihazdan Giriş Yapıldı” uyarısına yol açabilir. Eşzamanlı kullanım
desteklenmez. Bu nedenle live smoke her çalıştırmada açık
`--allow-session-takeover` onayı ister; flag yoksa initData istemeden ve ağa
bağlanmadan exit code 2 ile durur.

Güvenli çalışma sırası:

1. Telegram Mini App açıkken gerekli `initData` değerini geçici olarak alın.
2. Mini App'i kapatın; aynı oturumu eşzamanlı kullanmayın.
3. Collector'ı `--allow-session-takeover` flag'iyle başlatın ve hidden prompt'a
   initData'yı girin.
4. Collector bittikten sonra Telegram'da Mini App'i yeniden açarak yeni oturum
   başlatın.

Varsayılan smoke yalnız `get_brokerages` gönderir:

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py `
  --allow-session-takeover
```

Başarı (exit code 0), gönderilen request türlerinden **en az birinin** beklenen
response event'inin parser tarafından normalize edilmesini gerektirir. Yalnız
handshake, `welcome`/`hb` veya istenmeyen bir known event yeterli değildir;
beklenen response alınmazsa exit code 1 döner. Kontrol frame'lerinin yalnız event
adı gösterilir; payload şemaları yorumlanmaz ve piyasa parser'larına eklenmez.

Birden fazla isteği aynı bağlantıda doğrularken `--require-all-responses` ekleyin.
Bu seçenek, planlanan her isteğin gerçekten gönderilmiş ve karşılık gelen her
yanıtın normalize edilmiş olmasını ister; eksik türleri adlarıyla bildirip exit
code 1 döner. Varsayılan tek-istek smoke davranışı değişmez. Bu kontrol,
yanıtların sağlayıcıdan geldiğini gözlemler; finansal içeriğin doğruluğunu veya
farklı sembol/tarih varyantlarını kanıtlamaz.

Beşli yanıtın bellek içi kanıt görünümünü de doğrulamak için ayrıca
`--validate-evidence-bundle` kullanın. Bu flag yalnız
`--require-all-responses` ve varsayılan beş-istek `--symbol`/tarih profiliyle
çalışır. Eksik/çift/pending yanıtı, yerel istek bağlamı eksikliğini, sembol veya
günlük Takas tarihi uyuşmazlığını reddeder. Başarıda yalnız
`In-memory evidence view: validated` durumunu yazar; ham veri, kurum adı veya
sayısal değer yazmaz ve diske kaydetmez. Kullanıcının 2026-09-24'te paylaştığı
AKFIS/2026-09-23 canlı smoke çıktısında beş isteğin gönderimi, beş beklenen
yanıtın işlenmesi ve `In-memory evidence view: validated` sonucu görüldü.
Bu tek oturumluk doğrulama finansal içeriği veya diğer sembol/tarih varyantlarını
onaylamaz; kalıcı kayıt yapılmadı.

Smoke eşlemesi `scripts/live_collector_smoke.py::REQUEST_RESPONSE_EVENTS` içindedir:

| Request | Beklenen response |
|---|---|
| `get_brokerages` | `brokerages_data` |
| `get_akd` | `akd_data` (typed; güvenli yerel istek bağlamı gerekir) |
| `get_custodian` | `custodian_data` (canlı typed yanıt ve güvenli yerel istek bağlamı gerekir) |
| `get_custody_distribution` | `custody_distribution_data` (nested live typed yanıt ve tek gönderim provenance'ı gerekir) |
| `get_custody_stats` | `custody_stats_data` (live typed yanıt ve tek gönderim provenance'ı gerekir) |
| `custody_analysis` | `custody_analysis_data` (canlı typed yanıt, tek gönderilmiş istek ve sembol eşleşmesi gerekir) |
| `get_custodian_diff` | `custodian_diff_data` (canlı typed yanıt ve güvenli yerel istek bağlamı gerekir) |

Yalnız `akd_data` alınır ama tek gerçekten gönderilmiş isteğin bağlamı yoksa
smoke exit code 1 döner. Eski `akd_snapshot` artık `get_akd` isteğini
karşılamaz. Başka bir istenen response başarıyla normalize edilirse mevcut
“en az biri” başarı ölçütü korunur; bu AKD'nin tamamlandığını göstermez.

`--custodian-only --symbol AKFIS --custody-date 2026-09-23` yalnız günlük Takas
isteği planlar. Başarı için canlı typed yanıtın tek gerçekten gönderilmiş
isteğe bağlanması gerekir. Çıktı yerel istek tarihini ve sunucunun döndürdüğü
yanıt tarihini ayrı gösterir; eski düz fixture smoke başarısı sayılmaz.

`--custodian-diff-only --symbol AKFIS --start-date 2026-09-14 --end-date
2026-09-23` yalnız dönem farkı sorgusu planlar. Başarı için canlı typed yanıtın
tek gerçekten gönderilmiş isteğe bağlanması gerekir. İstenen ve dönen dönemler
ayrı gösterilir. Bir önceki örnekte dönen bitiş daha erkendi; sonraki canlı
smoke'da iki dönem eşleşti.

`--custody-analysis-only --symbol AKFIS --from-date 2026-09-14 --to-date
2026-09-23` yalnız bir analiz sorgusu planlar. 90 günü aşan aralıklar açık
`--allow-large-range` gerektirir. Smoke yerel istek aralığını ve sunucunun
`data_range.requested_from/requested_to/actual_from` alanlarını ayrı gösterir;
görülmeyen `actual_to` alanı hesaplanmaz.

`--custody-distribution-only --symbol AKFIS` yalnız bir dağılım isteği planlar;
tarih seçeneği almaz. Typed nested yanıt ve tek gerçek gönderim gereklidir.
Canlı deneme oturumun Mini App bağlantısını kapatabilir; yalnız kullanıcı
onayıyla ve Mini App kapalıyken çalıştırın:

```powershell
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py --allow-session-takeover --custody-distribution-only --symbol AKFIS
```

Kullanıcının bildirdiği son canlı dağılım smoke'unda `send completed`, bilinen
`custody_distribution_data` eventi ve smoke tamamlanması gözlendi. Sembol yalnız
yerel gönderilmiş istek provenance'ıydı; rapora ham yanıt, finansal değer veya
secret eklenmedi. Bu kullanıcı bildirimi, bağımsız yeniden test ya da sunucu
tarafı doğrulama iddiası değildir.

`--custody-stats-only --symbol AKFIS` yalnız tek `get_custody_stats`
isteği planlar; tarih ve history seçeneklerini kabul etmez:

```powershell
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py --allow-session-takeover --custody-stats-only --symbol AKFIS
```

Smoke başarısı için nested live typed yanıt ve aynı denemede gerçekten
tamamlanmış tek stats gönderiminin yerel provenance'ı gerekir. `request_id: null`
veya `cached: true` korelasyon sağlamaz; replay/legacy fixture ya da birden çok,
atlanan veya yapılmayan gönderim başarı sayılmaz. Oturum takeover uyarısı ve
onay gereksinimi bu komutta da geçerlidir.

`--akd-only` için `--symbol`, `--start-date` ve `--end-date` zorunludur; yalnız
bir `get_akd` planlar. Smoke, planlanan istekleri gerçek gönderimden ayrı
gösterir. Her deneme için yalnız izinli request tipi ve deneme numarasıyla
`send completed`, `send failed`, `rate limiter skipped` veya `not sent: no
top-level welcome` yazar. `send completed`, yerel `websocket.send()` çağrısının
başarıyla dönmesidir; sunucunun isteği kabul ettiğini veya yanıt ürettiğini
kanıtlamaz. Payload, initData ve ham URL yazdırılmaz.

Hata olduğunda deneme numarası ile `connect`/`receive`/`decode`/`parse`/`send`
aşaması ve sabit kategori gösterilir. Yalnız tanınmış event türü ve izinli şema
alan yolu eklenebilir; ham exception, response, close reason ve auth verisi
gösterilmez. Decode/parse/validation hataları yeniden denenmez; sınırlı retry
yalnız geçici transport hataları içindir. `send completed` ardından hata görülmesi
tek başına AKD yanıtı alındığını kanıtlamaz. Dedup koruması retry'da sürer.

Yalnız `custody_distribution_data` parse hatalarında güvenli shape özeti eklenir:
`event.data` ve `event.data.data` seviyelerindeki sabit allowlist alanlarının
yalnız varlık/null ve JSON tür kategorileri gösterilir. Alan değerleri, bilinmeyen
anahtar adları ve satır/date/code içerikleri özetlenmez; eksik alan `missing`,
explicit null ise `null` olarak ayrılır. Bu teşhis parse kurallarını gevşetmez
ve alternatif nesting için fallback anlamına gelmez.

`brokerages_data` parse hatasında ayrı ve daha dar bir özet üretilir. `event.data`
yalnız JSON tür kategorisiyle gösterilir; object ise yalnız `data`, `results`,
`date` alanlarının varlığı ve türü listelenir. `event.data.data` object ise aynı
allowlist uygulanır. String ise live parser en fazla 65.536 karakteri tek kez
JSON olarak çözer ve zaten çözülmüş değerden yalnız üst düzey JSON türünü (object
ise aynı allowlist anahtarlarını) tanılama nesnesine ekler; hata yolunda ikinci
bir parse yapılmaz. Bozuk metin için `invalid_json`, sınırı aşan metin için
`oversize` yazılır. Dizi satırlarında hata yolu sabit bir alan allowlist'i ve
`[index]` maskesi kullanır. İçerik/değerler, bilinmeyen alan adları, kurum
adı/kodu ve bağlantı/auth bilgisi tanılama çıktısına girmez. Live alan kontrolü
JS-derived candidate contract'tır; hata özeti tek başarılı smoke'un sınırını
genişletmez, tüm canlı varyantları doğrulamaz ve legacy parser'a fallback eklemez.

Analiz parser'ındaki tarih ve satır anahtarları teşhiste `[date]` ve `[index]`
olarak maskelenir. Doğrulanmış alan adında tür hatası varsa `expected`/`actual`
yalnız `null/string/number/boolean/array/object/date` gibi sabit kategorilerle
gösterilir. Kullanıcı tarafından bildirilen gözlemlerle `retail_pct` ve
`institutional_pct` finite JSON sayısı veya null olarak saklanır; sayısal
değerler `Decimal`dır ve ölçekleri dönüştürülmez. Alan yoksa `null_fields`
içine eklenmez. `investor_count` için null dışı değer hâlâ
`unsupported_metadata` olur. Bu gözlemler ölçek veya finansal anlam doğrulamaz;
`institutional_pct` kabulü, bildirilen `number` tipindeki canlı parse hatasına
karşı eklenmiştir.

`--max-frames` (varsayılan 8, en fazla 50), kontrol frame'leri dahil her bağlantı
denemesindeki toplam frame sınırıdır. Eski `--max-events` aynı seçeneğin uyumlu
alias'ıdır; yalnız piyasa event'lerini saymaz. `--timeout` (varsayılan 5 s,
en fazla 30 s) her receive için ayrı sınırdır, toplam oturum deadline'ı değildir.
Collector en fazla iki retry yapar; open/close timeout'ları da sınırlıdır.
Beklenen yanıt, bounded collection tamamlandıktan sonra değerlendirilir.
Varsayılan smoke tüm isteklerin tamamlandığını doğrulamaz; bunun için
`--require-all-responses` gerekir. Bu seçenek de sunucu tarafı request-id
korelasyonunu doğrulamaz. Canlı AKD/günlük Takas/dönem farkı/analiz için yerel
tek-gönderim bağlamı ayrıca gerekir; analizde yanıt sembolü de eşleştirilir.

Sembol snapshot'ları için tarihler açıkça verilmelidir:

```powershell
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py `
  --allow-session-takeover `
  --symbol ASELS `
  --start-date 2026-09-01 `
  --end-date 2026-09-22 `
  --custody-date 2026-09-22
```

Bu komut `get_brokerages`, `get_akd`, `get_custodian`,
`get_custody_distribution` ve `get_custody_stats` gönderir. Full custody history
varsayılan değildir. Yalnız açık istekle eklenir:

```powershell
.\.venv\Scripts\python.exe scripts\live_collector_smoke.py `
  --allow-session-takeover `
  --symbol ASELS `
  --start-date 2026-09-01 --end-date 2026-09-22 `
  --custody-date 2026-09-22 `
  --include-custody-history `
  --from-date 2026-09-01 --to-date 2026-09-22
```

90 günden uzun history aralığı ayrıca `--allow-large-range` gerektirir. Smoke
event type dışında raw response yazdırmaz ve persistent storage kullanmaz.

## Rate ve retry güvenliği

- Aynı canonical request anahtarı varsayılan 5 saniye içinde deduplicate edilir.
- Gönderimler arasında varsayılan en az 250 ms pacing uygulanır.
- Retry varsayılan olarak en fazla 2 yeniden denemedir.
- Backoff 0.5 saniyeden başlayıp iki kat artar ve 4 saniyede sınırlandırılır.
- Sonsuz reconnect iterator kullanılmaz.

## Snapshot assembler

`MarketSnapshotAssembler`, aynı sembolün depth, AKD, custody snapshot,
distribution, history ve stats bileşenlerini birleştirir. Her bileşenin kendi
tarih/zaman alanı korunur. Aynı bileşen için daha eski intrinsic tarih/zamanla
gelen veri reddedilir; yeni receive zamanı eski veriyi yeniymiş gibi göstermez.

## Güncel sınırlamalar

- Depth subscription request protokolü bilinmiyor; yalnız response parsing var.
- Trade flow ve chart response şemaları doğrulanmadı.
- Canlı smoke otomatik test suite'ine dahil değildir ve initData gerektirir.
- Collector veriyi diske yazmaz, checkpoint veya resume sağlamaz.
- Telegram login, browser automation ve auth yenileme yoktur.
- Persistent storage, retention, lisans ve provenance manifesti sonraki aşamadır.
