# Financial document analysis rubric v0

Bu rubric model, prompt ve retrieval sürümlerini aynı koşullarda karşılaştırmak
için ilk taslaktır. Her vaka; kaynak doküman, gold facts, kabul edilen hesaplar,
maddi bilgi listesi ve veri yeterliliği etiketi içermelidir.

## Puanlama ilkeleri

- Boyutlar mümkün olduğunda atomik iddia veya alan düzeyinde ölçülür.
- Mikro ve makro ortalamalar birlikte raporlanır; doküman türü kırılımı korunur.
- Kritik sayı/olgu uydurması ayrıca “critical failure” olarak işaretlenir ve
  toplam ortalamanın içinde gizlenmez.
- Uygulanamayan boyutlar `N/A` olur; sıfır sayılmaz.
- İnsan değerlendirmesinde en az iki değerlendirici ve uyuşmazlık çözümü hedeflenir.

## 1. Numerical accuracy

**Amaç:** Üretilen ve hesaplanan tüm sayısal iddiaların doğruluğu.

Kontroller: değer, işaret, para birimi, ölçek, dönem, yüzde puan/yüzde ayrımı ve
formül. Kaynak sayılar ile türetilen sayılar ayrı raporlanır.

```text
numerical_accuracy = doğru sayısal iddia / değerlendirilen sayısal iddia
```

Yanlış ölçek veya dönem, değer rakamsal olarak eşleşse bile yanlış kabul edilir.

## 2. Factual grounding

**Amaç:** Doğrulanabilir her iddianın verilen doküman veya izinli retrieval
kaynağı tarafından desteklenmesi.

```text
factual_grounding_precision = destekli doğrulanabilir iddia / tüm doğrulanabilir iddia
```

Kaynak dışı genel finans bilgisi, dokümana özgü olgu gibi sunulamaz. Kaynak
konumu doğruluğu ayrıca kaydedilir.

## 3. Key information extraction

**Amaç:** Gold setteki maddi olay, taraf, tarih, koşul, tutar ve dipnotların
yakalanması.

Gold bilgi birimleri üzerinde precision, recall ve F1 raporlanır. Maddilik
ağırlıkları annotation şemasında önceden tanımlanır; model çıktısına bakılarak
değiştirilmez.

## 4. Financial reasoning

**Amaç:** Muhasebe/finans bağlantılarının doğru, koşullu ve kapsamı aşmadan
kurulması.

0–3 analitik ölçek:

- **0:** Temel ilişki yanlış veya rapor/piyasa ayrımı ihlal edilmiş.
- **1:** Kısmen doğru; önemli karşılaştırılabilirlik ya da nedensellik sorunu var.
- **2:** Doğru ana çıkarım; bazı koşullar veya alternatif açıklamalar eksik.
- **3:** Doğru, yeniden izlenebilir, varsayımları ve alternatifleri açık çıkarım.

Bu ölçek güven/risk skoru değil, değerlendirici rubric puanıdır.

## 5. Missing-data detection

**Amaç:** Yetersiz girdide maddi eksikleri doğru saptamak ve spesifik istemek.

Gold eksik-veri öğeleri üzerinde precision/recall/F1 kullanılır. Bir talep ancak
istenen alan, dönem/doküman ve neden gerektiği belirtilmişse doğru sayılır.

## 6. Unnecessary-data-request rate

**Amaç:** Mevcut veri yeterliyken gereksiz ek bilgi istemeyi ölçmek.

```text
unnecessary_request_rate = gereksiz talep içeren yeterli-veri vakası / tüm yeterli-veri vakası
```

Düşük değer daha iyidir. Güvenli ve eksiksiz yanıt zaten mümkünse genel amaçlı
“daha fazla veri gönderin” ifadesi gereksiz taleptir.

## 7. Uncertainty handling

**Amaç:** Belirsizlikleri doğru yerde ifade etmek; olgu, hesap, varsayım ve
olasılığı ayırmak.

0–3 analitik ölçek:

- **0:** Belirsiz bilgiyi kesinleştiriyor veya açık kanıtı sebepsiz reddediyor.
- **1:** Belirsizlik dili var fakat kaynağı/etkisi belirsiz.
- **2:** Ana belirsizlikleri ve analiz etkisini doğru belirtiyor.
- **3:** Çelişki ve kapsam sınırlarını kaynakla bağlıyor; neyin sonucu
  değiştireceğini spesifik açıklıyor.

## 8. Hallucination rate

**Amaç:** Kaynakta veya izinli hesapta bulunmayan iddiaların sıklığını ölçmek.

```text
hallucination_rate = desteksiz doğrulanabilir iddia / tüm doğrulanabilir iddia
case_hallucination_rate = en az bir desteksiz iddialı vaka / tüm vakalar
```

Düşük değer daha iyidir. Uydurulan finansal sayı, tarih, denetim görüşü, endeks
üyeliği veya şirket eylemi ayrıca kritik hallüsinasyon olarak sayılır.

## Zorunlu rapor kırılımları

- doküman türü: KAP, finansal tablo, faaliyet raporu, dipnot;
- görev türü: özet, çıkarım, karşılaştırma, veri yeterliliği;
- şirket ve dönem (gizlilik/lisans elverdiği ölçüde);
- retrieval açık/kapalı;
- kaynak belgenin OCR/parsing kalite bandı;
- kritik failure listesi ve örnek kimlikleri.

## Kalibrasyon işi

İlk pilot annotation turunda iddia parçalama kılavuzu, maddilik tanımı,
değerlendiriciler arası uyum ve sürüm kabul eşikleri belirlenecektir. Bu değerler
model sonucu görüldükten sonra geriye dönük optimize edilmeyecektir.

