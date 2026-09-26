# FinTR-LLM ürün spesifikasyonu

Durum: Taslak v0.2
Kapsam: Türkiye/Borsa İstanbul piyasa mikroyapısı analizi

## 1. Amaç

FinTR-LLM; Borsa İstanbul için derinlik, gerçekleşen işlemler, aracı kurum
dağılımı (AKD), takas/saklama, kurum bilgisi ve fiyat-hacim verilerini ortak bir
şemada birleştirip Türkçe açıklayan bir araştırma sistemidir. LLM, deterministik
olarak hesaplanan özellikleri yorumlar; kaynak veriyi veya hesap motorunu ikame
etmez.

Sistem yatırım tavsiyesi, hedef fiyat ya da doğrudan al/sat kararı vermek için
tasarlanmaz. Çıktının amacı mevcut veriyi özetlemek, farklı zaman ufuklarındaki
bulguları karşılaştırmak, çelişkileri ve veri eksiklerini göstermek ve kanıtın
izin verdiği ölçüde koşullu yorum üretmektir.

## 2. Veri kapsamı

İlk ürün dilimi şu veri ailelerini kapsar:

- kademe bazlı alış/satış derinliği (`depth_update`);
- aracı kurum dağılımı (`get_akd`, `akd_snapshot`);
- takas/saklama anlık görünümü ve dönem farkları;
- takas dağılımı, geçmişi ve yatırımcı istatistikleri;
- aracı kurum kodu ve unvanı eşlemesi;
- ileride eklenecek fiyat, hacim ve gerçekleşen işlem serileri.

Her kayıt sembolünü, gözlem/işlem tarihini ve mümkünse kaynak zaman damgasını
korur. Kimlik bilgileri, token, cookie ve session alanları kalıcı veri modelinin
parçası değildir.

## 3. Temel ayrımlar

### 3.1 AKD ile takas aynı veri değildir

AKD, seçilen işlem dönemi içindeki aracı kurum alış/satış akışını gösterir.
Takas/saklama ise belirli tarihteki saklama bakiyesini veya tarihler arasındaki
değişimi gösterir. Sistem bu iki veri ailesini birbirinin yerine kullanamaz ve
aynı kurum kodunun görünmesini aynı yatırımcı ya da aynı ekonomik neden olarak
yorumlayamaz.

### 3.2 Gözlem ile neden aynı şey değildir

Emir defteri dengesizliği, yoğun kurum alımı veya takas değişimi bir gözlemdir.
Bunlardan manipülasyon, koordinasyon, sahiplik, niyet ya da gelecekteki fiyat
yönü doğrudan çıkarılamaz. Nedensel iddialar için ayrıca doğrulanabilir kanıt
gerekir.

### 3.3 Deterministik hesap ile LLM yorumu ayrıdır

Toplam lot, yoğunlaşma, spread ve dönem farkları feature engine tarafından açık
formüllerle hesaplanır. LLM sayıları yeniden hesaplamak yerine verilen kaynak ve
özellikleri açıklamak, ilişkilendirmek ve sınırlamalarını belirtmekle yükümlüdür.

## 4. Çıktı sözleşmesi

Bir analiz çıktısı, görevle ilgili olduğu ölçüde şunları içermelidir:

1. **Veri kapsamı:** Sembol, dönem, kullanılan veri aileleri ve eksik alanlar.
2. **Kanıtlanmış gözlemler:** Kaynak değerler ve deterministik özellikler.
3. **Çok ufuklu karşılaştırma:** Gün içi, 1 gün, 7 gün, 30 gün ve 90 gün
   sonuçlarını karıştırmadan ayrı yorum.
4. **Uyum ve çelişkiler:** Derinlik, AKD, takas ve fiyat-hacim bulgularının
   birbirini desteklediği veya ayrıştığı noktalar.
5. **Koşullu yorum:** Alternatif açıklamalar ve nedensellik sınırı.
6. **Eksik veri:** Yalnızca sonucu maddi biçimde geliştirecek spesifik alan,
   dönem ve gerekçe.

## 5. Fonksiyonel gereksinimler

### FR-01 — Sayısal sadakat

Kaynak değerlerin işareti, birimi, sembolü ve tarihi korunmalıdır. Türetilmiş her
değer belgelenmiş bir formüle dayanmalı; sıfır payda için yanıltıcı oran
üretilmemelidir.

### FR-02 — Derinlik analizi

Sistem toplam ve ilk beş kademe alış/satış lotlarını, alış-satış dengesizliğini,
spread'i ve spread yüzdesini hesaplayabilmelidir. Tek anlık derinlik görüntüsünü
kalıcı talep veya gelecekteki işlem garantisi olarak sunmamalıdır.

### FR-03 — AKD analizi

Sistem en yüksek net alıcı/satıcıları, ilk beş yoğunlaşmasını ve kurum maliyeti
ile piyasa fiyatı farkını hesaplayabilmelidir. Negatif net değerleri korumalı;
kurum kodu eksik satırı geçerli kabul etmemelidir.

### FR-04 — Takas/saklama analizi

Sistem 1/7/30/90 günlük değişimleri, ilk beş saklama yoğunlaşmasını ve kurum
akışının zaman içindeki yön tutarlılığını hesaplayabilmelidir. Takas değişimini
tek başına aynı günkü işlem, nihai yatırımcı kimliği veya niyet kanıtı saymamalıdır.

### FR-05 — Çok kaynaklı muhakeme

Derinlik, AKD, takas ve fiyat-hacim verileri ayrı kaynaklar olarak etiketlenmeli;
farklı tarih veya kapsamlı veriler doğrudan birleştirilmemelidir. Uyumlu ve
çelişkili sinyaller ayrı gösterilmelidir.

### FR-06 — Veri yeterliliği

Sistem istenen yorum için veri yeterliyse gereksiz ek veri istememelidir. Veri
yetersizse genel bir talep yerine gereken veri ailesini, sembolü, dönemi ve
analizdeki kullanımını belirtmelidir.

### FR-07 — Belirsizlik ve nedensellik

Olgu, deterministik hesap, çıkarım ve hipotez açıkça ayrılmalıdır. Kanıt olmadan
“manipülatif”, “topluyor”, “dağıtıyor” gibi niyet veya suçlayıcı etiketler
kullanılmamalıdır.

### FR-08 — Fiyat tahmini sınırı

Sistem desteklenmeyen fiyat yönü, getiri, hedef fiyat veya kesin zamanlama
tahmini üretmemelidir. Olası senaryolar sunulursa koşullar ve ters senaryo da
belirtilmelidir.

### FR-09 — Eksik ve hatalı veri

Parser yanlış tipi açık hata ile reddetmeli; sayısal metni, boş tarihi veya
eksik kurum kodunu sessizce dönüştürmemelidir. Gerçek sıfır ile eksik değer
birbirinden ayrılmalıdır.

### FR-10 — İzlenebilirlik

Normalize kayıtlar sembol, tarih/zaman, kaynak mesaj türü ve dönüşüm sürümüne
kadar izlenebilir olmalıdır. Feature çıktısı kullanılan formül sürümünü ve girdi
zamanını taşıyabilmelidir.

## 6. Kapsam dışı

- Sürekli/otonom veri toplama ve ticari veri yayını; mevcut kısa ömürlü,
  kullanıcı onaylı WebSocket sorguları kapsam içindedir;
- dataset toplama, KAP scraping veya RAG;
- training, PEFT/LoRA, TRL ve QLoRA;
- emir iletimi, portföy yönetimi veya otomatik al/sat;
- tek metrikten üretilen keyfi güven/risk skoru;
- kaynakta olmayan kurum, yatırımcı, fiyat veya hacim bilgisini uydurma.

## 7. Kalite kapıları

- Sayısal sadakat ve sembol/tarih eşleşmesinde kritik hata toleransı sıfırdır.
- AKD/takas karışıklığı ve desteklenmeyen fiyat tahmini ayrıca kritik hata olarak
  raporlanır.
- Evaluation fixture'ları analiz akışını veya prompt seçimini ayarlamak için
  kullanılmaz; geliştirme pilotları bağımsız evaluation sayılmaz.
- Parser ve feature formülleri LLM olmadan birim testlerinden geçmelidir.
- Model değerlendirmesi `eval/rubrics/market_microstructure_v0.md` ile
  sürümlenmelidir.

## 8. İlk ürün dilimi kabul ölçütleri

Sanitize edilmiş fixture'ların tümü stdlib veri modellerine parse edilmeli;
eksik alan ve yanlış tip testleri açık hata üretmelidir. İstenen feature'lar
deterministik biçimde hesaplanmalı ve formülleri belgelenmelidir. Güncel veri
önce kaynak, zaman ve yerel istek provenance'ı ile doğrulanmalı;
ardından sınırlı analiz bağlamı Efe'nin daha güçlü yönetici modelle yapacağı
incelemeyi desteklemelidir. Repository yerel model inference'ı, model eğitimi
veya doğrudan model API çağrısı içermez.
