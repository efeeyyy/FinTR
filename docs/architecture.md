# FinTR-LLM architecture

## Tasarım ilkeleri

- Ham piyasa verisi, normalize kayıt, deterministik özellik ve LLM yorumu ayrı
  katmanlardır.
- Sembol, tarih/zaman ve mesaj türü dönüşüm boyunca korunur.
- Parser'lar eksik alanı veya yanlış tipi sessizce düzeltmez.
- AKD ve takas farklı zaman/kapsam semantiğine sahip veri aileleridir.
- Development pilotları bağımsız evaluation vakası olarak kullanılmaz.
- Model yorumu, repository içindeki veri toplama ve hesaplama kodundan ayrı
  yürütülür.

## Hedef akış

```text
WebSocket / raw market data
            |
            v
          parser --------------> açık şema/tip hataları
            |
            v
     normalized schema --------> stdlib dataclasses + symbol/date provenance
            |
            v
       feature engine ---------> deterministik, sürümlü formüller
            |
            v
       context builder --------> kaynak / hesap / eksik veri ayrımı
            |
            v
       bounded context -------> Efe + daha güçlü yönetici model
            |
            v
        evaluation ------------> insan incelemesi + rubric puanlaması
```

Bounded WebSocket collector uygulanmıştır. Kullanıcının canlı denemelerinde
handshake başarılıdır. Bildirilen 2026-09-24 toplu smoke'ta aynı denemede
brokerage, AKD, günlük Takas, Takas dağılımı ve Takas istatistikleri istekleri
gönderilmiş; beş yanıt da normalize edilip sıkı kabul kontrolünden geçmiştir.
Bu yalnız bildirilen oturumdaki transport/parser davranışını gösterir; tüm
sağlayıcı varyantları, finansal değerler veya isimlerin doğruluğu/lisansı
kanıtlanmış değildir. `tests/fixtures/market/` altındaki sanitize
mesajlar sentetiktir ve canlı sağlayıcı şemasının genelliğini kanıtlamaz.
Brokerage kanıt sınırı için [collector sınırları](collector.md) belgesine bakın. Kullanıcının
Mini App ve kısa collector denemelerinde gözlenen AKD/takas yapıları
[protokol envanterindedir](protocol_inventory.md); canlı typed yanıtların
feature/context katmanına bağlandığı anlamına gelmez.

## Modül sorumlulukları

- `fintr.data.models`: normalize ve immutable piyasa veri sözleşmeleri.
- `fintr.data.parsers`: mesaj türüne özgü doğrulama ve normalize etme.
- `fintr.collector`: runtime secret provider, bounded WebSocket/replay transport,
  event dispatcher, request builders ve freshness-aware snapshot assembler.
- `fintr.features`: LLM'den bağımsız sayısal özellik hesapları.
- `fintr.inference.context`: sınırlı, provenance taşıyan structured context ve
  deterministik text renderer.
- `fintr.inference.prompts`: model çağrısından bağımsız analysis prompt şablonu.
- `fintr.evaluation`: piyasa mikroyapısı rubric'i ve deterministik kontroller.

## Veri modelleri

Fiyat, maliyet ve yüzdeler `Decimal`; lot, değer ve yatırımcı adedi `int`; tarih
alanları `date`/`datetime` olarak normalize edilir. Koleksiyonlar immutable
`tuple` biçimindedir. `MarketSnapshot`, aynı sembole ait opsiyonel veri ailelerini
bir araya getirir; tarih/kapsam uyumunu context builder ayrıca doğrulayacaktır.

Bu paragraf mevcut repo modelini tarif eder. Yeni WS gözlemlerindeki kesirli
takas değerleri/lot_count, nullable seriler ve altı dönem bu modelle tam uyumlu
değildir. Günlük takasın fraction ölçeğiyle dönem farkının yüzde ölçeği ayrı
temsil edilmelidir; docs-only envanter güncellemesi modeli değiştirmemiştir.

### Kısmi canlı AKD sözleşmesi kararı

İlk kısmi kanıt sırasında problem: kullanıcı Mini App üzerinde `get_akd` için `akd_data` envelope'unu
gözlemledi; mevcut `akd_snapshot` fixture'ı bu canlı sözleşmeyi temsil etmiyor.
O aşamada tam kurum satırı ve sembol/dönem bağlantısı bilinmiyordu. Eski modele zorla
eşlemek alış/satış, maliyet ve tarih alanlarında kanıtsız değer üretirdi; event'i
unknown bırakmak ise doğrulanan bilgiyi kaybederdi.

Karar: bilinen envelope/alan tiplerini ayrı validator ile kontrol et;
dispatcher `known=True`, `normalized=None`, `normalization_pending=True` döndürsün.
Bu, tamamlanmış bir `AKDSnapshot` değildir; feature/context/assembler'a verilmez
ve tek başına smoke başarısı üretmez. Bilinmeyen alanlar yorumlanmaz, raw payload
ve request_id tutulmaz/loglanmaz. Tam şema gelene kadar eski snapshot parser'ı
offline sözleşme olarak korunur. Sonuç: event tanıma ile finansal normalizasyon
başarısı ayrılır; yeni bir piyasa veri modeli veya tahmini request eklenmez.

Sonraki gözlemde `as_of`, `source`, `brokerage/net/total` satırı ve ayrı abonelik
snapshot'ı belirlendi. Bu tarihsel kısmi kararın ardından sorgu yanıtı için
aşağıdaki ayrı typed model/parser eklendi; bağlamsız replay sonucu yine pending
kalır. Legacy `AKDSnapshot` ile birleştirilmedi.

### AKD sorgu yanıtı için ilk typed dilim

Problem: gözlenen `akd_data` alanları kısmi validator'da kayboluyor; buna karşılık
yanıtta sembol ve sorgu tarih aralığı yok. Eski `AKDSnapshot`'a dönüştürmek
kanıtsız alış/satış, tarih ve birim üretir. Karar: doğrulanmış alanları ayrı,
immutable `LiveAKDResponse` modelinde koru; sembol/başlangıç/bitiş yalnız aynı
bağlantı denemesinde gerçekten gönderilen **tek** `get_akd` isteği varsa ayrı
`local_sent_request` provenance'ına bağlansın. Birden çok, hiç veya önceki
retry'dan kalan istek yanıtı doğrulanmış bağlam sayılmaz. `request_id` sunucu
tarafı korelasyon kanıtı olarak kullanılmaz. Sonuç: parser/replay bağlamsız
AKD'yi typed fakat pending tutar; collector'ın güvenli bağladığı yanıt smoke
başarısı olabilir. Feature/context/assembler ve diğer piyasa aileleri değişmez.

### Collector hata sınıflandırması

Problem: her exception'ı retry etmek ilk decode/parse hatasını gizleyip ikinci
denemede rate-limit atlaması gibi yanıltıcı bir iz bırakabilir. Karar: yalnız
geçici transport hatalarını sınırlı retry kapsamına al; decode/parse ve
beklenmeyen programlama hatalarında hemen dur. Deneme, aşama, izinli kategori
ve doğrulanmış şema yolu dışında hata metni/payload raporlanmaz. Böylece ilk
başarısız deneme görünür, fakat yanıt alınmış olduğu kanıtsız iddia edilmez.

Dinamik tarih anahtarlı analiz hatalarında alan yolu yalnız sabit şema adları
ve `[date]`/`[index]` yer tutucularıyla gösterilir. Beklenen ve gelen JSON türü
parser'ın yapılandırılmış hata metadata'sından alınır; ham değer/hata mesajı
çıktıya aktarılmaz. Bilinmeyen metadata non-null varyantı ayrı kategori olarak
bildirilir; şema bu teşhis nedeniyle gevşetilmez.

### Günlük Takas canlı yanıtı

Gözlenen `custodian_data` yanıtı `event.data.data.results` içinde kesirli
değerler ve ayrı bir yanıt tarihi taşıyor; legacy fixture ise düz zarf, sembol
ve tam sayı değer bekliyor. Bu yüzden canlı yanıt ayrı immutable model/parser
ile işlenir. Legacy biçim yalnız açık düz zarf işaretleriyle seçilir; eksik
canlı alanlar legacy parser'a düşmez. Sembol ve istenen tarih yalnız aynı
bağlantıda gerçekten gönderilmiş tek `get_custodian` isteğinin yerel kaydıdır;
yanıt tarihi değiştirilmez. Birim veya yüzde dönüşümü yapılmaz.

### Takas dönem farkı canlı yanıtı

`custodian_diff_data` canlı yanıtı iç içe zarf, `start_date/end_date` ve kesirli
sayısal alanlar kullanır; legacy fixture düz zarf ile `first_date/last_date`
bekler. Canlı yanıt ayrı immutable model/parser'a ayrılır ve legacy biçim
yalnız açık düz zarf işaretleriyle seçilir. Yanıttaki dönem korunur; sorgudaki
sembol ve istenen dönem yalnız tek gerçekten gönderilmiş isteğin yerel
provenance'ıdır. Yüzde alanları günlük Takas alanlarına dönüştürülmez.

### Takas analizi canlı yanıtı

Gözlenen `custody_analysis_data` ağaç görünümü işlem dağılımı `akd[date]` ile
sahiplik bakiyesi `custody[date]` serilerini ayrı gösterir. Legacy `CustodyHistory`
tek satırda ikisini birleştirir ve canlı custody satırında görülmeyen `daily_net`
ile `cost` ister. Karar: bilinen alanları ayrı immutable canlı modelde tut;
gösterimdeki `…` kısaltmalarını veri sayma ve eksik gün doldurma. Metadata'da
yalnız doğrulanan null durumu modellenir; non-null varyant henüz şema dışıdır.
`data_range` için yalnız gözlenen `requested_from/requested_to/actual_from`
alanları yorumlanır, `actual_to` varsayılmaz. Canlı yanıtın sembolü, tek
gerçekten gönderilmiş isteğin yerel sembolüyle eşleşmeden başarı sayılmaz.

### Takas dağılımı canlı yanıtı

Gözlenen `custody_distribution_data` iç içe envelope, nullable/kesirli values
ve `periods` ile eşleşen fakat uzunluğu sabit olmayan slot dizisi kullanır;
legacy fixture ise beş integer slotlu düz şekildir. Karar: legacy parser/model ve
assembler davranışını değiştirme, canlı response için doğrudan ve DevTools
ağacının gösterdiği üçüncü-seviye payload yollarını ayrı immutable model/parser
ile doğrula. İki path aynı anda mevcutsa sessiz seçim yerine hata ver. Legacy
modeli nullable/değişken slotlarla genişletmek, mevcut feature/assembler'ın sabit
slot beklentisini belirsizleştirirdi. Sembol yanıtta bulunmadığı için yalnız tek
gerçekten gönderilmiş isteğin yerel provenance'ı eklenir; `request_id` üzerinden
korelasyon veya değer birimi/ufuk anlamı türetilmez.

### Takas istatistikleri canlı yanıtı

Gözlenen `custody_stats_data` canlı zarfı legacy `CustodyStats` biçiminden
farklıdır: `request_id` null olabilir, `cached` bulunur ve üç seri içteki
`data` nesnesinde görüntüleme amaçlı `format` metadata'sıyla taşınır. Bazı
gözlemlerin `value` alanı null'dır ve `label` null olarak görülmüştür. Karar:
legacy parser/model ve `features/stats.py` tüketim davranışını değiştirmeden,
canlı zarfı ayrı immutable `LiveCustodyStatsResponse` ve strict parser ile
doğrula. Format alanları olduğu gibi saklanır; sayılar yuvarlanmaz, ölçeklenmez
ve null sıfıra çevrilmez. Sembol ve `history` yalnız aynı bağlantı denemesinde
tamamlanan tek `get_custody_stats` gönderiminden yerel provenance olarak eklenir;
`request_id` veya `cached` yanıt korelasyonu sayılmaz. Birden çok/hiç gönderim
ve rate-limit atlamasında response pending kalır.

### Brokerage kod-etiket indeksi

Problem: Kullanıcının ilk bildirdiği başarılı `brokerages_data` yanıtı, mevcut
candidate typed parser'dan geçmişti; sonraki kullanımda kodu etikete çevirmek
gerekebilir. Tekil gözlemler tarih, kaynak lisansı, provider-wide tutarlılık veya
başka event'lerle eşdeğerlik sağlamaz.

Alternatifler: veriyi otomatik `MarketSnapshot`/context'e eklemek, kalıcı ve
güncel bir referans tablosu kurmak ya da yalnız verilen response için saf bir
in-memory index sunmak. İlk iki seçenek zaman/provenance ve saklama kararlarını
gerektirir.

Karar: `LiveBrokerageIndex` yalnız `LiveBrokeragesResponse` üzerinden kurulur ve
exact code lookup yapar. Bilinmeyen kod `unresolved`; aynı kod için aynı
title/short-title çifti yinelenirse tek eşleşme, farklı etiket çiftleri varsa
`ambiguous` döner. Kod/ad/etiket değerleri repr ve hata metinlerine çıkarılmaz.

Sonuç: resolver bağımsız, değiştirilemez ve process belleğiyle sınırlıdır; tarih,
lisans, alias/case normalizasyonu, cross-event eşleştirme veya doğruluk iddiası
eklemez. `MarketSnapshot`, feature, context ve persistent storage bağlantıları
bilinçli olarak ertelenmiştir.

### Canlı AKD kurum adı bağlantısı

Problem: `akd_data` kurum kodları taşır, fakat kurum adları ayrı bir
`brokerages_data` yanıtından gelir. Parser'a ad yazmak sayısal AKD verisini ve
bağımsız kaynakların sınırını karıştırır.

Alternatifler: AKD modelini doğrudan değiştirmek, eksik kodlar için ad tahmin
etmek veya iki typed yanıtı isteğe bağlı bir görünümde ilişkilendirmek.

Karar: `resolve_live_akd_brokerages` verilen tek `LiveAKDResponse` ile tek
`LiveBrokerageIndex` arasında exact-code, bellekte bir bağlantı kurar. Her
satır orijinal AKD satırını ve `resolved` / `unresolved` / `ambiguous` sonucunu
ayrı tutar. İsim yalnız kesin tek eşleşmede kullanılabilir; girdi yanıtları,
sıra, sayılar ve yerel istek provenance'ı değiştirilmez. Görünüm, hangi AKD
yanıtı ve hangi kurum indeksiyle üretildiğini referans olarak korur.

Sonuç: Bu yalnız AKD için çağrıldığında üretilen bir ara çıktıdır; collector,
kalıcı depolama, legacy `MarketSnapshot`, feature ve LLM context yoluna henüz
otomatik bağlanmaz. Dizin yanıtı tarihsiz olduğundan adın geçerlilik dönemi
veya sağlayıcı genelindeki doğruluğu iddia edilmez.

### Canlı takas kurum adı bağlantısı

Problem: `custodian_data` ve `custodian_diff_data` kurum kodları taşır; kurum
adları ayrı ve tarihsiz bir `brokerages_data` yanıtından gelir. AKD, günlük
takas ve dönem farkı farklı kaynak/zaman kapsamları olarak kalmalıdır.

Alternatifler: parser/model içine ad yazmak, eksik kodlara ad tahmin etmek veya
AKD'deki gibi ayrı, isteğe bağlı bir görünüm kullanmak.

Karar: `resolve_live_custodian_brokerages` ve
`resolve_live_custodian_diff_brokerages`, ilgili tek typed yanıtı verilen tek
`LiveBrokerageIndex` ile exact-code olarak ilişkilendirir. Her satırın sayısal
alanları, tarih/aralığı ve request provenance'ı orijinal kaynakta korunur.
`unresolved` ve `ambiguous` kodlara ad atanmaz; görünüm her iki girdiye de
referans tutar.

Sonuç: Günlük takas ve dönem farkı için bellekte, çağrıldığı zaman üretilen
etiketli ara çıktı vardır. Dizin yanıtının tarihsiz oluşu nedeniyle etiketin
ilgili takas tarihinde geçerli olduğu iddia edilmez. `Takas Analizi`, collector,
kalıcı depolama ve LLM context entegrasyonu bu kararın kapsamında değildir;
Takas Analizi için ayrı karar aşağıdadır.

### Canlı Takas Analizi kurum adı bağlantısı

Problem: `custody_analysis_data` içindeki tarihli `akd` ve `custody` serileri
ayrı `broker_code` alanları taşır. Bunları tek seriye toplamak veya kurum adını
parser'a yerleştirmek, farklı veri anlamlarını ve seyrek günleri gizler.

Alternatifler: iki seriyi birleştirmek, adları modele yazmak veya verilen tek
kurum indeksini her seriye ayrı ve isteğe bağlı bir görünüm olarak uygulamak.

Karar: `resolve_live_custody_analysis_brokerages` her `akd` ve `custody` gününü
orijinal sırayla ayrı tutar; her satır için exact-code eşleme sonucunu ekler.
Orijinal yanıt ve kurum indeksi referansları korunur. Eşleşmeyen ve çelişen
kodlara ad atanmaz; sayılar, boş günler, tarih aralığı, metadata ve istek
provenance'ı değiştirilmez.

Sonuç: Bellekte çağrıldığında üretilen bir Takas Analizi görünümü vardır.
Kurum listesinin tarihsiz olması nedeniyle adların tarihsel geçerliliği iddia
edilmez. Collector, kalıcı depolama, legacy snapshot, feature ve LLM context
akışları henüz bu görünümü otomatik kullanmaz.

### Beşli canlı koleksiyon kanıt görünümü

Problem: Aynı kısa collector çağrısındaki brokerage, AKD, günlük Takas, dağılım
ve istatistik yanıtları ayrı typed nesnelerdir. Legacy `MarketSnapshot` bunların
kesirli, nullable ve farklı tarih kapsamlarını kayıpsız temsil etmez.

Alternatifler: canlı yanıtları legacy snapshot'a zorla dönüştürmek, doğrudan LLM
context'e aktarmak veya yalnız mevcut typed nesnelere referans tutan bir ara
görünüm kurmak. İlk iki seçenek doğrulanmamış birim/tarih eşitliği üretir.

Karar: `build_live_collection_evidence`, tek `collect` sonucundan her aile için
tam bir normalize yanıt ister; eksik/çift/pending yanıtı, yerel gönderim
provenance'ı bulunmayan girdiyi, sembol uyuşmazlığını ve günlük Takas'ın istenen
ile dönen tarih farkını reddeder. AKD aralığı, dağılım dönemleri ve istatistik
serileri ayrı korunur; çapraz dönem eşitliği varsayılmaz. Kurum adları yalnız
mevcut exact-code, tarihsiz index üzerinden isteğe bağlı görünümde bulunur;
dağılımın `code` alanı kurum koduna çevrilmez.

Sonuç: Salt-okunur, bellekte kalan bir `LiveCollectionEvidence` vardır. Girdi
`DispatchResult` nesneleri bağlantı kimliği taşımadığı için çağıran tarafın tek
`collect` dönüşünü vermesi gerekir; fonksiyon bunu bağımsız kanıtlayamaz.
Bu görünüm kalıcı kayıt, feature hesabı, LLM context veya finansal doğruluk
onayı değildir.

### Kaydedilmiş dönem paketinin çevrimdışı analizi

Problem: Dönem paketi kaydedildikten sonra AKD mutabakatı, takas farkı
tutarlılığı ve AKD–takas zamanlama eşleşmesi elle ya da model sohbetinde
hesaplanıyordu. Bu hesaplar tekrar üretilemiyor, formülleri kayıtlı değildi ve
her analizde token harcıyordu.

Alternatifler: hesapları model sohbetinde sürdürmek, dönem paketini canlı
collector çıktısı sırasında genişletmek veya kaydedilmiş dosyayı okuyan ayrı,
ağsız bir aşama eklemek. İlk seçenek deterministik değildir; ikincisi canlı
oturumu uzatır ve eski kayıtları kapsamaz.

Karar: `fintr.data.period_capture` kaydı katı biçimde okur, sayıları `Decimal`
olarak korur ve paket hash'ini yeniden hesaplar. `fintr.features.period`
formülleri `period_features_v1` olarak uygular; `fintr.inference.period_report`
bunları sabit sınır metinleriyle JSON rapora çevirir. `scripts/analyze_period.py`
ağ, model veya secret kullanmaz; kaydetme isteğe bağlıdır ve üzerine yazmaz.

Sonuç: Aynı dosya ve parametreler aynı hesapları üretir. Model sohbeti sayıları
yeniden hesaplamak yerine raporu yorumlar. Rapor resmi bir fiyat/hacim serisi
içermez; gecikme eşleşmesi nedensellik veya sağlayıcı yöntemi kanıtı değildir.

#### Ek: günlük piyasa proxy'si (`period_features_v2`)

Problem: Kaydedilmiş dönem paketlerinde fiyat/hacim serisi yok, ama günlük AKD
satırlarında kullanılmayan `total_size`/`total_cost` alanları taşınıyordu;
bunların anlamı doğrulanmamıştı.

Alternatifler: alanları hiç kullanmamak (mevcut durum), doğrulanmamış anlamını
resmi bir fiyat serisiymiş gibi sunmak, veya doğrulanmamış bir hipotez olarak
açıkça etiketleyip iç tutarlılık kontrolleriyle birlikte türetilmiş bir proxy
sunmak. İkinci seçenek AGENTS.md'deki "kaynakta olmayan rakam üretme" kuralını
ihlal eder.

Karar: `fintr.data.period_capture` bu iki alanı da katı biçimde okur;
`fintr.features.period` (`period_features_v2`) bunlardan `derived_proxy_
not_official_ohlc` etiketli bir günlük proxy (`sum_total_size`,
`implied_traded_lots`, `vwap_proxy`) ve dört semantik kontrol (net toplamı
sıfır, toplam lot çift, |net| <= total_size, VWAP aralık içinde) üretir;
eksik güne 0 değil `None` yazılır. `fintr.inference.period_report` bunu ayrı
bir `daily_market_proxy` bölümünde, resmi seri olmadığını belirten bir
kısıtla birlikte taşır.

Sonuç: İki gerçek AKFIS kaydında dört kontrol de her gün geçti (bkz.
`scripts/analyze_period.py --all` çıktısındaki `proxy_checks_passed`), ancak
bu tek sembolün gözlemidir ve hipotezi kanıtlamaz; alan adları veya sağlayıcı
davranışı değişirse kontroller yeniden çalıştırılıp doğrulanmalıdır.

## Veri bölgeleri

- `tests/fixtures/market`: yalnızca sanitize edilmiş küçük protokol örnekleri.
- `data/raw`: gelecekteki değiştirilemez ham veri; Git dışında.
- `data/interim`: ara çıktılar ve geliştirme pilotları; Git dışında.
- `data/processed`: normalize araştırma verisi; Git dışında.
- `data/eval`: erişimi ayrılmış gold evaluation verisi; Git dışında.

Token, cookie, session, kimlik bilgisi, gerçek lisanslı veri, model ağırlığı ve
cache dosyaları Git'e eklenmez.

## Güncel hedef

Öncelik güncel veriyi almak, yanıtları ve yerel gönderilmiş istekleri provenance
kurallarıyla doğrulamak, ardından sınırlı analiz bağlamı hazırlamaktır. Efe bu
kanıtları daha güçlü yönetici modelle birlikte inceler. Repository model
ağırlığı yüklemez, yerel model eğitmez ve doğrudan model API çağrısı yapmaz.

Context builder ve authentication'dan ayrıştırılmış bounded WebSocket collector
offline testlidir; kullanıcı 2026-09-24'te beş aileyi aynı bağlantıda işleyen
ve bellek içi kanıt görünümünü doğrulayan tek bir canlı smoke çıktısı paylaşmıştır.
Ham yanıtlar kalıcı olarak saklanmadı. Diğer request/response varyantları,
kaynak doğruluğu ve lisans/provenance koşulları ayrıca doğrulanmalıdır. Telegram
authentication otomasyonu, bilinmeyen depth/trade request protokolleri ve
fiyat/işlem şeması ayrı mimari karar ister.

AKFIS/2026-09-23 için doğrulanmış beş aileli context yalnız development
pilotudur; prompt/çıktı geliştirmesinde kullanıldığı için held-out evaluation
değildir. İsteğe bağlı capture `data/interim/pilot/` altında kaynak, izin
beyanı, dönüşüm soyu ve checksum metadata'sıyla saklanır; ham frame ve session
bilgisi kaydedilmez. Bağımsız lisans doğrulaması yapılmamıştır. Canlı toplama
ve pilot sınırları [canlı context belgesindedir](live_context.md).

### Offline event-study/backtest engine

Problem: Kaydedilmiş AKD/takas/fiyat verisi üzerinde "bu koşul geçmişte
farklı bir ileri getiriyle mi ilişkiliydi" sorusu elle, tekrar üretilemez ve
lookahead riskiyle sorulabiliyordu; ayrıca bir tahmin/tavsiye aracına
kayabilirdi.

Alternatifler: (a) hesapları model sohbetinde/manuel not defterinde
sürdürmek -- deterministik değil, seed/versiyon kaydı yok; (b) koşulları
serbest metin/model çağrısıyla ifade etmek -- AGENTS.md'nin "no free-text,
no model calls" kısıtını ihlal eder ve tekrar üretilemez; (c) sinyal/koşul
tarihinden sonraki günün fiyat barını (veya "adjusted" seri sınırını
belirsiz tutarak) doğrudan kullanmak -- lookahead riski taşır; (d) koşullu
olay kümesini baseline'ın ayrık tümleyeni olarak tanımlamak yerine "tüm
günler" (üst küme) olarak tanımlamak -- görevin "baseline (all days same
symbols)" ifadesiyle daha doğrudan örtüşüyor, ayrık tümleyen alternatif
olarak değerlendirildi ve reddedildi (istenmedikçe ekstra bir varsayım
eklerdi); (e) sembol/dosya keşfini `scripts/analyze_period.py`'daki gibi
otomatik "kapsayan dosya" aramasıyla yapmak yerine CLI'da açık
`SEMBOL=capture,price` eşlemesi istemek -- ilk sürüm için daha küçük,
gözden geçirilebilir ve hangi dosyanın kullanıldığını belirsiz bırakmayan
bir arayüz.

Karar: `fintr.research` paketi (`study_spec.py`, `conditions.py`,
`features.py`, `engine.py`, `report.py`) ve `scripts/run_study.py` eklendi.
Koşul, kapalı bir JSON gramerinde (karşılaştırma + and/or/not + sabit +
sabit dört özellik adı) tanımlanır; serbest metin veya model çağrısı yoktur.
Her özellik, `t` gününe kırpılmış bir `PeriodCapture` kopyası
(`truncate_period_capture`) üzerinde çalışır -- `t+1` verisi çağrılan
fonksiyonun elinde hiç bulunmaz, bu yüzden bir koşulun `t+1` verisini
kullanması yapısal olarak imkânsızdır (parametre hatası değil, veri yokluğu).
Giriş, sinyal gününden sonraki ilk işlem gören (placeholder tatil barı
hariç) barın açılışı/kapanışıdır; çıkış `horizon` işlem günü sonrasıdır.
Tutma penceresinde bir düzeltme sınırı (`price_join.
adjustment_boundary_dates`, tüm fiyat serisi üzerinden) varsa ham
açılış/kapanış karşılaştırılabilir değildir; spec'in
`adjustment_boundary_policy`'sine göre her iki uçta `adjusted_close`
kullanılır ya da olay düşürülür -- ikisi de açıkça sayılır. Takas paneli
toplamında ölçek sıçraması (`detect_custody_scale_jumps`) tespit edilirse ve
bir koşulun girdisi bu sıçramayı kapsıyorsa olay düşürülür/sayılır.
Train/test ayrımı zaman kesimi (`train_test_cutoff`) ve isteğe bağlı sembol
hold-out listesiyle yapılır; hold-out semboller train'de hiç kullanılmaz,
test yalnızca spec `frozen: true` iken üretilir (`report.run_study`
aksi halde hata verir). Sonuç JSON'u spec SHA-256'sını, girdi dosya
hash'lerini, bootstrap seed'ini, kod commit'ini ve Python sürümünü taşır.

Sonuç: Araç yalnızca geçmiş istatistik üretir (olay sayısı, ortalama/medyan,
hit rate, baseline farkı, seed'li bootstrap güven aralığı, uygulanan işlem
maliyeti); al/sat önerisi veya kanıtsız manipülasyon/niyet iddiası
üretmez. `examples/study_specs/` altındaki üç örnek spec açıkça örnek
olarak işaretlidir, öneri değildir. Bootstrap güven aralığı float ile
hesaplanır (istatistiksel tahmin) ve kaynak Decimal getirilerden ayrı
tutulur. `custody_change` özelliği "yabancı takas" gibi bir sınıflandırmayı
doğrulamaz; yalnız spec'in verdiği kod kümesini toplar -- bağımsız
doğrulanmış bir takas sınıflandırması bu repo'da yoktur.

### Ön kayıt motor ön koşulları: E1 blok bootstrap, Holm ailesi, E4 birleştirme

Problem: `docs/prereg/` tasarımı birincil testi sinyal tarihine göre
hareketli blok bootstrap (E1) olarak tanımlıyor, motor ise yalnız i.i.d.
bootstrap üretiyordu (aynı gün hisseler arası ve örtüşen 5 günlük
pencerelerdeki bağımlılığı yok sayar). Holm ailesi yalnız belgede
yazılıydı; `run_study.py` sembol başına tek capture alıyor ve aynı sembol
tekrar verilirse sessizce üzerine yazıyordu; `--cost-bps` yardım metni
"round-trip" diyor, formül taraf başına uyguluyordu.

Alternatifler: (a) i.i.d. bootstrap'ı blok bootstrap ile değiştirmek --
eski sonuçlarla karşılaştırılabilirliği bozar; reddedildi, i.i.d. ikincil
tanılama olarak kaldı. (b) Blok bootstrap'ı sembol bazında yapmak --
aynı günün piyasa ortak şokunu hesaba katmaz; reddedildi, birim sinyal
tarihidir. (c) Dairesel/durağan bootstrap -- ön kayıt "hareketli blok"
diyor; en dar okuma olan dairesel olmayan hareketli blok seçildi. (d) Holm'u
her spec'i ayrı çalıştıran `run_study.py` içinde yapmak -- tek çalıştırma
ailenin diğer p'lerini bilmez; aile spec'lerin `preregistration`
bloğunda tanımlanır, karar kaydedilmiş sonuçlardan ayrı bir CLI ile
verilir. (e) E4'te kod bazında birleştirme veya boşluk doldurma --
kanıtsız varsayım ekler; reddedildi.

Karar: `study_spec` isteğe bağlı `block_bootstrap` bloğunu ve
`preregistration` bloğunun bir alt kümesini doğrular (varsayılan yok;
`preregistration` varsa `block_bootstrap` zorunlu). `engine.
moving_block_bootstrap` havuzlanmış olayları sinyal tarihine göre gruplar,
aynı tarihin tüm olaylarını birlikte çeker ve koşullu/baseline
ortalamalarını aynı çekilmiş tarihlerden hesaplar; tek yönlü p yalnız ön
kayıtlı yön için raporlanır. `report.build_hypothesis_test` birincil vade
özetini ve Holm girdisini (asgari olay sağlanmazsa 1) üretir;
`research.family.evaluate_primary_family` + `scripts/evaluate_study_family.py`
Holm'u yalnız `primary` üyelere uygular, `secondary_descriptive`
hipotezleri düzeltmesiz listeler. `research.capture_merge` aynı sembolün
capture'larını "daha geç `captured_at_utc` tarihin tamamını kazanır"
kuralıyla birleştirir; zaman ve dönem sırası çelişirse reddeder, ek yeri
boşluklarını ve girdi hash'lerini `capture_merge` bloğunda raporlar.
`--cost-bps` yardım metni ve belgeler taraf başına olarak düzeltildi;
formül değişmedi. `formula_version` `study_engine_v2` oldu.

Sonuç: Ön kayıtlı birincil test tekrar üretilebilir (seed, B, blok uzunluğu
spec'te ve sonuçta). Blok bootstrap bağımlılığı yalnız kısmen yakalar
(blok uzunluğu 5, vade 5 ile örtüşmeyi kapsar; daha uzun süren rejimleri
kapsamaz). E4 zaman/dönem sırası çelişkisinde veri yeniden alınmalıdır.
Lag feature'ları boşluk üzerinden bitişik tarih gibi okur; rapor edilir,
engellenmez.

### Dondurma öncesi veri kalitesi kuralı: ratio-regime sinyal günü düşürme

Problem: `docs/prereg/DECISIONS.md`'de kaydedildiği gibi, yönetici oturumunda
yalnız AKD-türevi lot / Yahoo hacim oranına bakılarak (getiri okunmadan) 20
geliştirme hissesinden birinde (BIMAS) 159 günlük bir dönemde oran tam 0,5
bulundu: Yahoo'nun geriye dönük 1'e 2 ölçek düzeltmesi ile AKD'nin
düzeltilmemiş lotları arasında birim uyumsuzluğu. `fintr.features.
price_join.join_price_history` bunu zaten `ratio_regime`
(`probable_retroactive_price_adjustment`, tam sayılı k >= 2, `vwap_proxy/k`
ile korobore edilmiş) olarak tespit ediyordu, ama motor
(`fintr.research.engine`/`report.run_study`) bu tespiti hiç okumuyordu: bir
rejim içindeki günler sinyal olarak koşullu ve baseline kümelerine
düzeltilmemiş oranla karışık birimde giriyordu.

Alternatifler: (a) rejim içindeki günlerde getiriyi yeniden ölçeklemek --
AGENTS.md'nin "hesaplanan değerleri kaynak değerlerden ayır" ve "kaynakta
olmayan rakam üretme" kurallarını ihlal eder, ayrıca hangi seri
(AKD lot mu, Yahoo hacim mi) "doğru" kabul edileceğine dair kanıtsız bir
varsayım gerektirir; reddedildi. (b) yalnız exit penceresi rejime denk gelen
olayları düşürmek -- getiriler Yahoo'nun kendi içinde tutarlı serisinden
hesaplandığından (fiyat kendisi yeniden ölçeklenmiyor, yalnız AKD/hacim
oranı bozuluyor) gereksiz veri kaybı yaratırdı; reddedildi. (c) rejimi
sessizce yok saymak -- veri kalitesi sorununu gizler; reddedildi.

Karar: `fintr.research.engine.load_symbol_data` artık `join_price_history`'nin
`ratio_regime_tolerance` parametresiyle (varsayılan
`fintr.features.price_join.DEFAULT_RATIO_REGIME_TOLERANCE`, `run_study.py`'de
açık bir CLI parametresi) ürettiği `ratio_regimes`/`ratio_regime_dates`'i
`SymbolData`'ya taşır; `report.run_study`,
`engine.drop_ratio_regime_signal_dates` ile her sembolün rejim içindeki
sinyal günlerini hem koşullu hem baseline kümesinden, olay inşasından önce
düşürür. Getiri formülü değişmedi -- yalnız hangi sinyal günlerinin
değerlendirildiği değişti. Sonuç `ratio_regime_quality[SEMBOL]` altında
bulunan rejimleri (start/end/k/day_count) ve düşülen sinyal günü sayısını
raporlar; metin çıktısı aynı bilgiyi sembol bazında yazar. `formula_version`
`study_engine_v3` oldu.

Sonuç: Rejimsiz semboller/dönemler öncekiyle birebir aynı sonucu üretir
(boş rejim listesi, 0 düşülen gün). Bir rejim tespit edildiğinde onun
içindeki sinyal günleri hem birincil hem ikincil analizden çıkar; bu,
gücü azaltabilir ama birim uyumsuzluğunu sessizce sonuca karıştırmaz.
Tolerans gevşetilir/sıkılaştırılırsa hangi günlerin rejime girdiği değişir;
bu her koşuda sonuçta açıkça yazılır.

### Kurumsal aksiyon kaydı, düzeltilmiş görünüm ve sürümlü rapor adlandırması

Problem: Bir bedelsiz/split gibi kurumsal aksiyon geçmiş AKD/takas
serilerinin birimini (lot sayısını) değiştirir; bunu görmezden gelen bir
analiz, aksiyon öncesi/sonrası günleri karışık birimde karşılaştırır. Olgu
kap.org.tr'den bu oturumda ağ erişimi engellendiği için otomatik çekilemez;
elle girilen bir olgunun da provenance'sız, sessizce mevcut rapor
formülüne karışmaması gerekir. Ayrıca yeni bir formül sürümü veya kayıt
içeriği değişikliği, eski bir raporu sessizce geçersiz kılmamalı.

Alternatifler: (a) kurumsal aksiyonu `--price-history-root` gibi bir kökten
sembole göre otomatik keşfetmek -- görev açıkça "no auto-loading, no hidden
default" istedi ve bir kaydın sembole özgü olması `--all` toplu modunda
belirsizlik yaratırdı; bunun yerine `--corporate-actions PATH` yalnızca tek
kayıt modunda, `--price-history`'nin aynı kısıtlamasıyla eklendi. (b)
düzeltilmiş değerleri ham bölümlerin üzerine yazmak -- AGENTS.md'nin
"hesaplanan değerleri kaynak değerlerden ayır" kuralını ihlal eder; bunun
yerine tamamen ayrı bir `adjusted_view` bölümü eklendi. (c) düzeltilmiş
seriler için `broker_flow_series`/`lagged_akd_custody_match`/
`check_custodian_diff`'i yeniden yazmak -- yerine, çarpanla ölçeklenmiş
alanlarla yeni bir `PeriodCapture` (`dataclasses.replace`) inşa edip aynı
`fintr.features.period` fonksiyonlarını bu üzerinde çağırmak seçildi:
tekrarı önler ve iki yol arasında formül sürüklenmesi riskini ortadan
kaldırır. (d) rapor dosyalarını sürümsüz `<stem>.report.json` olarak
bırakmaya devam etmek -- yeni bir formül sürümü eski raporu sessizce
geçersiz bırakırdı; bunun yerine dosya adına
`period_features_version`/`price_join_version`/kayıt SHA-256'sının ilk 8
hex karakteri eklendi (bkz. `docs/features.md`), eski dosyalara asla
dokunulmaz.

Karar: `src/fintr/data/corporate_actions.py` (şema
`fintr_corporate_actions_v1`, katı yükleyici: pozitif olmayan çarpan,
`custody_effective_date < ex_date`, tekrarlayan/çakışan aksiyon reddedilir)
ve `scripts/register_corporate_action.py` (her alan zorunlu argüman, atomik
ekleme, tekrarlayanı reddet, kaynak dosya yerel olarak hash'lenir, hiçbir
zaman yüklenmez) eklendi. `src/fintr/features/period_adjusted.py`
(`FORMULA_VERSION = "period_adjusted_v1"`) çarpanı `ex_date`/
`custody_effective_date`'e göre kümülatif uygular ve
`check_corporate_action_consistency` ile aksiyonu ham veriye karşı test
eder (geçen/başarısız sayım). `period_report.py`'daki `adjusted_view`
her sayıyı formül + `action_id` + çarpanla etiketler.
`scripts/analyze_period.py`'ye `--corporate-actions PATH` (tek kayıt
modu) ve `report_filename()` ile sürümlü adlandırma eklendi;
`--all` modu bu bayrağı kabul etmez.

Sonuç: Ham bölümler hiçbir zaman değişmez; `adjusted_view` yalnızca bir
kayıt verildiğinde ve o kaydın sembolü capture'ınkiyle eşleştiğinde
hesaplanır, aksi halde `not_available`/`symbol_mismatch`/`no_actions`
durumlarından biriyle açıkça raporlanır. Geçen bir tutarlılık kontrolü,
girilen aksiyonun doğruluğunun bağımsız kanıtı değildir -- yalnızca bu
formülle uyumlu olduğunu gösterir; bu sınır hem kodda hem
`docs/corporate_actions.md`'de tekrarlanır.

### Dondurulmuş hisse ayrımı ve dışlama korumaları

Problem: BIST 30 holdout hisseleri (`configs/splits/`) ve kullanıcı kararıyla
dışlanan hisseler (`configs/exclusions/symbols.json`) yalnız belgelerde
kalırsa bir analiz, çalışma veya eval doğrulaması onları sessizce
kullanabilir; ayrım da (v1 → v2) yenilenebildiği için sabit bir dosya adı
güvenilir değildir.

Alternatifler: (a) her script'e `--split-file`/`--exclusions-file` bayrağı --
ilk WIP böyleydi, ama bir bayrak etkin ayrımı atlamaya veya boş bir dışlama
dosyasıyla dışlamayı kaldırmaya izin verir; reddedildi. (b) eksik
işaretçi/dışlama dosyasında korumayı kapatmak (cwd'ye göreli varsayılanlar) --
başka bir dizinden çalıştırınca koruma sessizce kapanıyordu; reddedildi.
(c) toplamayı da holdout için engellemek -- holdout verisinin toplanması
gereklidir (tek seferlik değerlendirme için); yalnız kullanım korunur.

Karar: `fintr.evaluation.symbol_guard.load_active_guards` etkin ayrımı
yalnız `configs/splits/current.json` üzerinden, dışlamaları yalnız
`configs/exclusions/symbols.json`'dan, repo köküne göre mutlak yollarla ve
kapalı-güvenli yükler; `frozen: false` veya `superseded_by` taşıyan ayrım
etkin olamaz. Analiz/çalışma/eval araçları holdout ve dışlama kurallarını,
toplama araçları yalnız dışlamayı uygular. `--no-split-guard` yalnız holdout
kontrolünü kapatır ve çıktıda belirgin biçimde raporlanır; dışlamanın
geçersiz kılma seçeneği yoktur. `run_study --split test` motoru
`symbol_scope="holdout_only"` ile çalıştırır.

Sonuç: Her çıktı uygulanan ayrım ve dışlama dosyasını SHA-256'larıyla
kaydeder. Bozuk/eksik işaretçi, ayrım veya dışlama dosyası tüm korunan
araçları durdurur (bilinçli). Kurallar ve tablo:
[`docs/evaluation_protocol.md`](evaluation_protocol.md).

### Çevrimdışı gözlem tarayıcısı (`fintr.scanner`, aşama 2 adım 1)

Problem: Ön kayıtlı H1–H4 dondurulduktan sonra (`docs/prereg/FREEZE.md`),
kaydedilmiş bir günün hangi geliştirme hissesinde hangi koşulu sağladığını
görmenin tek yolu `run_study.py`'yi elle çalıştırıp istatistiksel çıktıyı
yorumlamaktı -- bu hem tek bir günü sormak için gereğinden ağır hem de
getiri/istatistik üretimiyle "bu gün şu koşulu sağladı" gözlemini aynı
çıktıda karıştırıyordu, ki AGENTS.md'nin yatırım tavsiyesi/al-sat sinyali
üretmeme sınırına yakın bir alan.

Alternatifler: (a) yeni bir kural dili (serbest metin veya ayrı bir DSL) --
`fintr.research.conditions`'ın kapalı JSON gramerini tekrar icat eder,
AGENTS.md'nin "no free text, no model call" kısıtını yeniden kanıtlamayı
gerektirir ve iki paralel koşul temsilinin sürüklenmesi riskini doğurur;
reddedildi. (b) mevcut `condition` grammar'ını olduğu gibi kullanmak, ama
tarayıcıya kendi eşik/parametrelerini eklemesine izin vermek -- ön kayıt
disiplinini (AGENTS.md "kaynakta olmayan rakam üretme", `docs/prereg/
FREEZE.md`) ihlal eder: tarayıcı sonradan ayarlanabilir bir eşikle "en iyi
sonucu bulana kadar dene" aracına dönüşebilirdi; reddedildi. (c) mevcut
grammar'ı, yalnız dondurulmuş spec dosyalarının `condition` alanından
okuyarak, hiçbir yeni eşik eklemeden kullanmak -- seçildi.

Karar: Yeni paket `src/fintr/scanner/` (`rules.py`, `freshness.py`,
`observe.py`, `report.py`) ve `scripts/scan_observations.py` eklendi. Tazelik
raporunun takas gecikmesi (T+2) koddan değil tarih-etkin
`configs/market/settlement_cycle.json`'dan okunur (2026-10-14). Hiçbir
formül yeniden yazılmadı: `fintr.research.conditions.evaluate_condition`,
`fintr.research.features.truncate_period_capture`/`evaluate_feature`,
`fintr.research.engine.load_symbol_data`/`detect_custody_scale_jumps`/
`drop_ratio_regime_signal_dates`/`tradeable_price_bars` ve
`fintr.evaluation.symbol_guard.load_active_guards` doğrudan çağrılır.
`fintr.scanner.rules.load_scanner_rules`, `configs/scanner/rules_v1.json`
içindeki her kuralın `docs/prereg/FREEZE.md`'deki ham dosya ve kanonik
`spec_sha256` hash'lerini spec dosyasının disk üzerindeki hâliyle karşılaştırır
ve `frozen: false` ya da hash uyuşmazlığında açık hata verip çalışmayı
reddeder -- `run_study.py --split test`'in dondurma kapısıyla aynı disiplin,
ama her çalıştırmada, tek bir günü sormak için bile uygulanır. Çıktı şeması
(`fintr_scanner_observations_v1`) kasıtlı olarak `score`/`rank`/`return`/
`target` alanı içermez (`tests/test_scanner.py::ForbiddenFieldsTests` bunu
doğrular); her gözlem sabit `validation_status:
"preregistered_hypothesis_holdout_test_pending"` ve sabit Türkçe uyarı metni
taşır. `run_study` ile aynı veri kalitesi kuralları (ratio-regime, takas
ölçek sıçraması) gözlem seviyesinde `suppressed` alanıyla raporlanır;
placeholder (tatil) bar hiçbir zaman kapanış sağlamaz
(`tradeable_price_bars` zaten hariç tutar). Hisse kapsamı kod ile zorunlu:
holdout hissesi her zaman reddedilir (`--no-split-guard` seçeneği yoktur --
`run_study.py`/`analyze_period.py`'nin aksine, tarayıcının burada bir istisna
kapısına ihtiyacı yok), dışlanan hisse her zaman reddedilir.

Sonuç: Tarayıcı yalnız kaydedilmiş veriyi okur (ağ, Telegram, model çağrısı
yok; `data/interim/studies/` hiç okunmaz) ve yalnız gözlem bildirir --
`fintr.research`'ün ürettiği istatistiksel bulgudan (ortalama fark, bootstrap
CI, Holm) tamamen ayrıdır ve onun yerine geçmez. H1–H4 hâlâ holdout testini
bekliyor; bir kuralın `condition_met: true` dönmesi ön kayıtlı hipotezin
doğrulandığı anlamına gelmez, yalnız o günün ön kayıtlı koşulu sağladığı
anlamına gelir. `docs/scanner.md` şema, sınır ve komut örneklerini taşır.

### Tek seferlik holdout testi: test planı, birincil/ikincil kapsam ve kilitli yürütücü

Problem: `docs/prereg/README.md` 6. madde ile `holdout-guard` dalıyla gelen
CLI koruması çelişiyordu (motor tanımı 30 sembol/`symbol_scope="declared"`
diyordu, CLI yalnız `symbol_scope="holdout_only"` çalıştırıyordu) --
dondurmadan önce Efe'nin karara bağlaması gereken açık bir soru olarak
işaretlenmişti (`docs/prereg/README.md` "Efe'nin kararları" 4). Ayrıca
Nisan 2027'deki gerçek çalıştırmanın (a) yanlış spec/parametreyle, (b)
pencere sonrasına taşan sinyal tarihleriyle veya (c) ikinci kez
çalıştırılmasıyla tekrar edilemez hale gelmemesi gerekiyordu.

Alternatifler: (a) kapsam çelişkisini `run_study.py`'ye sabit kodlamak
(yalnız `holdout_only` ya da yalnız `declared`) -- README'deki iki tanımdan
birini sessizce geçersiz kılardı; reddedildi. (b) test penceresi bitişini
yalnız operatör disipliniyle sınırlamak (E3'ü "önerilir" durumunda
bırakmak) -- pencere sonrası veri toplanınca sinyal tarihi sınırı elle
uygulanmak zorunda kalırdı, kolayca unutulur; reddedildi. (c) tek seferlik
çalıştırmayı yalnız bir "elle bir kez çalıştır" notuyla belgelemek, kilit
dosyası olmadan -- iki paralel çalıştırma ya da bir hatadan sonra yanlışlıkla
tekrar çalıştırma riski taşırdı; reddedildi.

Karar: 2026-09-28 (`docs/prereg/DECISIONS.md`), Efe onayı, yönetici önerisi
üzerine -- **birincil** test motorun `--split test` tanımını kullanır (30
sembolün tamamı, `symbol_scope="declared"`; Holm yalnız bu sonuçlara
uygulanır), `holdout-guard`'ın `symbol_scope="holdout_only"` davranışı
**ikincil** (E5, sağlamlık) kapsam olarak korunur. `scripts/run_study.py
--split test` artık `--test-plan`/`--test-scope` (`primary`|`secondary`)
ister; ikinci bir pre-registration artefact'ı olan `fintr_test_plan_v1`
(`configs/studies/prereg/test_plan_v1.json`, `fintr.research.test_plan`)
her spec'in dondurma zamanındaki ham ve kanonik hash'ini taşır ve
`--spec` dosyası byte-byte eşleşmezse çalıştırma reddedilir. Pencere bitişi
(E3) `engine.signal_dates_for_split`'in yeni `window_end` parametresiyle
motor seviyesinde zorlanır (train etkilenmez). Tek seferlik yürütme
`scripts/run_holdout_test.py`'dedir: varsayılan `--dry-run` altı denetimi
(plan/spec hash, git temiz ağaç, pencere-sonrası dönem kaydı, 25 tradeable
bar, AKD/fiyat gün kapsaması, kilit/önceki çıktı yokluğu) yalnız raporlar,
hiçbir dosya yazmaz; `--execute` önce aynı denetimleri geçmeyi şart koşar,
sonra `data/interim/studies/test/LOCK`'u atomik (`O_EXCL`) oluşturur --
kilit varsa manifest tamamlanmış olsa bile reddeder, yarım kalmış bir
manifest'te durup Efe kararı ister, otomatik yeniden denemez. Stdout'a
yalnız dosya yolu ve durum yazılır; getiri/p-değeri yalnız kaydedilen
JSON'dadır.

Sonuç: Test planının SHA-256'sı ve spec hash'leri her `--split test`
sonucunda (`test_plan_path`, `test_plan_sha256`, `test_scope`) ve
manifest'te kayıtlıdır. `fintr.research.family.evaluate_primary_family`
`test_scope`'un aile genelinde tutarlı olmasını zorlar ve doğrulayıcı
kararı yalnız `test_scope="primary"` (ya da alan hiç yoksa) için verir --
`secondary` hiçbir zaman tek başına doğrulayıcı değildir. Fiyat geçmişinin
pencere öncesini de içeren tam serisinin düzeltme sınırı/ratio-regime
tespitinde kullanılmaya devam etmesi (motorun zaten var olan, train'de de
geçerli davranışı) bilinçli olarak değiştirilmedi ve
Efe 2026-09-28'de bunu onayladı (DECISIONS.md "Holdout fiyat geçmişi kararı"): tam geçmiş yalnız bu veri kalitesi tespiti için kullanılır.

### Bildirim kanalı: Telegram (`fintr.notify`, aşama 2 adım 2)

Problem: `docs/scanner.md`'deki gözlem tarayıcısı çıktıyı yalnız
`data/interim/scanner/<as_of>/observations.json` dosyasına yazıyor; Efe'nin
bunu her seferinde elle açması yerine, gözlem üretildiğinde kendisine
otomatik olarak ulaşan bir kanal gerekiyordu.

Alternatifler: (a) e-posta (SMTP) -- kurulum için bir SMTP sağlayıcısı/uygulama
şifresi gerektirir, mobil bildirim gecikmeli olabilir; (b) yerel bildirim
(masaüstü toast/dosya izleyici) -- yalnız Efe'nin makinesi açıkken çalışır,
bulut oturumundan (`claude.ai/code`) tetiklenemez, mobilde görünmez.

Karar: 2026-09-28 (Efe) -- Telegram Bot API (`sendMessage`, düz metin).
Gerekçe: Efe'nin zaten kullandığı, mobilde anlık bildirim veren, kurulumu
tek bir bot token'ı ve chat id'den ibaret bir kanal; yalnız standart
kütüphane (`urllib`) ile, yeni bağımlılık eklemeden çağrılabiliyor. Sürekli
veri toplama için daha önce reddedilen Telegram Mini App yaklaşımından
(bkz. `docs/STATUS.md` "Amaç ve sınırlar" -- tek oturum, 429 hız sınırı)
farklı: burada Telegram yalnız çıkış kanalı, veri toplama veya AKD/fiyat
kaynağı değil.

Sonuç: `src/fintr/notify/format.py` (saf biçimlendirme) ve
`src/fintr/notify/telegram.py` (yalnız `urllib` ile `sendMessage`),
`scripts/notify_observations.py` (varsayılan dry-run, `--send` ile gerçek
gönderim, token/chat id yalnız `FINTR_TELEGRAM_BOT_TOKEN`/
`FINTR_TELEGRAM_CHAT_ID` ortam değişkenlerinden). Bildirim de tarayıcı gibi
bir gözlem bildirimidir -- getiri, hedef, puan, sıralama, stop veya serbest
metin al/sat önerisi üretmez; her mesajın sonunda sabit uyarı metni ve
`validation_status` bulunur. Tek istisna (Efe kararı, 2026-10-02, yalnız kişisel
kullanım; AGENTS.md "Güvenlik sınırları", `docs/prereg/DECISIONS.md`): her
`condition_met` satırı, dondurulmuş spec'in hash doğrulamalı `predicted_direction`
alanından türetilen "beklenen yön: yukarı (al yönlü)" / "aşağı (sat yönlü)" etiketini
doğrulanmamış hipotez uyarısıyla taşır; kural/eşik/spec ve tarayıcı şeması değişmez,
çözülemeyen yön "yön bilinmiyor" olur, `--no-direction-label` önceki metni geri getirir.
Alternatif (etiketi holdout sonucuna kadar bekletmek, önceki plan) Efe tarafından
reddedildi. Ayrıntılar `docs/notify.md`.

### Bildirim düzeni v2: özet + hisse başına mesaj (`fintr.notify.layout`)

Problem: Efe'nin ilk gerçek Telegram mesajı (2026-10-03) tek uzun metindi: 30
basamaklı ham değerler, birbirinin aynısı 20 satırlık "veri durumu" listesi,
teknik kural cümleleri ve uzun dipnotlar; okunmuyordu.

Alternatifler: (a) v1 metnini yalnız kısaltmak -- hisse başına bilgi yine tek
blokta kalır; (b) Telegram HTML biçimi -- kalın/başlık sağlar ama tüm dinamik
metnin escape'i ve ek hata yüzeyi gerekir, düzen sorununu çözmez; (c) özet
mesajı + koşulu sağlayan her hisse için ayrı mesaj, düz metin (seçilen).

Karar (Efe onayı, 2026-10-03 görev notu): varsayılan düzen v2; `--layout v1`
önceki tek düz metni birebir korur. Ham değer mesajda gösterilmez, yüzdeye
çevrilir (formül `docs/notify.md`); "Kural"/"Ne demek" metinleri çalışma
anında üretilmez, Git'teki `configs/notify/rule_explanations_v1.json` dosyasından
gelir ve dosyanın SHA-256'sı receipt'e yazılır. Çok mesajlı gönderim sırayla,
mesajlar arası bekleyerek (`configs/notify/delivery_v1.json`) yapılır;
kısmi gönderim receipt'te mesaj bazında kaydedilir ve aynı `as_of` yeniden
çalıştırılınca yalnız gönderilmemiş mesajlar gönderilir (receipt'ler
`O_EXCL` ile yalnız oluşturulur, hiçbir zaman yeniden yazılmaz).

Sonuç: `layout.py` (saf render, I/O yok), `explanations.py` (sabit metin
yükleyici + yasak ifade koruması `guard.py`), `delivery.py` (plan/gönderim/
receipt), `scripts/notify_observations.py` yalnız bayrak ve bağlantı
ekler. Tarayıcı çıktı şeması, kurallar, eşikler, `--resend` ve token/chat id
redaksiyonu değişmez. Bilinen risk: Telegram'a yanıt alınamadan kabul edilmiş
bir mesaj (zaman aşımı) kayıtsız kalır ve devam çalıştırmasında bir kez daha
gidebilir. Ayrıntılar `docs/notify.md`.

Değişiklik (Efe kararı, 2026-10-03, `notify-layout-v2-pct`): ilk v2 sürümü
yüzdeyi `-v/(1+v)*100` ile kapanışın VWAP proxy'ye göre konumu olarak
gösteriyordu; bu, dondurulmuş özellikten (`vwap_proxy_vs_close`,
`docs/prereg/H1.md`) ayrı bir türev rakamdı ve kural eşiğini "~%1"e
yuvarlatıyordu. Karar: yüzde doğrudan dondurulmuş özelliktir, `|v|*100`,
özne VWAP proxy ("Ortalama fiyat (VWAP proxy) kapanışın %1,53 altında/üstünde");
kural satırları spec'teki eşiği ("en az %1") ve bandı ("en çok %10") aynen
okur. Alternatif (eski formülü korumak) reddedildi: mesajdaki rakam spec'teki
eşikle aynı ölçekte olmalı. Yön etiketi AGENTS.md ifadesiyle birebir ve küçük
harflidir. `--layout v1`, `--no-direction-label`, `--llm-note-dir`, receipt v2,
`--resend` ve redaksiyon değişmedi. Eski mesajlarla karşılaştırılırken rakamlar
aynı değildir (ör. `v = 0,015` eskiden "%1,48", şimdi "%1,50").

### Günlük Mini App toplama ve birleştirme (aşama 2 adım 3)

Problem: Tarayıcı ve bildirim botu her işlem günü güncel AKD, takas ve fiyat
verisine ihtiyaç duyuyor; oysa veri yalnız elle başlatılan, uzun aralıklı
dönem kayıtlarıyla geliyordu. Eksik olanlar: günlük plan ve eksik gün
telafisi, takasın AKD'nin gerisinde kesinleşmesi (T+2) ve sağlayıcı
revizyonları, kısa kayıtların uzun geçmişle birleştirilmesi (tarayıcının
"en son biten tek dosya" seçimi kısa bir dosyayla yıllık dosyayı gölgelerdi),
tek oturum/429 sınırı ve çalıştırma kaydı. Tam gerekçe ve şemalar:
[`docs/research/daily_miniapp_collection_adr.md`](research/daily_miniapp_collection_adr.md).

Alternatifler: (a) yeni günlük kayıt şeması ve ikinci bir yazıcı/yükleyici --
doğrulanmış paket yolunu yeniden yazmayı gerektirir; reddedildi. (b) her gün
tek büyük dönem kaydını yeniden çekmek -- günlük yük ve 429 riski en yüksek,
90 gün üstü aralık onayı ister; reddedildi. (c) kayan pencereli dönem
kayıtları + deterministik birleştirici -- seçildi.

Karar: 2026-09-28, Efe kararları K1-K5 (ADR "Efe'nin kararları"). **Aşama A**:
Efe her işlem günü (T+1 08:00-09:30, Europe/Istanbul, kendi Windows
makinesi) `initData`'yı bir kez girer, tek komut tüm hisseleri insansız
toplar; `initData` yalnız bellekte tutulur. Mevcut `fintr_period_analysis_
capture_v1` şemasıyla `E = R-1`, `S = E-14` takvim günü kayan pencereler
`data/interim/daily_capture/<SYM>/` altına yazılır (no-overwrite, telafi
için `S` son başarılı pencereden geriye genişler; 90 gün üstü insan kararı
ister). Saf `fintr.research.compose_period_capture.compose_period_capture`
temel ve kayan kayıtları tarih x aile bazında birleştirir (en yeni
`captured_at_utc` kazanır; boşluk asla doldurulmaz, revizyon ve
kesinleşmemiş günler `CompositionLineage`'e yazılır; dönem toplamları
birleştirilemez). Kapsam yalnız 20 geliştirme hissesi; holdout hisseleri
günlük toplanmaz, DSTKF hiçbir zaman. Mini App kaynağının lisans/izin
bilgisi Efe'nin beyanıdır (`user_asserted_2026-09-28_not_independently_
verified`); bağımsız olarak doğrulanmamıştır.

Bileşenler:

- `scripts/collect_daily.py`: planlayıcı (`--dry-run`: ağ yok, `initData`
  istemez) ve canlı çalıştırıcı (`--allow-session-takeover` ister). İlk
  `handshake_rejected_http_429`'da kalan hisseleri denemeden durur;
  her çalıştırma `daily_capture/_runs/<run_id>.json`
  (`fintr_daily_collection_run_v1`; finansal değer, kurum kodu, `initData`,
  URL yok) yazar. İkinci adım Yahoo fiyat geçmişidir (`--skip-price` ile
  atlanır); "bugün" Europe/Istanbul tarihidir. Ayrıntı `docs/collector.md`.
- `scripts/compare_daily_d0.py`: günlük kayıtların yıllık temel kayıtlarla
  revizyon karşılaştırması; kesme tarihi sabit 2026-09-25, yalnız sayı
  raporlar (değer ve kurum kodu yok).
- `scripts/scan_observations.py --daily-root`: varsayılan kapalı; verilirse
  sembol verisi birleştiriciden gelir, çıktı şeması
  `fintr_scanner_observations_v2` olur ve pencereyle sınırlı
  `composition`/`data_status` sayımlarını taşır (`docs/scanner.md`).
- `scripts/notify_observations.py`: `_v2` dosyalarında sembol başına tek
  satırlık veri durumu (boşluk / kesinleşmemiş / revize gün sayısı) ekler
  (`docs/notify.md`).

Veri bütünlüğü kuralları: Efe K4 -- 2027-03-31'e kadar H1-H4 için test
penceresi verisiyle ara performans değerlendirmesi yapılmaz (gözlem sonrası
getiri hesabı veya isabet sayımı dahil); kural ve eşikler dondurulmuş
hâliyle kalır. Günlük kayıtlar `run_holdout_test.py` girdisi değildir;
holdout testi kaynağı 2027-03-31 sonrası tek seferlik dönem kaydıdır. D0
karşılaştırması yalnız 2026-09-25 ve öncesi tarihlerde yapılır. Gözlem
çıktısı revize/kesinleşmemiş günleri yalnız işaretler, bastırmaz.

Sonuç ve riskler: Günlük toplama hâlâ bir insan adımı (`initData`) içerir;
tam insansız çalışma (Aşama B, `initData`'nın anahtar deposunda saklanması)
ayrı bir ADR ister. Kaynak tek (Mini App); protokol kırılırsa günlük veri
durur. Yahoo kullanım koşulları otomatik çekimi yasaklıyor olabilir
(doğrulanmadı); Efe'nin Mini App beyanı Yahoo'yu kapsamaz, fiyat için
ayrı ve önceki beyanla sınırlıdır. Kayan kayıtlar için yaklaşık 3 MB/gün
bütçe tahmini ölçüm değildir; disk saklama/temizlik politikası kararlaştırılmadı.
Günlük sıra: `collect_daily` -> (ilk canlı çalıştırmadan sonra)
`compare_daily_d0` -> `scan_observations --daily-root` ->
`notify_observations` (varsayılan dry-run, `--send`);
bkz. `docs/live_check_runbook.md` bölüm 6g.

### Günlük analiz notu: Claude Opus 5.5, Claude Code CLI (`fintr.llm_note`, aşama 2 adım 4)

Problem: Deterministik bildirim (`fintr.notify`) hangi kuralın koşulunun sağlandığını
söylüyor; Efe'nin her gözlem için ham kurum bazlı AKD/takas verisine bakıp "veri bu
hipotezin yönünü destekliyor mu?" sorusunu elle cevaplaması gerekiyordu. Bu bir
kanıt değerlendirmesidir; işlem kararı değildir.

Alternatifler: (a) modelin doğrudan "al/sat" kararı vermesi -- AGENTS.md sınırlarına
aykırı ve doğrulanamaz; reddedildi (yönetici önerisi, Efe kabul etti). (b) özetlenmiş
özellik paketi göndermek -- model yalnız tarayıcının hesabını tekrar ederdi, kurum
bazlı kanıta bakamazdı; Efe ham veriyi seçti. (c) Anthropic API anahtarı -- ayrı
faturalama/secret yönetimi; Efe aboneliğiyle Claude Code CLI başsız modu
(`claude -p`) kullanılacak. (d) Modelin serbest metin üretmesi ve çıktıya güvenmek --
rakam uydurma/yasak ifade riski; reddedildi: çıktı JSON şemalıdır ve kodla
fail-closed doğrulanır.

Karar (2026-10-03, Efe; ADR kaydı `docs/prereg/DECISIONS.md`): Telegram "sinyal chati" düzeni v3
(`fintr.notify.layout_v3`). Problem: v2'nin çok mesajı ve uzun metinleri okunmuyor; ayrı not
mesajları da dağınık. Alternatifler: (a) v2'yi kısaltmak -- reddedildi, v1/v2 baytları korunmalı;
(b) notu v3 mesajından ayrı göndermek -- reddedildi, tek mesaj istendi; (c) bildirimi nota
beklettirmek -- reddedildi, AGENTS.md deterministik bildirimin nota bağlı düşmemesini şart
koşar. Karar: v3 tek mesaj, hisse başına tek satır; satıra yalnız kabul edilmiş notun kanıt
kelimesi (yoksa `—`) girer; pipeline notu mesajdan önce çalıştırır (zaman sınırlı) ve sonucu ne
olursa olsun mesajı gönderir; receipt/devam v2 ile aynı. Sonuç: not metni artık telefonda
görünmez (kayıt `data/interim/llm_notes` altında kalır); `--layout v2` eski akışı korur.

Karar: 2026-10-02 (Efe) -- `scripts/llm_note.py` (`src/fintr/llm_note/`),
`run_daily_pipeline.py --llm-note` ile açılan (varsayılan kapalı) adım: tarama ile
bildirim arasında çalışır, bildirim dosyasındaki `condition_met` hisseleri için
`as_of` sonrası veri ve fiyat serisi içermeyen ham paket (`packet.py`;
birleştirici ile aynı hash/bütünlük kontrollü yükleyiciler, holdout/DSTKF koruması)
kurar. **Hisse başına bir paket ve bir model çağrısı**: ilk tasarımdaki tek paket
(400 000 bayt sınırı, alfabetik sondan hisse atma) gerçek veride 13 hissenin 5'ini
sessizce dışarıda bırakıyordu; reddedildi. Şimdi her hissenin paketi paylaşılan
kural/sütun bağlamı + o hissenin verisidir; tek paket `max_symbol_packet_bytes`'ı
aşarsa o hisse kırpılmadan "alınamadı (packet_too_large)" olur, `max_symbols_per_run`'ı
aşan hisseler "alınamadı (symbol_limit)" olur, `usage_limit`/`cli_not_found` sonrası
kalanlar çağrılmadan aynı kategoriyle, `max_run_seconds` sonrası `run_time_budget`
ile kaydedilir (hiçbir hisse sessizce düşmez). Her çağrı Opus 5.5'e `--tools ""` ile
verilir ve cevap doğrulanır (`validate.py`: şema, izinli değer, kapsam, kanıt yolu
çözümü ve genişlik sınırı, yasak ifade, yazıyla sayı, rakam/tarih denetimi). Rakam
denetimi gerekçe bazlıdır: gerekçedeki her rakam/tarih, o gerekçenin kendi
`evidence_paths` altındaki değerlerden biri olmalıdır; işaret eşleşir; `%` için `p%`
paketteki `p` ile aynen veya `p/100` yalnız `|v| <= 1` değerlerle eşleşir; yuvarlama
toleransı yok (alternatif: yalnız büyüklük eşleşmesi + bütün paket -- işaret çevirme
ve tesadüfi eşleşme riski nedeniyle reddedildi). Her gözlem için zorunlu `plain_explanation` (finans bilgisi olmayana 2-4 kısa cümle;
koşulun gündelik karşılığı, bugünkü sayıların anlamı, hipotezin varsayımı ve gerekçesi --
`docs/prereg/H*.md` "Hipotez"/"Gerekçe" bölümleri paylaşılan kural bağlamında pakete
girer) aynı doğrulayıcıdan geçer; rakamları gerekçelerin yollarından veya kuralın kendi
girdisinden gelmelidir. Yuvarlama yalnız yazılan hassasiyette kabul edilir (en az bir
ondalık yazılmış sayı, o hassasiyette yuvarlanan paket değeriyle eşleşir; tam sayı
aynen; >4 ondalık reddedilir), böylece mesajda ham 30 haneli sayı olmaz. Telegram:
her notlu hisse kendi mesajı + kapanış mesajı. Kalan sınırlar: küçük tam sayılar
yol altındaki başka değerle tesadüfen eşleşebilir; "bir"/"yarım" gibi sözcükler ve
türetilmiş anlatımlar denetlenemez. Reddedilen not gösterilmez. Kayıtlar
`data/interim/llm_notes/<as_of>/<SYM>/attempt-NNN/` altında no-overwrite saklanır
(hisse başına paket, ham yanıt, doğrulama, çağrı süresi ve CLI'nin bildirdiği
kullanım). Aynı gün yeniden çalıştırma yalnız doğrulanmış notu olmayan hisseleri
tekrar dener. Yükleyici `as_of`'a duyarlıdır: `as_of`'tan sonra başlayan günlük
kayıtlar açılmaz ve fiyat dosyasının yalnız `as_of`'a kadar kapsaması yeter (canlı
çalışma ertesi günün kaydını fiyat dosyasından önce yazabilir; eski `as_of`'un yeniden
çalıştırılması da bu yüzden başarısız olmamalı). `notify_observations.py
--llm-note-dir` yalnız notu, kendi mesajı ve kendi makbuzuyla (`note_receipt.json`)
yazar/gönderir (kayıt bütünlüğü yeniden doğrulanır; notu olmayan her hisse için kendi
durum satırı ve sayı özeti). Boru hattı sırası: toplama, tarama, deterministik
bildirim, not, not bildirimi -- deterministik bildirim modeli beklemez (ilk tasarımdaki
"not önce, tek mesaj" sırası reddedildi: 13 sıralı çağrı bildirimi geciktirirdi) ve
not hatası gönderilmiş bildirimi/çıkış kodunu değiştirmez. Çocuk `claude -p`
izolasyonu: `--tools ""`, `--strict-mcp-config`, `--disable-slash-commands`,
`--setting-sources local`, `--no-session-persistence`, her çağrıda yeni boş geçici
çalışma dizini, API anahtarı değişkenleri çıkarılmış ortam; `--bare` kullanılamaz
(OAuth girişini okumaz). İzolasyon makinede `--probe-isolation` ile (tek kanarya
sorusu: kanarya sözcükleri config'te, `llm_notes/_probes/` kaydı) doğrulanır; `isolation_probe` config bayrağı bunu
her çalıştırmada fail-closed ön kontrol yapar.

Doğrulayıcı sürümü 2 (`validate.VALIDATOR_VERSION`; attempt kaydında
`validation.validator_version`) ve negatif örnek derlemi: `tests/fixtures/llm_note_cases/`
(sentetik paket + 43 elle yazılmış not çıktısı; 6 kabul, 37 ret) `tests/test_llm_note_cases.py`
ile gerçek doğrulayıcıdan geçer; doğrulayıcının yanlış karar verdiği örnekler
silinmez, `known_gap` ile işaretlenip mevcut davranışı sabitlenir (değişirse test
bildirir). İlk derlemde 16 açık vardı; 12'si deterministik ve yanlış-ret riski düşük
olduğu için kapatıldı: (a) yasak ifade denetimi NFKC + görünmez/birleştirici karakter
temizliği + ASCII ve Kiril/Yunanca benzeri harf katlaması + harf harfe yazım ("k a z a n ç")
+ uzun gövdelerde ayraçsız eşleşme ("ka zanç"), `kâr`/`karlılık` çekimleri; (b) yazıyla sayı
denetimi çekimli biçimleri (ikisi, beşi, üçüncü, ikişer, -larca/-lerce), İngilizce sayı
sözcüklerini ve rakam benzeri simgeleri (½, ², ①, Roman rakamı) da yakalar; (c) kanıt yolu
en çok 40 değer kapsar (önce 600): prompt zaten "tek kurum satırı, tek gün" ister, gerçek
8 kabul edilmiş notta en geniş yol 8 değerdi; geniş yol başka günün/kurumun değerini
"yolun altında" gösteriyordu. Gerçek kabul edilmiş 8 not (+ paketleri) yeni doğrulayıcıdan
salt okunur geçirildi: 8/8 geçer. Kalan, bilinen ve belgelenmiş açıklar (hepsi anlamsal
sadakat sınıfı; sayı veya sözcük eşleşir ama yorum yanlış olabilir): (1) doğru sayı aynı
günün başka kurumuna atfedilebilir (yol bir günün tamamıysa); (2) doğru sayı aynı satırın
başka alanına atfedilebilir (net_size/total_size/total_cost tek yolda); (3) oran `%` işaretiyle
yazılırsa (0,05 yerine %0,05) `%p` paketteki `p` ile aynen eşleştiği için yakalanmaz --
alan başına birim bilgisi yok; (4) karar sözcüğü (`destekliyor`/`zayıflatıyor`/`belirsiz`)
kendi gerekçesindeki kanıtla çelişebilir veya metin başka bir karar sözcüğü içerebilir;
bu anlamsal yargı kodla denetlenmez. Ayrıca "bir"/"yarım" gibi sözcükler, "on"/"yüz" gibi
kısa köklerin çekimleri denetlenemez.

Sonuç: Bu adım AGENTS.md'de açıkça belirtilmiş bir istisnadır (yalnız kanıt
değerlendirmesi; emir, hedef, stop, pozisyon, getiri tahmini veya puan yok). Riskler:
(1) ham verinin Anthropic'e gönderimi için lisans kapsamı doğrulanmadı (Efe kararı);
(2) LLM çıktısı deterministik değildir -- tekrar üretilebilirlik kaydı attempt
dizinidir; (3) rakam denetimi modelin uydurduğu sayıyı yakalar ama yorumun doğruluğunu
kanıtlamaz; (4) "destekliyor/zayıflatıyor" doğrulanmamış hipotez yönüne göredir ve
holdout sonucu bunu desteklemezse adım kapatılmalıdır; (5) CLI bayrakları sürüme
bağlıdır (`runner.build_command` tek yerdedir) ve abonelik limitine takılırsa not
"alınamadı" olur, deterministik bildirim etkilenmez. Ayrıntı `docs/notify.md`,
`docs/live_check_runbook.md` 6i ve `docs/prereg/DECISIONS.md`.
